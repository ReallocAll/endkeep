from __future__ import annotations

import argparse
import json
from pathlib import Path

from endstone_endkeep.logical.amulet_reader import write_fresh_leveldb
from endstone_endkeep.repository.logicalize import Logicalizer


def add_raw(storage: Path, snapshot_id: str, state: list[tuple[bytes, bytes]], sidecar: bytes) -> None:
    raw = storage / "raw" / snapshot_id
    world = raw / "level"
    write_fresh_leveldb(world / "db", state)
    (world / "level.dat").write_bytes(sidecar)
    (world / "levelname.txt").write_bytes(b"test-world")

    files = []
    total = 0
    for path in sorted(world.rglob("*")):
        if path.is_file():
            size = path.stat().st_size
            files.append({"path": path.relative_to(raw).as_posix(), "snapshot_bytes": size})
            total += size

    (raw / "snapshot.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "id": snapshot_id,
                "captured_at": "2026-10-06T12:00:00+08:00",
                "scheduled_for": None,
                "world_name": "level",
                "files": files,
                "total_bytes": total,
            }
        ),
        encoding="utf-8",
    )


def build_fixture(storage: Path) -> None:
    logicalizer = Logicalizer(storage, compression_level=6, compression_threads=1)
    add_raw(storage, "20261006-120000", [(b"a", b"1"), (b"b", b"2")], b"first")
    logicalizer.logicalize(storage / "raw" / "20261006-120000")
    add_raw(
        storage,
        "20261006-163000",
        [(b"b", b"changed"), (b"c", b"3")],
        b"second",
    )
    logicalizer.logicalize(storage / "raw" / "20261006-163000")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("storage", type=Path)
    args = parser.parse_args()
    build_fixture(args.storage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
