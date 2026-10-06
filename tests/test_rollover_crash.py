from __future__ import annotations

from pathlib import Path

import pytest

from endstone_endkeep.logical.format import DeltaOperation, write_base, write_delta
from endstone_endkeep.logical.merge import hash_state
from endstone_endkeep.repository.gc import collect_orphan_objects, referenced_object_hashes
from endstone_endkeep.repository.manifest import ManifestStore, RepositoryManifest, SnapshotNode
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.repository.recovery import StartupRecovery
from endstone_endkeep.repository.rollover import Rollover
from endstone_endkeep.repository.transaction import RepositoryTransaction


class InjectedCrash(RuntimeError):
    pass


def _fixture(tmp_path: Path) -> tuple[Path, ManifestStore, ObjectStore, RepositoryManifest]:
    storage = tmp_path / "backups"
    repo = storage / "repo"
    objects = ObjectStore(repo, compression_level=6, compression_threads=1)

    state_a = [(b"a", b"1"), (b"b", b"2")]
    state_b = [(b"b", b"changed"), (b"c", b"3")]

    base_meta, base_stats = objects.create(lambda stream: write_base(stream, state_a))
    delta_meta, _ = objects.create(
        lambda stream: write_delta(
            stream,
            [
                DeltaOperation.delete(b"a"),
                DeltaOperation.put(b"b", b"changed"),
                DeltaOperation.put(b"c", b"3"),
            ],
        )
    )
    sidecar_a, _ = objects.create(lambda stream: stream.write(b"sidecar-a"))
    sidecar_b, _ = objects.create(lambda stream: stream.write(b"sidecar-b"))
    state_b_stats = hash_state(state_b)

    base = SnapshotNode(
        snapshot="s1",
        world_name="level",
        type="base",
        object=base_meta,
        sidecar=sidecar_a,
        state_sha256=base_stats.state_sha256,
        records=base_stats.records,
        value_bytes=base_stats.value_bytes,
        captured_at="2026-10-05T12:00:00+08:00",
    )
    delta = SnapshotNode(
        snapshot="s2",
        world_name="level",
        type="delta",
        object=delta_meta,
        sidecar=sidecar_b,
        state_sha256=state_b_stats.sha256,
        records=state_b_stats.records,
        value_bytes=state_b_stats.value_bytes,
        captured_at="2026-10-06T12:00:00+08:00",
    )
    manifest = RepositoryManifest(1, (base, delta))
    store = ManifestStore(repo)
    RepositoryTransaction(store).commit(manifest)
    return storage, store, objects, manifest


@pytest.mark.parametrize(
    ("point", "expected_generation"),
    [
        ("after_manifest_fsync", 1),
        ("after_manifest_rename", 2),
        ("after_manifest_dir_fsync", 2),
        ("after_head_fsync", 2),
        ("after_commit", 2),
    ],
)
def test_rollover_crash_recovers_old_or_new_chain(
    tmp_path: Path,
    point: str,
    expected_generation: int,
) -> None:
    storage, store, objects, manifest = _fixture(tmp_path)

    def crash_hook(current: str) -> None:
        if current == point:
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        Rollover(store, objects, fault_hook=crash_hook).run(manifest, 1)

    StartupRecovery(storage, store, objects).run()
    current = store.load_current()
    assert current is not None
    assert current.generation == expected_generation
    assert current.chain[0].snapshot == ("s1" if expected_generation == 1 else "s2")


def test_gc_interruption_after_rollover_never_deletes_referenced_objects(tmp_path: Path) -> None:
    storage, store, objects, manifest = _fixture(tmp_path)
    new_manifest, _ = Rollover(store, objects).run(manifest, 1)

    def crash_after_first_delete(point: str) -> None:
        if point == "after_gc_delete":
            raise InjectedCrash(point)

    with pytest.raises(InjectedCrash):
        collect_orphan_objects(objects, new_manifest, fault_hook=crash_after_first_delete)

    StartupRecovery(storage, store, objects).run()
    current = store.load_current()
    assert current == new_manifest

    for logical_hash in referenced_object_hashes(current):
        assert objects.path_for(logical_hash).is_file()
