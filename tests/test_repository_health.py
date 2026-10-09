from __future__ import annotations

import time
from pathlib import Path

import pytest

from endstone_endkeep.repository.health import RepositoryHealth
from endstone_endkeep.repository.maintenance import RepositoryService
from endstone_endkeep.repository.verify import RepositoryVerifier, VerificationError
from endstone_endkeep.staging.metadata import load_raw_snapshot
from tests.standalone_fixture import add_raw, build_fixture


def _service(storage: Path, *, max_pending: int = 18) -> RepositoryService:
    return RepositoryService(
        storage,
        compression_level=6,
        compression_threads=1,
        max_pending=max_pending,
        max_age_days=3,
        min_free_space_gib=0,
        keep_days=7,
        keep_last=28,
        verify_mode="normal",
    )


def _result(service: RepositoryService):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = service.poll()
        if result is not None:
            return result
        time.sleep(0.01)
    raise TimeoutError("repository operation timed out")


def test_failed_deep_verify_persists_across_worker_restart(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)

    original_verify = RepositoryVerifier.verify

    def fail_verify(self, *, deep=False, object_progress=None, state_progress=None):
        if deep:
            raise VerificationError("injected invalid state")
        return original_verify(self, deep=deep, object_progress=object_progress, state_progress=state_progress)

    service = _service(storage)
    try:
        monkeypatch.setattr(RepositoryVerifier, "verify", fail_verify)
        assert service.start_verify(deep=True)
        with pytest.raises(VerificationError, match="injected invalid state"):
            _result(service)

        assert service.health.failed
        assert "injected invalid state" in service.health.status()["reason"]
        assert (storage / "repository-health.json").is_file()
        with pytest.raises(RuntimeError, match="REPOSITORY FAILED"):
            service.start_maintenance("FULL")
        with pytest.raises(RuntimeError, match="REPOSITORY FAILED"):
            service.plan_mutation("delete", "20261006-120000")
    finally:
        service.close()

    service = _service(storage)
    try:
        assert service.health.failed
        assert service.start_verify(deep=False)
        assert _result(service).kind == "verify"
        assert service.health.failed  # structural success cannot unlock a deep failure

        monkeypatch.setattr(RepositoryVerifier, "verify", original_verify)
        assert service.start_verify(deep=True)
        assert _result(service).kind == "verify"
        assert service.health.status() == {"status": "HEALTHY", "reason": None}
        assert not (storage / "repository-health.json").exists()
        assert service.start_maintenance("FULL")
        assert _result(service).kind == "maintenance"
    finally:
        service.close()


def test_fail_closed_keeps_raw_at_capacity(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    snapshot = "20261007-120000"
    add_raw(storage, snapshot, [(b"x", b"new")], b"level.dat")
    raw = storage / "raw" / snapshot
    assert load_raw_snapshot(raw).snapshot_id == snapshot

    service = _service(storage, max_pending=1)
    try:
        service.health.fail(VerificationError("bad repository"))
        assert service.start_pre_capture()
        with pytest.raises(RuntimeError, match="raw queue is full"):
            _result(service)
        assert raw.exists()
        assert len(service.queue.pending()) == 1
    finally:
        service.close()


def test_fail_closed_allows_safe_raw_without_logicalizing(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    build_fixture(storage)
    snapshot = "20261007-120000"
    add_raw(storage, snapshot, [(b"x", b"new")], b"level.dat")
    raw = storage / "raw" / snapshot

    service = _service(storage, max_pending=3)
    try:
        service.health.fail(VerificationError("bad repository"))
        assert service.start_pre_capture()
        result = _result(service)
        assert result.kind == "pre_capture"
        assert result.raw_limits.logicalized == ()
        assert result.raw_limits.dropped == ()
        assert raw.exists()
    finally:
        service.close()


def test_malformed_health_marker_fails_closed(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    storage.mkdir()
    (storage / "repository-health.json").write_text("{broken", encoding="utf-8")
    health = RepositoryHealth(storage)
    assert health.failed
    with pytest.raises(RuntimeError, match="REPOSITORY FAILED"):
        health.require_healthy()
    health.clear_after_deep_verify()
    assert not RepositoryHealth(storage).failed
