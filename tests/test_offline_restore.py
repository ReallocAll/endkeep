from __future__ import annotations

from pathlib import Path

from endstone_endkeep.logical.amulet_reader import iter_visible_state
from endstone_endkeep.offline.cli import command_restore, command_verify
from tests.standalone_fixture import build_fixture


def test_repository_only_restore(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)

    repo = storage / "repo"
    assert command_verify(repo) == 0

    restored = tmp_path / "restored-world"
    assert command_restore(repo, restored, "20261006-163000") == 0
    assert (restored / "level.dat").read_bytes() == b"second"
    assert (restored / "levelname.txt").read_bytes() == b"test-world"

    with iter_visible_state(restored / "db") as state:
        assert list(state) == [(b"b", b"changed"), (b"c", b"3")]
