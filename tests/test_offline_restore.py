from __future__ import annotations

from pathlib import Path

from endstone_endkeep.logical.amulet_reader import iter_visible_state
from endstone_endkeep.offline.cli import command_restore, command_verify
from endstone_endkeep.repository.manifest import ManifestStore
from endstone_endkeep.repository.objects import ObjectStore
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


def test_restore_older_snapshot_ignores_corruption_after_target(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    repo = storage / "repo"

    manifest = ManifestStore(repo).load_current()
    assert manifest is not None
    assert len(manifest.chain) == 2

    objects = ObjectStore(repo, compression_level=6, compression_threads=1)
    later_sidecar = objects.path_for(manifest.chain[1].sidecar.logical_sha256)
    payload = later_sidecar.read_bytes()
    later_sidecar.write_bytes(payload[:-1])

    restored = tmp_path / "restored-first"
    assert command_restore(repo, restored, "20261006-120000") == 0
    assert (restored / "level.dat").read_bytes() == b"first"
    assert (restored / "levelname.txt").read_bytes() == b"test-world"

    with iter_visible_state(restored / "db") as state:
        assert list(state) == [(b"a", b"1"), (b"b", b"2")]
