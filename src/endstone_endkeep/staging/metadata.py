from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from endstone_endkeep.bds.model import SnapshotEntry, SnapshotManifest


@dataclass(frozen=True)
class RawSnapshotMetadata:
    snapshot_id: str
    path: Path
    manifest: SnapshotManifest
    captured_at: str
    scheduled_for: str | None


def load_raw_snapshot(path: Path) -> RawSnapshotMetadata:
    metadata_path = path / "snapshot.json"
    raw = json.loads(metadata_path.read_text(encoding="utf-8"))
    if int(raw.get("schema", -1)) != 1:
        raise ValueError(f"unsupported raw snapshot schema in {metadata_path}")
    snapshot_id = str(raw["id"])
    if snapshot_id != path.name:
        raise ValueError(f"raw snapshot id/path mismatch: {snapshot_id!r} != {path.name!r}")

    entries = tuple(
        SnapshotEntry(PurePosixPath(str(item["path"])), int(item["snapshot_bytes"])) for item in raw["files"]
    )
    manifest = SnapshotManifest(str(raw["world_name"]), entries)
    if int(raw["total_bytes"]) != manifest.total_bytes:
        raise ValueError(f"raw snapshot total byte mismatch: {snapshot_id}")

    scheduled_for = raw.get("scheduled_for")
    if scheduled_for is not None:
        scheduled_for = str(scheduled_for)

    return RawSnapshotMetadata(
        snapshot_id=snapshot_id,
        path=path,
        manifest=manifest,
        captured_at=str(raw["captured_at"]),
        scheduled_for=scheduled_for,
    )
