from __future__ import annotations

from io import BytesIO
from pathlib import Path, PurePosixPath

from endstone_endkeep.bds.model import SnapshotEntry
from endstone_endkeep.logical.sidecar import extract_sidecar, write_sidecar


def test_sidecar_roundtrip(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "level").mkdir(parents=True)
    (raw / "level" / "level.dat").write_bytes(b"level-data")
    (raw / "level" / "levelname.txt").write_bytes(b"world")

    entries = (
        SnapshotEntry(PurePosixPath("level/level.dat"), 10),
        SnapshotEntry(PurePosixPath("level/levelname.txt"), 5),
    )
    archive = BytesIO()
    stats = write_sidecar(archive, raw, entries)
    assert (stats.files, stats.bytes) == (2, 15)

    destination = tmp_path / "restore"
    destination.mkdir()
    archive.seek(0)
    restored = extract_sidecar(archive, destination)
    assert restored == stats
    assert (destination / "level/level.dat").read_bytes() == b"level-data"
    assert (destination / "level/levelname.txt").read_bytes() == b"world"


def test_sidecar_restore_strips_world_prefix(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "level").mkdir(parents=True)
    (raw / "level" / "level.dat").write_bytes(b"abc")
    archive = BytesIO()
    write_sidecar(
        archive,
        raw,
        (SnapshotEntry(PurePosixPath("level/level.dat"), 3),),
    )

    destination = tmp_path / "world"
    destination.mkdir()
    archive.seek(0)
    extract_sidecar(archive, destination, strip_prefix="level")
    assert (destination / "level.dat").read_bytes() == b"abc"
    assert not (destination / "level").exists()
