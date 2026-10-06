from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from endstone_endkeep.logical.amulet_reader import iter_visible_state, write_fresh_leveldb


def _read_all(db_path: Path) -> list[tuple[bytes, bytes]]:
    with iter_visible_state(db_path) as state:
        return list(state)


@pytest.mark.parametrize(
    "state",
    [
        [],
        [(b"a", b"1")],
        [(b"a", b"1"), (b"b", b"2"), (b"c", b"3")],
    ],
)
def test_iter_visible_state_exhausts_without_high_level_iterator(
    tmp_path: Path,
    state: list[tuple[bytes, bytes]],
) -> None:
    db_path = tmp_path / "db"
    write_fresh_leveldb(db_path, state)

    assert _read_all(db_path) == state


def test_iter_visible_state_exhausts_in_repository_worker_thread(tmp_path: Path) -> None:
    db_path = tmp_path / "db"
    expected = [(b"a", b"1"), (b"b", b"2"), (b"c", b"3")]
    write_fresh_leveldb(db_path, expected)

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(_read_all, db_path).result(timeout=10)

    assert result == expected
