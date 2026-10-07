from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .config import EndKeepConfig

ScheduleKind = Literal["capture", "maintenance"]
MaintenanceMode = Literal["FULL", "LOGIC_ONLY"]


@dataclass(frozen=True)
class ScheduleEvent:
    kind: ScheduleKind
    configured_time: str
    maintenance_mode: MaintenanceMode | None = None
    request_id: str | None = None


def _maintenance_request_id(date_string: str, configured_time: str, mode: MaintenanceMode) -> str:
    return f"maintenance:{date_string}:{configured_time}:{mode}"


class SchedulerState:
    """Durable schedule ledger plus one coalescing pending maintenance slot."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "capture": {},
            "maintenance": {},
            "pending_maintenance": None,
        }
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("scheduler-state.json must contain an object")
        for kind in ("capture", "maintenance"):
            section = raw.get(kind, {})
            if not isinstance(section, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in section.items()
            ):
                raise ValueError(f"scheduler-state.json has invalid {kind!r} section")
            self.data[kind] = dict(section)

        pending = raw.get("pending_maintenance")
        if pending is None:
            self.data["pending_maintenance"] = None
        elif (
            isinstance(pending, dict)
            and pending.get("mode") in ("FULL", "LOGIC_ONLY")
            and isinstance(pending.get("configured_time"), str)
            and isinstance(pending.get("date"), str)
        ):
            mode = str(pending["mode"])
            configured_time = str(pending["configured_time"])
            date_string = str(pending["date"])
            request_id = pending.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                request_id = _maintenance_request_id(
                    date_string,
                    configured_time,
                    mode,  # type: ignore[arg-type]
                )
            self.data["pending_maintenance"] = {
                "mode": mode,
                "configured_time": configured_time,
                "date": date_string,
                "request_id": request_id,
            }
        else:
            raise ValueError("scheduler-state.json has invalid pending_maintenance")

    def already_triggered(self, event: ScheduleEvent, date_string: str) -> bool:
        return self.data[event.kind].get(event.configured_time) == date_string

    def mark_triggered(self, event: ScheduleEvent, date_string: str) -> None:
        self.data[event.kind][event.configured_time] = date_string
        self._save_atomic()

    def queue_maintenance_and_mark_triggered(self, event: ScheduleEvent, date_string: str) -> None:
        if event.kind != "maintenance" or event.maintenance_mode is None or event.request_id is None:
            raise ValueError("only identified maintenance events can be queued")

        current = self.data.get("pending_maintenance")
        replacement = {
            "mode": event.maintenance_mode,
            "configured_time": event.configured_time,
            "date": date_string,
            "request_id": event.request_id,
        }
        if not isinstance(current, dict) or (current.get("mode") != "FULL" and event.maintenance_mode == "FULL"):
            self.data["pending_maintenance"] = replacement

        # Persist the coalesced pending slot and schedule ledger together.
        self.data["maintenance"][event.configured_time] = date_string
        self._save_atomic()

    def pending_maintenance(self) -> ScheduleEvent | None:
        raw = self.data.get("pending_maintenance")
        if not isinstance(raw, dict):
            return None
        return ScheduleEvent(
            "maintenance",
            str(raw["configured_time"]),
            str(raw["mode"]),  # type: ignore[arg-type]
            str(raw["request_id"]),
        )

    def clear_pending_maintenance(self, request_id: str) -> bool:
        current = self.data.get("pending_maintenance")
        if not isinstance(current, dict) or current.get("request_id") != request_id:
            return False
        self.data["pending_maintenance"] = None
        self._save_atomic()
        return True

    def _save_atomic(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        payload = json.dumps(self.data, indent=2, sort_keys=True) + "\n"
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)
        try:
            fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class EndKeepScheduler:
    """Local-clock scheduler with no missed-event catch-up."""

    def __init__(
        self,
        config: EndKeepConfig,
        state: SchedulerState,
        dispatch: Callable[[ScheduleEvent], bool],
    ) -> None:
        self._config = config
        self._state = state
        self._dispatch = dispatch

    @property
    def pending_maintenance(self) -> ScheduleEvent | None:
        return self._state.pending_maintenance()

    def dispatch_pending(self) -> ScheduleEvent | None:
        event = self._state.pending_maintenance()
        if event is None:
            return None
        if not self._dispatch(event):
            return None
        # Keep the durable slot until the matching worker completion is handled.
        return event

    def complete_pending(self, request_id: str) -> bool:
        return self._state.clear_pending_maintenance(request_id)

    def tick(self, now: datetime | None = None) -> list[ScheduleEvent]:
        current = now or datetime.now()
        hhmm = current.strftime("%H:%M")
        date_string = current.date().isoformat()
        due: list[ScheduleEvent] = []

        if hhmm in self._config.capture.times:
            due.append(ScheduleEvent("capture", hhmm))

        if hhmm in self._config.maintenance.times:
            mode = self._config.maintenance.mode_for(hhmm)
            due.append(
                ScheduleEvent(
                    "maintenance",
                    hhmm,
                    mode,  # type: ignore[arg-type]
                    _maintenance_request_id(date_string, hhmm, mode),  # type: ignore[arg-type]
                )
            )

        accepted_events: list[ScheduleEvent] = []
        for event in due:
            if self._state.already_triggered(event, date_string):
                continue

            accepted = self._dispatch(event)
            if not accepted and event.kind == "maintenance":
                self._state.queue_maintenance_and_mark_triggered(event, date_string)
                accepted_events.append(event)
                continue

            if not accepted:
                continue
            self._state.mark_triggered(event, date_string)
            accepted_events.append(event)
        return accepted_events
