from __future__ import annotations

import time
from pathlib import Path

import pytest

from endstone_endkeep.logical.amulet_reader import iter_visible_state
from endstone_endkeep.repository.lock import RepositoryLock
from endstone_endkeep.repository.logicalize import Logicalizer
from endstone_endkeep.repository.maintenance import RepositoryService
from endstone_endkeep.repository.manifest import ManifestStore
from endstone_endkeep.repository.mutation import SnapshotMutator
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
    Logicalizer(storage, compression_level=6, compression_threads=1).logicalize(storage / "raw" / S3)
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
