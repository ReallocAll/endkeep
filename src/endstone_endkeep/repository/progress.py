from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Literal

JobState = Literal["idle", "running", "cancel_requested"]


class JobCancelled(RuntimeError):
    """Raised when a repository job reaches a safe cooperative cancellation point."""


@dataclass(frozen=True)
class JobStatus:
    state: JobState
    kind: str | None
    mode: str | None
    stages: tuple[str, ...]
    stage_index: int
    current: int | None
    total: int | None
    unit: str | None
    snapshot: str | None
    detail: str | None
    elapsed_seconds: float
    cancelable: bool

    def to_dict(self) -> dict:
        return asdict(self)


class ProgressTracker:
    def __init__(self) -> None:
        self._lock = RLock()
        self._state: JobState = "idle"
        self._kind: str | None = None
        self._mode: str | None = None
        self._stages: tuple[str, ...] = ()
        self._stage_index = 0
        self._current: int | None = None
        self._total: int | None = None
        self._unit: str | None = None
        self._snapshot: str | None = None
        self._detail: str | None = None
        self._started = 0.0
        self._cancel_requested = False
        self._cancelable = False

    def begin(self, kind: str, stages: tuple[str, ...], *, mode: str | None = None) -> None:
        if not stages:
            raise ValueError("repository jobs require at least one stage")
        with self._lock:
            self._state = "running"
            self._kind = kind
            self._mode = mode
            self._stages = stages
            self._stage_index = 0
            self._current = None
            self._total = None
            self._unit = None
            self._snapshot = None
            self._detail = None
            self._started = time.monotonic()
            self._cancel_requested = False
            self._cancelable = True

    def finish(self) -> None:
        with self._lock:
            self._state = "idle"
            self._kind = None
            self._mode = None
            self._stages = ()
            self._stage_index = 0
            self._current = None
            self._total = None
            self._unit = None
            self._snapshot = None
            self._detail = None
            self._started = 0.0
            self._cancel_requested = False
            self._cancelable = False

    def update(
        self,
        *,
        stage: str | None = None,
        current: int | None = None,
        total: int | None = None,
        unit: str | None = None,
        snapshot: str | None = None,
        detail: str | None = None,
        cancelable: bool | None = None,
    ) -> None:
        with self._lock:
            if self._state == "idle":
                return
            if stage is not None:
                try:
                    self._stage_index = self._stages.index(stage)
                except ValueError as exc:
                    raise ValueError(f"unknown job stage: {stage}") from exc
            self._current = current
            self._total = total
            self._unit = unit
            self._snapshot = snapshot
            self._detail = detail
            if cancelable is not None:
                self._cancelable = cancelable
            self._state = "cancel_requested" if self._cancel_requested else "running"

    def request_cancel(self) -> bool:
        with self._lock:
            if self._state == "idle":
                return False
            self._cancel_requested = True
            self._state = "cancel_requested"
            return True

    def checkpoint(self) -> None:
        with self._lock:
            if self._cancel_requested and self._cancelable:
                raise JobCancelled("repository job cancelled at a safe checkpoint")

    def snapshot(self) -> JobStatus:
        with self._lock:
            elapsed = 0.0 if self._started == 0.0 else max(0.0, time.monotonic() - self._started)
            state: JobState = "cancel_requested" if self._cancel_requested else self._state
            return JobStatus(
                state=state,
                kind=self._kind,
                mode=self._mode,
                stages=self._stages,
                stage_index=self._stage_index,
                current=self._current,
                total=self._total,
                unit=self._unit,
                snapshot=self._snapshot,
                detail=self._detail,
                elapsed_seconds=elapsed,
                cancelable=self._cancelable,
            )
