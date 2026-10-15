from __future__ import annotations

import io
import threading
import time
from pathlib import Path, PurePosixPath

import pytest

from endstone_endkeep.bds.model import SnapshotEntry
from endstone_endkeep.logical.sidecar import write_sidecar
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.repository.progress import JobCancelled
from endstone_endkeep.repository.transaction import RepositoryTransaction
from endstone_endkeep.staging.clone import clone_world
from endstone_endkeep.worker.server import WorkerApplication
from tests.standalone_fixture import add_raw
from tests.test_worker import _settings


def test_clone_cancel_preserves_raw_during_byte_copy(tmp_path: Path, monkeypatch) -> None:
    raw = tmp_path / "raw"
    source = raw / "level" / "db" / "large.ldb"
    source.parent.mkdir(parents=True)
    original = b"raw" * (1024 * 1024)
    source.write_bytes(original)
    work = tmp_path / "work"
    destination = work / "s1" / "level" / "db" / "large.ldb"
    monkeypatch.setattr("endstone_endkeep.staging.clone._FICLONE", 0)

    def cancel() -> None:
        if destination.exists() and destination.stat().st_size >= 1024 * 1024:
            raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        clone_world(raw, "level", work, "s1", cancel_check=cancel)
    assert source.read_bytes() == original
    assert destination.exists(), "cancellation should defer clone cleanup until startup"


def test_sidecar_cancel_preserves_raw(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()
    original = b"sidecar" * (512 * 1024)
    (raw / "large").write_bytes(original)
    stream = io.BytesIO()

    def cancel() -> None:
        if stream.tell() >= 1024 * 1024:
            raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        write_sidecar(stream, raw, [SnapshotEntry(PurePosixPath("large"), len(original))], cancel_check=cancel)
    assert len(stream.getvalue()) < len(original)
    assert (raw / "large").read_bytes() == original


def test_object_verification_can_cancel_without_changing_object(tmp_path: Path) -> None:
    objects = ObjectStore(tmp_path / "repo", compression_level=1, compression_threads=1)
    metadata, _ = objects.create(lambda stream: stream.write(b"data" * (1024 * 1024)))
    path = objects.path_for(metadata.logical_sha256)
    original = path.read_bytes()

    def cancel() -> None:
        raise JobCancelled("stop")

    with pytest.raises(JobCancelled):
        objects.verify(metadata, cancel_check=cancel)
    assert path.read_bytes() == original
    objects.verify(metadata)


def test_controller_death_finishes_commit_then_cancels_remaining_raw(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "backups"
    dead = threading.Event()
    monkeypatch.setattr(
        "endstone_endkeep.worker.server.process_identity",
        lambda pid: "new" if pid == 200 else (None if dead.is_set() else "owner"),
    )
    app = WorkerApplication(storage, priority="background", controller_pid=100, controller_start="owner")
    app.handle({"command": "configure", "settings": _settings()})
    first, second = "20261014-120000", "20261014-130000"
    add_raw(storage, first, [(b"a", b"first")], b"first")
    add_raw(storage, second, [(b"a", b"second")], b"second")
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    commit = RepositoryTransaction.commit

    def blocked_commit(transaction, manifest):
        def hook(point):
            if point == "after_manifest_fsync":
                entered.set()
                assert release.wait(5)

        transaction.fault_hook = hook
        return commit(transaction, manifest)

    monkeypatch.setattr(RepositoryTransaction, "commit", blocked_commit)
    app.set_stop_callback(stopped.set)
    monitor = threading.Thread(target=app.monitor, daemon=True)
    monitor.start()
    try:
        assert app.handle({"command": "start_maintenance", "mode": "LOGIC_ONLY"})["accepted"]
        assert entered.wait(2)
        dead.set()
        deadline = time.monotonic() + 2
        while not app.shutdown_requested and time.monotonic() < deadline:
            time.sleep(0.02)
        assert app.shutdown_requested
        assert not stopped.is_set()
        response = app.handle(
            {
                "command": "attach",
                "controller_pid": 200,
                "controller_start": "new",
                "controller_generation": app.controller_generation,
            }
        )
        assert response["ok"] is False and "exiting" in response["error"]
        release.set()
        assert stopped.wait(3)
        monitor.join(timeout=2)
        manifest = app.service.manifests.load_current()
        assert manifest is not None and [n.snapshot for n in manifest.chain] == [first]
        assert (storage / "raw" / second).is_dir()
        assert app.results[0]["payload"]["kind"] == "cancelled"
    finally:
        release.set()
        app.shutdown_requested = True
        monitor.join(timeout=5)
        app.close()


@pytest.mark.parametrize("kind", ["mutation", "export"])
def test_management_job_cancel_preserves_repository(tmp_path: Path, monkeypatch, kind: str) -> None:
    from endstone_endkeep.repository.mutation import SnapshotMutator
    from tests.standalone_fixture import build_fixture

    storage = tmp_path / "backups"
    build_fixture(storage)
    app = WorkerApplication(storage, priority="background")
    app.handle({"command": "configure", "settings": _settings()})
    service = app.service
    original = service.manifests.load_current()
    entered, release = threading.Event(), threading.Event()
    if kind == "mutation":
        operation = SnapshotMutator._rollover

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return operation(*args, **kwargs)

        monkeypatch.setattr(SnapshotMutator, "_rollover", blocked)
        assert service.start_mutation("rollover", "20261006-163000", original.generation)
    else:
        from endstone_endkeep.offline import cli

        operation = cli.command_restore

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return operation(*args, **kwargs)

        monkeypatch.setattr(cli, "command_restore", blocked)
        assert service.start_export("20261006-163000")
    try:
        assert entered.wait(2)
        assert service.request_cancel()
        release.set()
        deadline = time.monotonic() + 3
        result = None
        while result is None and time.monotonic() < deadline:
            result = service.poll()
            time.sleep(0.01)
        assert result is not None and result.kind == "cancelled"
        assert service.manifests.load_current() == original
        assert not (storage / "exports" / "20261006-163000").exists()
    finally:
        release.set()
        app.close()


@pytest.mark.parametrize(
    "point,new_committed",
    [
        ("after_manifest_fsync", False),
        ("after_manifest_rename", False),
        ("after_manifest_dir_fsync", False),
        ("after_head_fsync", False),
        ("after_commit", True),
    ],
)
def test_process_kill_during_commit_recovers_and_restores(tmp_path: Path, point: str, new_committed: bool) -> None:
    import subprocess
    import sys

    from endstone_endkeep.offline.cli import command_restore
    from endstone_endkeep.repository.lock import RepositoryLock
    from endstone_endkeep.repository.manifest import ManifestStore
    from endstone_endkeep.repository.recovery import StartupRecovery
    from endstone_endkeep.repository.verify import RepositoryVerifier

    storage, ready = tmp_path / "backups", tmp_path / "ready"
    process = subprocess.Popen(
        [sys.executable, "-m", "tests.transaction_crash_fixture", str(storage), point, str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[1],
    )
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child must reach the requested on-disk commit boundary"
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
    manifests = ManifestStore(storage / "repo")
    objects = ObjectStore(storage / "repo", compression_level=6, compression_threads=1)
    with RepositoryLock(storage / "worker-owner"), RepositoryLock(storage / "repo"):
        recovery = StartupRecovery(storage, manifests, objects).run()
    current = manifests.load_current()
    assert current is not None and current.generation == (3 if new_committed else 2)
    assert ("20261006-200000" in recovery.pending_raw) is not new_committed
    RepositoryVerifier(manifests, objects).verify(deep=True)
    assert command_restore(storage / "repo", tmp_path / "restored", None) == 0
    assert (tmp_path / "restored" / "db").is_dir()


def test_cancel_reaches_merge_that_deletes_every_record() -> None:
    from endstone_endkeep.logical.format import DeltaOperation
    from endstone_endkeep.logical.merge import apply_delta

    consumed = 0
    total = 32768

    def state():
        nonlocal consumed
        for index in range(total):
            consumed += 1
            yield index.to_bytes(4, "big"), b"value"

    def cancel():
        if consumed >= 16384:
            raise JobCancelled("stop during non-emitting merge")

    deletions = (DeltaOperation.delete(index.to_bytes(4, "big")) for index in range(total))
    with pytest.raises(JobCancelled):
        list(apply_delta(state(), deletions, cancel_check=cancel))
    assert consumed < total


@pytest.mark.parametrize("stage", ["logicalize", "commit"])
def test_cancelled_raw_can_retry_in_same_service(tmp_path: Path, monkeypatch, stage: str) -> None:
    storage = tmp_path / "backups"
    app = WorkerApplication(storage, priority="background")
    app.handle({"command": "configure", "settings": _settings()})
    snapshot = "20261014-120000"
    add_raw(storage, snapshot, [(b"a", b"value")], b"world")
    service = app.service
    entered, release = threading.Event(), threading.Event()
    if stage == "logicalize":
        create = service.logicalizer._create_base

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return create(*args, **kwargs)

        monkeypatch.setattr(service.logicalizer, "_create_base", blocked)
    else:
        commit = RepositoryTransaction.commit

        def blocked(transaction, manifest):
            def pause(point):
                if point == "after_manifest_fsync":
                    entered.set()
                    assert release.wait(5)

            transaction.fault_hook = pause
            return commit(transaction, manifest)

        monkeypatch.setattr(RepositoryTransaction, "commit", blocked)
    try:
        assert service.start_maintenance("LOGIC_ONLY")
        assert entered.wait(2)
        assert service.request_cancel()
        release.set()
        for kind in ("cancelled", "maintenance"):
            deadline = time.monotonic() + 3
            result = None
            while result is None and time.monotonic() < deadline:
                result = service.poll()
                time.sleep(0.01)
            assert result is not None and result.kind == kind
            if kind == "cancelled":
                assert service.start_maintenance("LOGIC_ONLY")
            else:
                assert not result.maintenance.failures
                assert [r.snapshot_id for r in result.maintenance.committed] == (
                    [snapshot] if stage == "logicalize" else []
                )
                assert [n.snapshot for n in service.manifests.load_current().chain] == [snapshot]
                assert not (storage / "raw" / snapshot).exists()
    finally:
        release.set()
        app.close()
