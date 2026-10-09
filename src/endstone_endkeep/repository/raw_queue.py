from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from endstone_endkeep.staging.metadata import load_raw_snapshot

from .progress import JobCancelled


@dataclass(frozen=True)
class PendingRaw:
    path: Path
    snapshot_id: str
    captured_at: datetime
    metadata_valid: bool


@dataclass(frozen=True)
class RawLimitResult:
    logicalized: tuple[str, ...]
    dropped: tuple[str, ...]
    blocked_for_space: bool


class RawQueue:
    def __init__(
        self,
        storage_root: Path,
        *,
        max_pending: int,
        max_age_days: int,
        min_free_space_gib: int,
    ) -> None:
        self.storage_root = storage_root
        self.raw_root = storage_root / "raw"
        self.max_pending = max_pending
        self.max_age_days = max_age_days
        self.min_free_bytes = min_free_space_gib * 1024**3

    def pending(self) -> list[PendingRaw]:
        result: list[PendingRaw] = []
        if not self.raw_root.exists():
            return result

        for path in self.raw_root.iterdir():
            if not path.is_dir() or path.name == ".incoming":
                continue
            try:
                metadata = load_raw_snapshot(path)
                captured = datetime.fromisoformat(metadata.captured_at)
                if captured.tzinfo is None:
                    captured = captured.astimezone()
                result.append(PendingRaw(path, metadata.snapshot_id, captured, True))
            except FileNotFoundError:
                continue
            except Exception:
                try:
                    captured = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
                except FileNotFoundError:
                    continue
                result.append(PendingRaw(path, path.name, captured, False))
        result.sort(key=lambda item: (item.captured_at, item.snapshot_id))
        return result

    def free_space_allows(self, additional_bytes: int = 0) -> bool:
        free = shutil.disk_usage(self.storage_root).free
        return free - additional_bytes >= self.min_free_bytes

    def enforce_before_capture(
        self,
        logicalize: Callable[[Path], None],
        *,
        required_bytes: int = 0,
        now: datetime | None = None,
    ) -> RawLimitResult:
        """Enforce hard backlog/age/free-space limits before accepting another raw."""

        if required_bytes < 0:
            raise ValueError("required_bytes cannot be negative")

        current = now or datetime.now().astimezone()
        logicalized: list[str] = []

        while True:
            pending = self.pending()
            if not pending:
                break
            oldest = pending[0]
            too_many = len(pending) >= self.max_pending
            too_old = oldest.captured_at < current - timedelta(days=self.max_age_days)
            low_space = not self.free_space_allows(required_bytes)
            if not (too_many or too_old or low_space):
                break

            try:
                logicalize(oldest.path)
                logicalized.append(oldest.snapshot_id)
                continue
            except JobCancelled:
                raise
            except Exception as exc:
                # A backup system must not destroy an uncommitted recovery point
                # just because conversion failed. Reject the new capture instead.
                raise RuntimeError(
                    f"cannot commit pending raw snapshot {oldest.snapshot_id}; "
                    "refusing new capture while preserving existing recovery data"
                ) from exc

        return RawLimitResult(
            logicalized=tuple(logicalized),
            dropped=(),
            blocked_for_space=not self.free_space_allows(required_bytes),
        )
