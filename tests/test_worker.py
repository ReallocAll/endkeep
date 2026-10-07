from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from endstone_endkeep.worker.client import RepositoryWorkerClient, WorkerError
from endstone_endkeep.worker.server import WorkerApplication


def _settings(*, verify_mode: str = "normal") -> dict[str, Any]:
    return {
        "compression_level": 6,
        "compression_threads": 1,
        "max_pending": 18,
        "max_age_days": 3,
        "min_free_space_gib": 0,
        "keep_days": 7,
        "keep_last": 28,
        "verify_mode": verify_mode,
    }


def test_worker_starts_reconnects_and_runs_normal_verify(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "backups"
    first = RepositoryWorkerClient.connect_or_start(storage, priority="background")
    pid = first.runtime.pid
    try:
        recovery = first.configure(_settings())
        assert recovery.get("pending_raw", []) == []

        second = RepositoryWorkerClient.connect_or_start(storage, priority="background")
        assert second.runtime.pid == pid
        assert second.runtime.instance_id == first.runtime.instance_id
        second.configure(_settings())

        request_id = "maintenance:2026-10-07:18:30:LOGIC_ONLY"
        assert second.start_maintenance("LOGIC_ONLY", request_id=request_id)
        maintenance_result = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and maintenance_result is None:
            time.sleep(0.05)
            maintenance_result = second.poll()

        assert maintenance_result is not None
        maintenance_result_id, maintenance_payload = maintenance_result
        assert maintenance_payload["kind"] == "maintenance"
        assert maintenance_payload["request_id"] == request_id
        assert second.current_request_id == request_id
        assert second.ack_result(maintenance_result_id) is True

        assert second.start_verify(deep=False)
        result = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and result is None:
            time.sleep(0.05)
            result = second.poll()

        assert result is not None
        result_id, payload = result
        assert payload["kind"] == "verify"
        assert payload["verify"]["deep"] is False

        # Results are non-destructive until the controller confirms that it
        # handled them, so a short poll timeout cannot lose completion state.
        repeated = second.poll()
        assert repeated is not None
        assert repeated[0] == result_id
        assert repeated[1] == payload
        assert second.busy is True
        assert second.start_verify(deep=False) is False

        # An idle worker with an unacknowledged result must survive reload and
        # even a requested priority change; otherwise completion state can be lost.
        third = RepositoryWorkerClient.connect_or_start(storage, priority="balanced")
        assert third.runtime.pid == pid
        assert third.priority_deferred is True

        monkeypatch.setattr("endstone_endkeep.worker.client.package_version", lambda: "next-build")
        adopted_new_plugin = RepositoryWorkerClient.connect_or_start(storage, priority="balanced")
        assert adopted_new_plugin.runtime.pid == pid
        assert adopted_new_plugin.upgrade_deferred is True

        with pytest.raises(WorkerError, match="unacknowledged results"):
            adopted_new_plugin.shutdown()

        assert adopted_new_plugin.ack_result(result_id) is True
        assert third.poll() is None
        assert second.refresh()["job"]["state"] == "idle"

        if os.name == "posix":
            assert (storage / "worker-runtime.json").stat().st_mode & 0o077 == 0
    finally:
        current = RepositoryWorkerClient._load_runtime(storage / "worker-runtime.json")
        if current is not None:
            client = RepositoryWorkerClient(
                storage / "worker-runtime.json",
                current,
                desired_priority=current.priority,
            )
            try:
                client.shutdown()
            except Exception:
                pass
            try:
                RepositoryWorkerClient._wait_for_exit(current.pid, storage / "worker-runtime.json")
            except Exception:
                pass


def test_full_maintenance_honors_configured_deep_verify(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    client = RepositoryWorkerClient.connect_or_start(storage, priority="background")
    try:
        client.configure(_settings(verify_mode="deep"))
        assert client.start_maintenance("FULL")

        result = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and result is None:
            time.sleep(0.05)
            result = client.poll()

        assert result is not None
        result_id, payload = result
        assert payload["kind"] == "maintenance"
        assert payload["maintenance"]["verify"]["deep"] is True
        assert client.ack_result(result_id) is True
    finally:
        current = RepositoryWorkerClient._load_runtime(storage / "worker-runtime.json")
        if current is not None:
            cleanup = RepositoryWorkerClient(
                storage / "worker-runtime.json",
                current,
                desired_priority=current.priority,
            )
            try:
                cleanup.shutdown()
            except Exception:
                pass
            try:
                RepositoryWorkerClient._wait_for_exit(current.pid, storage / "worker-runtime.json")
            except Exception:
                pass


def test_control_poll_does_not_touch_repository_disk_state(tmp_path: Path) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background")

    def disk_access_forbidden():
        raise AssertionError("control-plane poll touched repository disk state")

    app.service = SimpleNamespace(
        busy=False,
        manifests=SimpleNamespace(load_current=disk_access_forbidden),
        queue=SimpleNamespace(pending=disk_access_forbidden),
    )

    response = app.handle({"command": "poll"})
    assert response["ok"] is True
    assert "repository" not in response["status"]
    assert response["status"]["job_occupied"] is False


def test_automatic_control_rpc_timeouts_are_tick_safe(tmp_path: Path, monkeypatch) -> None:
    runtime = SimpleNamespace(
        pid=12345,
        port=19132,
        token="token",
        instance_id="instance",
        protocol=1,
        version="test",
        storage_root=str(tmp_path),
        priority="background",
    )
    client = RepositoryWorkerClient(
        tmp_path / "worker-runtime.json",
        runtime,
        desired_priority="background",
    )
    observed: list[tuple[str, float | None]] = []

    def fake_rpc(payload, *, timeout=None):
        observed.append((payload["command"], timeout))
        return {
            "accepted": True,
            "status": {
                "job": {"state": "running"},
                "job_occupied": True,
                "pending_results": 0,
                "settings_deferred": False,
            },
        }

    monkeypatch.setattr(client, "_rpc", fake_rpc)

    assert client.start_pre_capture(timeout=client.AUTO_CONTROL_TIMEOUT_SECONDS)
    assert client.start_maintenance(
        "LOGIC_ONLY",
        request_id="maintenance:test",
        timeout=client.AUTO_CONTROL_TIMEOUT_SECONDS,
    )

    assert observed == [
        ("start_pre_capture", client.AUTO_CONTROL_TIMEOUT_SECONDS),
        ("start_maintenance", client.AUTO_CONTROL_TIMEOUT_SECONDS),
    ]
    assert client.POLL_TIMEOUT_SECONDS <= 0.01
    assert client.ACK_TIMEOUT_SECONDS <= 0.01
    assert client.AUTO_CONTROL_TIMEOUT_SECONDS <= 0.02
