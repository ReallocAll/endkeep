from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


@dataclass(frozen=True, order=True)
class SnapshotEntry:
    path: PurePosixPath
    snapshot_bytes: int

    def __post_init__(self) -> None:
        if self.snapshot_bytes < 0:
            raise ValueError("snapshot byte size cannot be negative")


@dataclass(frozen=True)
class SnapshotManifest:
    world_name: str
    entries: tuple[SnapshotEntry, ...]

    def __post_init__(self) -> None:
        if not self.world_name:
            raise ValueError("world_name cannot be empty")
        if not self.entries:
            raise ValueError("snapshot manifest cannot be empty")

    @property
    def total_bytes(self) -> int:
        return sum(entry.snapshot_bytes for entry in self.entries)

    @property
    def file_count(self) -> int:
        return len(self.entries)

    @property
    def sidecar_entries(self) -> tuple[SnapshotEntry, ...]:
        db_prefix = PurePosixPath(self.world_name) / "db"
        return tuple(
            entry
            for entry in self.entries
            if entry.path != db_prefix and db_prefix not in entry.path.parents
        )
