from __future__ import annotations

from pathlib import Path

from endstone_endkeep.logical.amulet_reader import write_fresh_leveldb
from endstone_endkeep.repository.manifest import ManifestStore
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.repository.verify import RepositoryVerifier
from tests.standalone_fixture import build_fixture


def test_deep_verify_reports_object_and_state_progress(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    repo = storage / "repo"

    manifest = ManifestStore(repo).load_current()
    assert manifest is not None

    objects = ObjectStore(repo, compression_level=6, compression_threads=1)
    object_progress: dict[str, tuple[int, int]] = {}
    state_progress: dict[str, tuple[int, int]] = {}

    def on_object(node, role, metadata, current: int, total: int) -> None:
        del node, role
        object_progress[metadata.logical_sha256] = (current, total)

    def on_state(node, current: int, total: int) -> None:
        state_progress[node.snapshot] = (current, total)

    report = RepositoryVerifier(ManifestStore(repo), objects).verify(
        deep=True,
        object_progress=on_object,
        state_progress=on_state,
    )

    assert report.snapshots == len(manifest.chain)
    assert object_progress
    assert all(current == total == 1 for current, total in object_progress.values())
    assert state_progress == {node.snapshot: (node.records, node.records) for node in manifest.chain}


def test_fresh_leveldb_reports_committed_record_progress(tmp_path: Path) -> None:
    updates: list[int] = []
    records, value_bytes = write_fresh_leveldb(
        tmp_path / "db",
        [(b"a", b"1"), (b"b", b"22"), (b"c", b"333")],
        batch_records=2,
        progress=updates.append,
    )

    assert records == 3
    assert value_bytes == 6
    assert updates == [2, 1]
