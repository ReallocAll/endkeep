from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .config import EndKeepConfig

ScheduleKind = Literal["capture", "maintenance"]
MaintenanceMode = Literal["FULL", "LOGIC_ONLY"]


@dataclass(frozen=True)
class ScheduleEvent:
    kind: ScheduleKind
    configured_time: str
    maintenance_mode: MaintenanceMode | None = None


class SchedulerState:
    """Small durable ledger preventing duplicate triggers within the same local date."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict[str, str]] = {"capture": {}, "maintenance": {}}
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

    def already_triggered(self, event: ScheduleEvent, date_string: str) -> bool:
        return self.data[event.kind].get(event.configured_time) == date_string

    def mark_triggered(self, event: ScheduleEvent, date_string: str) -> None:
        self.data[event.kind][event.configured_time] = date_string
        self._save_atomic()

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
    """Local-clock scheduler with no timezone abstraction and no missed-event catch-up."""

    def __init__(
        self,
        config: EndKeepConfig,
        state: SchedulerState,
        dispatch: Callable[[ScheduleEvent], None],
    ) -> None:
        self._config = config
        self._state = state
        self._dispatch = dispatch

    def tick(self, now: datetime | None = None) -> list[ScheduleEvent]:
        current = now or datetime.now()
        hhmm = current.strftime("%H:%M")
        date_string = current.date().isoformat()
        due: list[ScheduleEvent] = []

        if hhmm in self._config.capture.times:
            due.append(ScheduleEvent("capture", hhmm))

        if hhmm in self._config.maintenance.times:
            mode = self._config.maintenance.mode_for(hhmm)
            due.append(ScheduleEvent("maintenance", hhmm, mode))  # type: ignore[arg-type]

        fired: list[ScheduleEvent] = []
        for event in due:
            if self._state.already_triggered(event, date_string):
                continue
            # Persist acceptance before dispatch. A failed job is surfaced operationally
            # and can be retried manually; the scheduler never loops within one minute.
            self._state.mark_triggered(event, date_string)
            self._dispatch(event)
            fired.append(event)
        return fired
