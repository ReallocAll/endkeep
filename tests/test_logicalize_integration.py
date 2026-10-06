from __future__ import annotations

import json
from pathlib import Path

import pytest

from endstone_endkeep.logical.amulet_reader import write_fresh_leveldb
from endstone_endkeep.repository.logicalize import Logicalizer
from endstone_endkeep.repository.reader import RepositoryReader
from endstone_endkeep.staging.metadata import load_raw_snapshot


def _raw_snapshot(
    storage: Path,
    snapshot_id: str,
    state: list[tuple[bytes, bytes]],
    *,
    captured_at: str = "2026-10-06T12:00:00+08:00",
) -> Path:
    raw = storage / "raw" / snapshot_id
    world = raw / "level"
    db = world / "db"
    write_fresh_leveldb(db, state)
    (world / "level.dat").write_bytes(f"sidecar-{snapshot_id}".encode())
    (world / "levelname.txt").write_bytes(b"test")

    files = []
    total = 0
    for path in sorted(world.rglob("*")):
        if path.is_file():
            relative = path.relative_to(raw).as_posix()
            size = path.stat().st_size
            files.append({"path": relative, "snapshot_bytes": size})
            total += size

    raw.mkdir(parents=True, exist_ok=True)
    (raw / "snapshot.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": snapshot_id,
                "captured_at": captured_at,
                "scheduled_for": None,
                "world_name": "level",
                "files": files,
                "total_bytes": total,
            }
        ),
        encoding="utf-8",
    )
    load_raw_snapshot(raw)
    return raw


def test_base_then_two_deltas(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    logicalizer = Logicalizer(storage, compression_level=6, compression_threads=1)

    state_a = [(b"a", b"1"), (b"b", b"2")]
    state_b = [(b"b", b"2"), (b"c", b"3")]
    state_c = [(b"b", b"changed"), (b"c", b"3"), (b"d", b"4")]

    logicalizer.logicalize(_raw_snapshot(storage, "20261006-120000", state_a))
    logicalizer.logicalize(_raw_snapshot(storage, "20261006-163000", state_b))
    logicalizer.logicalize(_raw_snapshot(storage, "20261006-203000", state_c))

    manifest = logicalizer.manifests.load_current()
    assert manifest is not None
    assert [node.type for node in manifest.chain] == ["base", "delta", "delta"]
    assert [node.snapshot for node in manifest.chain] == [
        "20261006-120000",
        "20261006-163000",
        "20261006-203000",
    ]

    reader = RepositoryReader(logicalizer.objects)
    assert list(reader.iter_state(manifest)) == state_c
    assert not (storage / "raw" / "20261006-120000").exists()
    assert not (storage / "raw" / "20261006-163000").exists()
    assert not (storage / "raw" / "20261006-203000").exists()


def test_rejects_raw_older_than_repository_tail(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    logicalizer = Logicalizer(storage, compression_level=6, compression_threads=1)

    logicalizer.logicalize(
        _raw_snapshot(
            storage,
            "20261006-163000",
            [(b"a", b"newer")],
            captured_at="2026-10-06T16:30:00+08:00",
        )
    )
    older = _raw_snapshot(
        storage,
        "20261006-120000",
        [(b"a", b"older")],
        captured_at="2026-10-06T12:00:00+08:00",
    )

    with pytest.raises(ValueError, match="not newer than repository tail"):
        logicalizer.logicalize(older)

    manifest = logicalizer.manifests.load_current()
    assert manifest is not None
    assert [node.snapshot for node in manifest.chain] == ["20261006-163000"]
    assert older.exists()
