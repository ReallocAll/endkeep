"""Compatibility validation for Amulet-LevelDB 3.0.8a1.

Exercise the precise API used by EndKeep, with binary keys/values and
iterator release before DB close.
"""

from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from amulet.leveldb import LevelDB


def main() -> None:
    assert version("amulet-leveldb").split("+", 1)[0] == "3.0.8a1"
    expected = {
        b"\x00": b"alpha",
        b"\x02": b"bravo",
        b"\xff": b"\x00\xff",
        b"binary\x00key": b"binary\x00value",
    }
    with TemporaryDirectory(prefix="amulet-308-compat-") as tmp:
        location = Path(tmp) / "db"
        db = LevelDB(str(location), True)
        db.put(b"\x00", b"alpha")
        db.put_batch({key: value for key, value in expected.items() if key != b"\x00"})
        for key, value in expected.items():
            assert db.get(key) == value
        iterator = db.create_iterator()
        iterator.seek_to_first()
        observed = []
        while iterator.valid():
            observed.append((iterator.key(), iterator.value()))
            iterator.next()
        assert observed == sorted(expected.items()), observed
        del iterator
        db.close()

        reopened = LevelDB(str(location), False)
        for key, value in expected.items():
            assert reopened.get(key) == value
        reopened.close()

    print("PASS: binary KV, batch writes, ordered iterator, close/reopen")


if __name__ == "__main__":
    main()
