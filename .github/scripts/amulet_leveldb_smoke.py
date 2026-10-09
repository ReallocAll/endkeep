"""Isolated smoke test for Amulet-LevelDB 3.0.7a0 experimental manylinux wheels."""

from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from amulet.leveldb import LevelDB


def main() -> None:
    assert version("amulet-leveldb").split("+", 1)[0] == "3.0.7a0"
    expected = {b"\x00": b"alpha", b"\x02": b"bravo", b"\xff": b"\x00\xff"}
    with TemporaryDirectory(prefix="amulet-backport-") as tmp:
        location = Path(tmp) / "db"
        db = LevelDB(str(location), True)
        db.put(b"\x00", b"alpha")
        db.put_batch({b"\x02": b"bravo", b"\xff": b"\x00\xff"})
        for key, value in expected.items():
            assert db.get(key) == value, (key, value)
        iterator = db.create_iterator()
        iterator.seek_to_first()
        got = []
        while iterator.valid():
            got.append((iterator.key(), iterator.value()))
            iterator.next()
        assert got == sorted(expected.items()), got
        del iterator
        db.close()

        reopened = LevelDB(str(location), False)
        for key, value in expected.items():
            assert reopened.get(key) == value
        reopened.close()
    print("OK: CPython wheel import, KV write/batch, sorted iterator, close/reopen")


if __name__ == "__main__":
    main()
