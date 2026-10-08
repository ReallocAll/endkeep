from __future__ import annotations

from pathlib import Path

import pytest

from endstone_endkeep.logical.format import DeltaOperation, write_base, write_delta
from endstone_endkeep.repository.manifest import ManifestStore, RepositoryManifest, SnapshotNode
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.repository.recovery import StartupRecovery
from endstone_endkeep.repository.transaction import RepositoryTransaction


class InjectedCrash(RuntimeError):
    pass


def _node(snapshot: str, node_type: str, metadata, state_hash: str) -> SnapshotNode:
    return SnapshotNode(
        snapshot=snapshot,
        world_name="level",
        type=node_type,  # type: ignore[arg-type]
        object=metadata,
        sidecar=metadata,
        state_sha256=state_hash,
        records=1,
        value_bytes=1,
        captured_at="2026-10-06T12:00:00+08:00",
    )


@pytest.mark.parametrize(
    ("point", "expected_generation"),
    [
        ("after_manifest_fsync", 1),
        ("after_manifest_rename", 1),
        ("after_manifest_dir_fsync", 1),
        ("after_head_fsync", 1),
        ("after_commit", 2),
    ],
)
def test_startup_recovers_old_or_new_authoritative_generation(
    tmp_path: Path,
    point: str,
    expected_generation: int,
) -> None:
    storage = tmp_path / "backups"
    repo = storage / "repo"
    objects = ObjectStore(repo, compression_level=6, compression_threads=1)
    base_meta, base_stats = objects.create(lambda stream: write_base(stream, [(b"a", b"1")]))
    delta_meta, _ = objects.create(lambda stream: write_delta(stream, [DeltaOperation.put(b"a", b"2")]))

    base = _node("s1", "base", base_meta, base_stats.state_sha256)
    store = ManifestStore(repo)
    RepositoryTransaction(store).commit(RepositoryManifest(1, (base,)))

    delta = _node("s2", "delta", delta_meta, "d" * 64)
    new_manifest = RepositoryManifest(2, (base, delta))

    def crash_hook(current: str) -> None:
        if current == point:
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        RepositoryTransaction(store, fault_hook=crash_hook).commit(new_manifest)

    recovery = StartupRecovery(storage, store, objects).run()
    current = store.load_current()
    assert current is not None
    assert current.generation == expected_generation
    assert recovery.recovered_head in (None, 2)
    assert not list((repo / "manifests").glob("*.part"))
    assert not (repo / "HEAD.part").exists()
