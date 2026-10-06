from __future__ import annotations

from collections.abc import Generator, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def iter_visible_state(db_path: Path) -> Generator[Iterator[tuple[bytes, bytes]]]:
    """Open Mojang LevelDB through Amulet and stream its sorted visible KV state."""

    from amulet.leveldb import LevelDB

    db = LevelDB(str(db_path), False)
    try:
        yield db.iterate()
    finally:
        db.close()


def write_fresh_leveldb(
    db_path: Path,
    state: Iterable[tuple[bytes, bytes]],
    *,
    batch_records: int = 4096,
) -> tuple[int, int]:
    """Build a fresh Bedrock LevelDB from canonical visible state."""

    from amulet.leveldb import LevelDB

    if db_path.exists():
        raise FileExistsError(f"restore LevelDB destination already exists: {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = LevelDB(str(db_path), True)
    records = 0
    value_bytes = 0
    batch: dict[bytes, bytes] = {}
    previous: bytes | None = None
    try:
        for key, value in state:
            if previous is not None and key <= previous:
                raise ValueError("restore state is not strictly increasing")
            batch[key] = value
            records += 1
            value_bytes += len(value)
            previous = key
            if len(batch) >= batch_records:
                db.put_batch(batch)
                batch.clear()
        if batch:
            db.put_batch(batch)
    finally:
        db.close()
    return records, value_bytes
