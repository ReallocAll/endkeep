from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from endstone_endkeep.logical.amulet_reader import iter_visible_state
from endstone_endkeep.offline.cli import command_restore
from endstone_endkeep.repository.lock import RepositoryLock
from endstone_endkeep.repository.logicalize import Logicalizer
from endstone_endkeep.repository.maintenance import RepositoryService
from endstone_endkeep.repository.manifest import ManifestStore
from endstone_endkeep.repository.mutation import SnapshotMutator
from endstone_endkeep.worker.client import RepositoryWorkerClient, WorkerRequestIndeterminate, WorkerTimeout
from endstone_endkeep.worker.server import WorkerApplication
from tests.standalone_fixture import add_raw, build_fixture

S1 = "20261006-120000"
S2 = "20261006-163000"
S3 = "20261006-200000"


def _service(storage: Path) -> RepositoryService:
    return RepositoryService(
        storage,
        compression_level=6,
        compression_threads=1,
        max_pending=18,
        max_age_days=3,
        min_free_space_gib=0,
        keep_days=7,
        keep_last=28,
        verify_mode="normal",
    )


def _result(service: RepositoryService):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = service.poll()
        if result is not None:
            return result
        time.sleep(0.01)
    raise TimeoutError("repository management job timed out")


def test_worker_mutation_preview_and_generation_guard(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    service = _service(storage)
    try:
        app = WorkerApplication(storage, priority="background")
        app.service = service
        preview = app.handle({"command": "plan_mutation", "operation": "delete", "snapshot": S1})
        assert preview["ok"] is True
        plan = preview["plan"]
        assert plan["removed"] == [S1]
        assert plan["detail"] == f"replace BASE with {S2}"
        assert plan["remaining"] == 1
        assert service.start_mutation("delete", S1, plan["generation"])
        assert service.request_cancel() is False

        # A repository task cannot be accepted while another job is running.
        assert (
            app.handle(
                {
                    "command": "start_mutation",
                    "operation": "delete",
                    "snapshot": S2,
                    "expected_generation": plan["generation"],
                    "request_id": "e" * 32,
                }
            )["accepted"]
            is False
        )
        outcome = _result(service)
        assert outcome.kind == "mutation"
        assert outcome.mutation["base"] == S2
        assert outcome.mutation["generation"] == plan["generation"] + 1

        current = ManifestStore(storage / "repo").load_current()
        assert current is not None
        assert [node.snapshot for node in current.chain] == [S2]
        assert service.start_mutation("delete", S2, plan["generation"])
        with pytest.raises(RuntimeError, match="changed since preview"):
            _result(service)
        assert ManifestStore(storage / "repo").load_current() == current
    finally:
        service.close()


def test_mutating_middle_delta_retains_restore_state(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    add_raw(storage, S3, [(b"a", b"final"), (b"c", b"3")], b"last")
    third = Logicalizer(storage, compression_level=6, compression_threads=1).logicalize(storage / "raw" / S3)
    # Detect empty/misread Windows clone data before destructive chain edits.
    # Otherwise an empty DELTA could look like a successful delete/export.
    assert third.records == 2, f"third snapshot unexpectedly empty before mutation: {third}"
    service = _service(storage)
    try:
        preview = service.plan_mutation("delete", S2)
        assert preview["removed"] == [S2]
        assert "bridge DELTA" in preview["detail"]
        assert service.start_mutation("delete", S2, preview["generation"])
        result = _result(service)
        assert result.mutation["remaining"] == 2
        manifest = ManifestStore(storage / "repo").load_current()
        assert manifest is not None
        assert [node.snapshot for node in manifest.chain] == [S1, S3]
        # Export the still-restorable successor snapshot through the same worker.
        assert service.start_export(S3)
        export = _result(service)
        assert export.kind == "export"
        destination = storage / "exports" / S3
        assert export.export["destination"] == str(destination)
        with iter_visible_state(destination / "db") as visible:
            assert list(visible) == [(b"a", b"final"), (b"c", b"3")]
        assert (destination / "level.dat").read_bytes() == b"last"
    finally:
        service.close()


def test_rollover_preview_requires_newer_base(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    service = _service(storage)
    try:
        with pytest.raises(ValueError, match="already the BASE"):
            service.plan_mutation("rollover", S1)
        plan = service.plan_mutation("rollover", S2)
        assert plan["removed"] == [S1]
        assert service.start_mutation("rollover", S2, plan["generation"])
        outcome = _result(service)
        assert outcome.mutation["base"] == S2
        assert outcome.mutation["remaining"] == 1
    finally:
        service.close()


def test_export_rejects_unsafe_and_existing_destinations(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    service = _service(storage)
    try:
        for invalid in ("../worlds", "latest", "level/../../worlds", ""):
            with pytest.raises(ValueError, match="exact snapshot ID"):
                service.start_export(invalid)

        assert service.start_export(S2)
        assert service.request_cancel() is False
        assert _result(service).kind == "export"
        assert service.start_export(S2)
        with pytest.raises(FileExistsError, match="already exists"):
            _result(service)
        assert (storage / "exports" / S2 / "level.dat").read_bytes() == b"second"

        # An otherwise absent dangling symlink must not escape the exports root.
        dangling = exports = storage / "exports"
        outside = tmp_path / "outside"
        (dangling / S1).symlink_to(outside, target_is_directory=True)
        assert service.start_export(S1)
        with pytest.raises(ValueError, match="symbolic link"):
            _result(service)
        assert not outside.exists()
        (dangling / S1).unlink()

        # Refuse to follow a top-level export directory symlink.
        exports = storage / "exports"
        exports.rename(storage / "old-exports")
        exports.symlink_to(tmp_path / "outside", target_is_directory=True)
        assert service.start_export(S1)
        with pytest.raises(ValueError, match="symbolic link"):
            _result(service)
        assert not (tmp_path / "outside").exists()
    finally:
        service.close()


def test_external_repository_change_makes_preview_stale(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    service = _service(storage)
    try:
        plan = service.plan_mutation("rollover", S2)
        manifests = ManifestStore(storage / "repo")
        with RepositoryLock(storage / "repo"):
            current = manifests.load_current()
            assert current is not None
            SnapshotMutator(manifests, service.objects).rollover(current, S2)
        assert service.start_mutation("rollover", S2, plan["generation"])
        with pytest.raises(RuntimeError, match="changed since preview"):
            _result(service)
    finally:
        service.close()


def test_worker_deduplicates_accepted_management_requests(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    service = _service(storage)
    try:
        app = WorkerApplication(storage, priority="background")
        app.service = service
        generation = service.plan_mutation("delete", S1)["generation"]
        request = {
            "command": "start_mutation",
            "operation": "delete",
            "snapshot": S1,
            "expected_generation": generation,
            "request_id": "a" * 32,
        }
        assert app.handle(request)["accepted"] is True
        assert app.handle(request)["accepted"] is True  # retry while busy
        conflict = {**request, "snapshot": S2}
        assert app.handle(conflict)["ok"] is False
        assert _result(service).kind == "mutation"
        assert app.handle(request)["accepted"] is True  # retry after completion
        assert ManifestStore(storage / "repo").load_current().generation == generation + 1
    finally:
        service.close()


def test_export_preserves_disk_reserve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    service = _service(storage)
    try:
        monkeypatch.setattr(
            "endstone_endkeep.repository.maintenance.shutil.disk_usage",
            lambda _path: SimpleNamespace(free=1),
        )
        assert service.start_export(S2)
        with pytest.raises(RuntimeError, match="insufficient free space"):
            _result(service)
        assert not (storage / "exports" / S2).exists()
    finally:
        service.close()


def test_export_accepts_suffix_snapshots(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    snapshot = "20261006-200000-01"
    add_raw(storage, snapshot, [(b"a", b"three")], b"third")
    Logicalizer(storage, compression_level=6, compression_threads=1).logicalize(storage / "raw" / snapshot)
    service = _service(storage)
    try:
        assert service.start_export(snapshot)
        result = _result(service)
        assert result.export["snapshot"] == snapshot
        assert (storage / "exports" / snapshot / "level.dat").read_bytes() == b"third"
    finally:
        service.close()


def test_management_client_retries_with_same_request_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(RepositoryWorkerClient)
    client._status = {"job": {"state": "idle"}}
    seen: list[dict] = []

    def rpc(request: dict) -> dict:
        seen.append(dict(request))
        if len(seen) == 1:
            raise WorkerTimeout("lost first response")
        return {"accepted": True, "status": {"job": {"state": "running"}}}

    monkeypatch.setattr(client, "_rpc", rpc)
    assert client.start_export(S2)
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert len(seen[0]["request_id"]) == 32

    def always_timeout(_request: dict) -> dict:
        raise WorkerTimeout("response lost")

    monkeypatch.setattr(client, "_rpc", always_timeout)
    with pytest.raises(WorkerRequestIndeterminate, match="unknown outcome"):
        client.start_export(S2)


def test_restore_disk_guard_failure_removes_partial_export(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    destination = tmp_path / "exported"
    checks = 0

    def reserve_check() -> None:
        nonlocal checks
        checks += 1
        if checks > 1:
            raise RuntimeError("disk reserve reached")

    with pytest.raises(RuntimeError, match="disk reserve reached"):
        command_restore(storage / "repo", destination, S2, space_guard=reserve_check)
    assert checks > 1
    assert not destination.exists()
