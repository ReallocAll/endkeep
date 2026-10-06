from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from endstone_endkeep.bds.model import SnapshotManifest

from .exact import StageResult, stage_manifest


@dataclass(frozen=True)
class StagedRawSnapshot:
    snapshot_id: str
    incoming_path: Path
    manifest: SnapshotManifest
    captured_at: str
    scheduled_for: str | None
    stage: StageResult


class RawSnapshotStore:
    """Crash-safe publication of short-lived raw snapshots."""

    def __init__(self, storage_root: Path, source_root: Path) -> None:
        self.storage_root = storage_root
        self.source_root = source_root
        self.raw_root = storage_root / "raw"
        self.incoming_root = self.raw_root / ".incoming"

    def prepare(self) -> None:
        self.incoming_root.mkdir(parents=True, exist_ok=True)

    def allocate_snapshot_id(self, now: datetime | None = None) -> str:
        current = now or datetime.now()
        base = current.strftime("%Y%m%d-%H%M%S")
        for suffix in range(0, 100):
            snapshot_id = base if suffix == 0 else f"{base}-{suffix:02d}"
            if not (self.raw_root / snapshot_id).exists() and not (self.incoming_root / snapshot_id).exists():
                return snapshot_id
        raise RuntimeError(f"too many snapshot-id collisions for {base}")

    def stage(
        self,
        manifest: SnapshotManifest,
        *,
        scheduled_for: str | None,
        cancel=None,
        now: datetime | None = None,
    ) -> StagedRawSnapshot:
        self.prepare()
        captured = now or datetime.now()
        snapshot_id = self.allocate_snapshot_id(captured)
        incoming = self.incoming_root / snapshot_id
        try:
            result = stage_manifest(self.source_root, incoming, manifest, cancel=cancel)
        except Exception:
            if incoming.exists():
                shutil.rmtree(incoming, ignore_errors=True)
            raise
        return StagedRawSnapshot(
            snapshot_id=snapshot_id,
            incoming_path=incoming,
            manifest=manifest,
            captured_at=captured.astimezone().isoformat(timespec="seconds"),
            scheduled_for=scheduled_for,
            stage=result,
        )

    def publish(self, staged: StagedRawSnapshot) -> Path:
        metadata = {
            "schema": 1,
            "id": staged.snapshot_id,
            "captured_at": staged.captured_at,
            "scheduled_for": staged.scheduled_for,
            "world_name": staged.manifest.world_name,
            "files": [
                {"path": entry.path.as_posix(), "snapshot_bytes": entry.snapshot_bytes}
                for entry in staged.manifest.entries
            ],
            "total_bytes": staged.manifest.total_bytes,
        }
        metadata_path = staged.incoming_path / "snapshot.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(metadata_path, flags, 0o600)
        try:
            payload = (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode()
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write while publishing snapshot metadata")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

        self._fsync_tree(staged.incoming_path)

        final = self.raw_root / staged.snapshot_id
        if final.exists():
            raise FileExistsError(f"raw snapshot already exists: {final}")
        os.rename(staged.incoming_path, final)
        self._fsync_directory(self.raw_root)
        return final

    def discard(self, staged_path: Path) -> None:
        try:
            if staged_path.parent == self.incoming_root and staged_path.exists():
                shutil.rmtree(staged_path)
        except OSError:
            # Cleanup failure must not mask the capture error. Startup recovery will
            # remove abandoned raw/.incoming directories.
            pass

    @classmethod
    def _fsync_tree(cls, root: Path) -> None:
        directories: list[Path] = []
        for current, dirnames, filenames in os.walk(root):
            current_path = Path(current)
            directories.append(current_path)
            for filename in filenames:
                path = current_path / filename
                fd = os.open(path, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        for directory in reversed(directories):
            cls._fsync_directory(directory)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                # Directory fsync is not uniformly supported outside POSIX.
                pass
        finally:
            os.close(fd)
