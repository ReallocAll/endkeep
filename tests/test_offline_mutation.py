from __future__ import annotations

import io
from pathlib import Path

import pytest

from endstone_endkeep.logical.amulet_reader import iter_visible_state
from endstone_endkeep.offline.cli import build_parser, command_mutation, command_restore, command_verify
from endstone_endkeep.repository.lock import RepositoryLock
from endstone_endkeep.repository.logicalize import Logicalizer
from endstone_endkeep.repository.manifest import ManifestStore
from endstone_endkeep.repository.mutation import SnapshotMutator
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.repository.recovery import StartupRecovery
from tests.standalone_fixture import add_raw, build_fixture

S1 = "20261006-120000"
S2 = "20261006-163000"
S3 = "20261006-200000"
S4 = "20261006-230000"
STATES = {
    S1: [(b"a", b"1"), (b"b", b"2")],
    S2: [(b"b", b"changed"), (b"c", b"3")],
    S3: [(b"a", b"new"), (b"b", b"changed"), (b"c", b"3")],
    S4: [(b"a", b"new"), (b"c", b"4"), (b"d", b"last")],
}
SIDECARS = {S1: b"first", S2: b"second", S3: b"third", S4: b"fourth"}


class InjectedCrash(RuntimeError):
    pass


def _fixture(tmp_path: Path) -> tuple[Path, ManifestStore, ObjectStore]:
    storage = tmp_path / "backups"
    build_fixture(storage)
    logicalizer = Logicalizer(storage, compression_level=6, compression_threads=1)
    for snapshot in (S3, S4):
        add_raw(storage, snapshot, STATES[snapshot], SIDECARS[snapshot])
        logicalizer.logicalize(storage / "raw" / snapshot)
    repo = storage / "repo"
    return storage, ManifestStore(repo), ObjectStore(repo, compression_level=6, compression_threads=1)


def _assert_recoverable(tmp_path: Path, store: ManifestStore) -> None:
    repo = store.repo_root
    assert command_verify(repo) == 0
    manifest = store.load_current()
    assert manifest is not None
    for node in manifest.chain:
        destination = tmp_path / f"restore-{node.snapshot}-{manifest.generation}"
        assert command_restore(repo, destination, node.snapshot) == 0
        with iter_visible_state(destination / "db") as state:
            assert list(state) == STATES[node.snapshot]
        assert (destination / "level.dat").read_bytes() == SIDECARS[node.snapshot]


@pytest.mark.parametrize(
    ("operation", "snapshot", "expected"),
    [
        ("delete", S4, (S1, S2, S3)),
        ("delete", S1, (S2, S3, S4)),
        ("delete", S2, (S1, S3, S4)),
        ("rollover", S3, (S3, S4)),
    ],
)
def test_offline_mutations_preserve_every_retained_restore(
    tmp_path: Path, operation: str, snapshot: str, expected: tuple[str, ...]
) -> None:
    _storage, store, objects = _fixture(tmp_path)
    original = store.load_current()
    assert original is not None
    with RepositoryLock(store.repo_root):
        mutator = SnapshotMutator(store, objects)
        updated = mutator.delete(original, snapshot) if operation == "delete" else mutator.rollover(original, snapshot)

    assert tuple(node.snapshot for node in updated.chain) == expected
    assert updated.generation == original.generation + 1
    if snapshot == S2:
        assert updated.chain[1].object != original.chain[2].object
        assert updated.chain[1].state_sha256 == original.chain[2].state_sha256
        assert updated.chain[1].sidecar == original.chain[2].sidecar
    _assert_recoverable(tmp_path, store)


def test_refuse_deleting_only_snapshot_and_noop_rollover(tmp_path: Path) -> None:
    _storage, store, objects = _fixture(tmp_path)
    with RepositoryLock(store.repo_root):
        mutator = SnapshotMutator(store, objects)
        initial = store.load_current()
        assert initial is not None
        only = mutator.rollover(initial, S4)
        with pytest.raises(ValueError, match="only recovery point"):
            mutator.delete(only, S4)
        with pytest.raises(ValueError, match="already the BASE"):
            mutator.rollover(only, S4)
        assert store.load_current() == only
    _assert_recoverable(tmp_path, store)


@pytest.mark.parametrize("operation,snapshot", [("delete", S2), ("rollover", S3)])
@pytest.mark.parametrize(
    ("point", "published"),
    [
        ("after_manifest_fsync", False),
        ("after_manifest_rename", False),
        ("after_manifest_dir_fsync", False),
        ("after_head_fsync", False),
        ("after_commit", True),
    ],
)
def test_interrupted_mutation_keeps_authoritative_head(
    tmp_path: Path, operation: str, snapshot: str, point: str, published: bool
) -> None:
    storage, store, objects = _fixture(tmp_path)
    original = store.load_current()
    assert original is not None

    def fault_hook(current: str) -> None:
        if current == point:
            raise InjectedCrash(point)

    with RepositoryLock(store.repo_root):
        mutator = SnapshotMutator(store, objects, fault_hook=fault_hook)
        with pytest.raises(InjectedCrash):
            if operation == "delete":
                mutator.delete(original, snapshot)
            else:
                mutator.rollover(original, snapshot)

    # HEAD never changes until the atomic replacement, regardless of an
    # already-durable but unpublished manifest generation.
    current = store.load_current()
    assert current is not None
    assert current.generation == (original.generation + 1 if published else original.generation)
    StartupRecovery(storage, store, objects).run()
    current = store.load_current()
    assert current is not None
    assert current.generation == (original.generation + 1 if published else original.generation)
    if not published:
        assert not store.path_for(original.generation + 1).exists()
        # The discarded generation must not block a subsequent successful commit.
        with RepositoryLock(store.repo_root):
            assert SnapshotMutator(store, objects).delete(current, S4).generation == original.generation + 1
    _assert_recoverable(tmp_path, store)


def test_mutation_requires_confirmation_and_reports_orphans(tmp_path: Path, capsys, monkeypatch) -> None:
    _storage, store, _objects = _fixture(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    before = store.load_current()
    with pytest.raises(RuntimeError, match="--yes"):
        command_mutation(store.repo_root, "delete", S2)
    assert store.load_current() == before

    assert command_mutation(store.repo_root, "delete", S2, yes=True) == 0
    printed = capsys.readouterr().out
    assert "bridge DELTA" in printed
    assert "orphan_objects=" in printed
    assert "GC=not_run" in printed
    _assert_recoverable(tmp_path, store)


def test_cli_parser_exposes_offline_only_mutation_flags() -> None:
    for command in ("delete", "rollover"):
        args = build_parser().parse_args([command, S2, "--yes"])
        assert args.command == command
        assert args.snapshot == S2
        assert args.yes is True
