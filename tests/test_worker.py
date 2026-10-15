from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from endstone_endkeep.worker.client import RepositoryWorkerClient, WorkerError, WorkerTimeout
from endstone_endkeep.worker.process import process_identity
from endstone_endkeep.worker.protocol import RuntimeInfo
from endstone_endkeep.worker.server import WorkerApplication


def _poll_with_retry(client: RepositoryWorkerClient):
    try:
        return client.poll()
    except WorkerTimeout:
        return None


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
            maintenance_result = _poll_with_retry(second)

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
            result = _poll_with_retry(second)

        assert result is not None
        result_id, payload = result
        assert payload["kind"] == "verify"
        assert payload["verify"]["deep"] is False

        # Results are non-destructive until the controller confirms that it
        # handled them, so a short poll timeout cannot lose completion state.
        repeated = _poll_with_retry(second)
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
        assert _poll_with_retry(third) is None
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
            result = _poll_with_retry(client)

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


def test_detach_preserves_worker_and_reconnects(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    first = RepositoryWorkerClient.connect_or_start(storage, priority="background")
    try:
        first.configure(_settings())
        first.detach()
        second = RepositoryWorkerClient.connect_or_start(storage, priority="background")
        assert second.runtime.instance_id == first.runtime.instance_id
        assert second.runtime.pid == first.runtime.pid
        assert second.start_verify(deep=False)
        deadline = time.monotonic() + 5
        result = None
        while time.monotonic() < deadline and result is None:
            result = _poll_with_retry(second)
            time.sleep(0.05)
        assert result is not None
        assert second.ack_result(result[0])
        second.shutdown()
        RepositoryWorkerClient._wait_for_exit(
            first.runtime.pid, first.runtime_path, process_start=first.runtime.process_start
        )
    finally:
        if RepositoryWorkerClient._runtime_alive(first.runtime):
            try:
                first.shutdown()
            except WorkerError:
                pass


def test_orphan_worker_exits_with_persisted_unacked_result(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "backups"
    app = WorkerApplication(storage, priority="background", controller_pid=1234, controller_start="old")
    app._queue_result({"kind": "maintenance", "request_id": "saved"})
    stopped = threading.Event()
    app.set_stop_callback(stopped.set)
    monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda pid: None)
    thread = threading.Thread(target=app.monitor, daemon=True)
    thread.start()
    assert stopped.wait(3), "orphan must exit even with an unacknowledged result"
    thread.join(timeout=2)
    next_app = WorkerApplication(storage, priority="background")
    assert list(next_app.results)[0]["payload"]["request_id"] == "saved"
    assert next_app.handle({"command": "ack_result", "result_id": 1})["accepted"]
    next_app.close()
    assert not WorkerApplication(storage, priority="background").results


def test_worker_owner_attach_rejects_live_foreign_controller(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background", controller_pid=100, controller_start="first")
    monkeypatch.setattr(
        "endstone_endkeep.worker.server.process_identity",
        lambda pid: {100: "first", 200: "second"}.get(pid),
    )
    response = app.handle(
        {
            "command": "attach",
            "controller_pid": 200,
            "controller_start": "second",
            "controller_generation": app.controller_generation,
        }
    )
    assert response["ok"] is False
    assert app.controller_pid == 100
    monkeypatch.setattr(
        "endstone_endkeep.worker.server.process_identity",
        lambda pid: {200: "second"}.get(pid),
    )
    response = app.handle(
        {
            "command": "attach",
            "controller_pid": 200,
            "controller_start": "second",
            "controller_generation": app.controller_generation,
        }
    )
    assert response["ok"] is False and "exiting" in response["error"]
    assert app.controller_pid == 100
    assert app.shutdown_requested


def test_worker_reconnect_recovers_from_transient_rpc_timeout(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "backups"
    first = RepositoryWorkerClient.connect_or_start(storage, priority="background")
    original_rpc = RepositoryWorkerClient._rpc
    count = 0

    def flaky_rpc(self, payload, *, timeout=None):
        nonlocal count
        if payload.get("command") == "ping" and count == 0:
            count += 1
            raise WorkerTimeout("temporary unresponsive worker")
        return original_rpc(self, payload, timeout=timeout)

    try:
        monkeypatch.setattr(RepositoryWorkerClient, "_rpc", flaky_rpc)
        second = RepositoryWorkerClient.connect_or_start(storage, priority="background")
        assert count == 1
        assert second.runtime.instance_id == first.runtime.instance_id
        second.shutdown()
    finally:
        monkeypatch.setattr(RepositoryWorkerClient, "_rpc", original_rpc)
        if RepositoryWorkerClient._runtime_alive(first.runtime):
            first.shutdown()


def test_worker_unresponsive_does_not_start_second_writer(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "backups"
    first = RepositoryWorkerClient.connect_or_start(storage, priority="background")
    monkeypatch.setattr(
        RepositoryWorkerClient, "_rpc", lambda *a, **kw: (_ for _ in ()).throw(WorkerTimeout("blocked"))
    )
    monkeypatch.setattr("endstone_endkeep.worker.client.time.sleep", lambda unused: None)
    with pytest.raises(WorkerError, match="four RPC attempts"):
        RepositoryWorkerClient.connect_or_start(storage, priority="background")
    assert RepositoryWorkerClient._load_runtime(first.runtime_path).instance_id == first.runtime.instance_id
    monkeypatch.undo()
    first.shutdown()


def test_reused_pid_does_not_count_as_same_runtime(tmp_path: Path, monkeypatch) -> None:
    runtime = RuntimeInfo(
        protocol=1,
        version="x",
        pid=12345,
        port=123,
        token="x",
        instance_id="old",
        storage_root=str(tmp_path),
        priority="background",
        process_start="original",
    )
    monkeypatch.setattr("endstone_endkeep.worker.client.process_identity", lambda pid: "reused")
    assert not RepositoryWorkerClient._runtime_alive(runtime)


def test_worker_process_lifetime_lock_prevents_second_writer(tmp_path: Path) -> None:
    storage = tmp_path / "backups"
    first = RepositoryWorkerClient.connect_or_start(storage, priority="background")
    args = [
        sys.executable,
        "-m",
        "endstone_endkeep.worker",
        "--storage-root",
        str(storage),
        "--runtime-file",
        str(storage / "worker-runtime.json"),
        "--priority",
        "background",
        "--controller-pid",
        str(os.getpid()),
        "--controller-start",
        process_identity(os.getpid()) or "",
        "--launch-id",
        "competing-worker-test",
    ]
    try:
        second = subprocess.run(args, capture_output=True, text=True, timeout=10)
        assert second.returncode != 0, second.stdout
        assert RepositoryWorkerClient._load_runtime(first.runtime_path).instance_id == first.runtime.instance_id
        assert RepositoryWorkerClient._runtime_alive(first.runtime)
    finally:
        first.shutdown()


def test_attach_cannot_revive_worker_after_exit_has_started(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background", controller_pid=100, controller_start="dead")
    monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda pid: "new" if pid == 200 else None)
    stopping = threading.Event()
    release = threading.Event()

    def stop():
        stopping.set()
        release.wait(3)

    app.set_stop_callback(stop)
    thread = threading.Thread(target=app.monitor, daemon=True)
    thread.start()
    try:
        assert stopping.wait(2)
        response = app.handle(
            {
                "command": "attach",
                "controller_pid": 200,
                "controller_start": "new",
                "controller_generation": app.controller_generation,
            }
        )
        assert response["ok"] is False, "a worker committed to exit cannot promise ownership to a new controller"
        assert "exiting" in response["error"]
        assert app.handle({"command": "shutdown"})["ok"] is True
    finally:
        release.set()
        thread.join(timeout=3)
        app.close()


@pytest.mark.parametrize("abandonment", ["controller-death", "unload", "reload"])
def test_running_job_cancels_after_abandonment_but_survives_reload(
    tmp_path: Path, monkeypatch, abandonment: str
) -> None:
    from tests.standalone_fixture import add_raw

    storage = tmp_path / "backups"
    app = WorkerApplication(storage, priority="background", controller_pid=100, controller_start="owner")
    monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda pid: "owner")
    app.handle({"command": "configure", "settings": _settings()})
    add_raw(storage, "20261014-120000", [(b"a", b"one"), (b"b", b"two")], b"world")
    entered = threading.Event()
    release = threading.Event()
    stopped = threading.Event()
    service = app.service
    assert service is not None
    logicalize = service.logicalizer.logicalize

    def blocked_logicalize(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return logicalize(*args, **kwargs)

    monkeypatch.setattr(service.logicalizer, "logicalize", blocked_logicalize)
    assert app.handle({"command": "start_maintenance", "mode": "LOGIC_ONLY"})["accepted"]
    assert entered.wait(2)
    if abandonment == "controller-death":
        monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda pid: None)
    else:
        monkeypatch.setattr(
            "endstone_endkeep.worker.server._HANDOFF_GRACE_SECONDS", 0 if abandonment == "unload" else 15
        )
        app.handle({"command": "detach", "controller_generation": app.controller_generation})
        if abandonment == "reload":
            assert app.handle(
                {
                    "command": "attach",
                    "controller_pid": 100,
                    "controller_start": "owner",
                    "controller_generation": app.controller_generation,
                }
            )["ok"]
            assert app.service is service
    app.set_stop_callback(stopped.set)
    monitor = threading.Thread(target=app.monitor, daemon=True)
    monitor.start()
    try:
        if abandonment != "reload":
            deadline = time.monotonic() + 2
            while not app.shutdown_requested and time.monotonic() < deadline:
                time.sleep(0.02)
            assert app.shutdown_requested
        assert not stopped.is_set(), "shutdown must wait until the job reaches a safe checkpoint"
        assert not app.handle({"command": "start_verify"}).get("accepted", False)
        release.set()
        if abandonment == "reload":
            deadline = time.monotonic() + 4
            while not app.results and time.monotonic() < deadline:
                time.sleep(0.02)
            assert app.results
            assert not stopped.is_set()
            app.shutdown_requested = True
        assert stopped.wait(4)
        monitor.join(timeout=2)
        manifest = service.manifests.load_current()
        if abandonment == "reload":
            assert manifest is not None
            assert [node.snapshot for node in manifest.chain] == ["20261014-120000"]
        else:
            assert manifest is None
            assert (storage / "raw" / "20261014-120000").is_dir()
        app.close()
        restored_app = WorkerApplication(storage, priority="background")
        payload = restored_app.results[0]["payload"]
        assert payload["kind"] == ("maintenance" if abandonment == "reload" else "cancelled")
        if abandonment == "reload":
            assert payload["maintenance"]["failures"] == []
            assert payload["maintenance"]["committed"][0]["snapshot_id"] == "20261014-120000"
        restored_app.close()
    finally:
        release.set()
        app.shutdown_requested = True
        monitor.join(timeout=5)
        app.close()


def test_monitor_flush_failure_still_triggers_safe_exit(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background")
    app._queue_result({"kind": "maintenance", "request_id": "durable"})
    assert app.handle({"command": "ack_result", "result_id": 1})["accepted"]
    stopped = threading.Event()
    app.set_stop_callback(stopped.set)

    def fail_flush(*args):
        raise OSError("injected worker result flush failure")

    monkeypatch.setattr(app, "_save_results", fail_flush)
    monitor = threading.Thread(target=app.monitor, daemon=True)
    monitor.start()
    try:
        assert stopped.wait(2), "a failed monitor must shut down the worker instead of leaving an orphan"
        assert not app.handle({"command": "configure", "settings": _settings()})["ok"]
    finally:
        monitor.join(timeout=2)
        monkeypatch.undo()
        app.close()


def test_close_drains_service_even_if_result_flush_fails(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background")
    closed = []
    app.service = SimpleNamespace(close=lambda: closed.append(True), poll=lambda: None)
    app._results_dirty = True
    monkeypatch.setattr(app, "_save_results", lambda *args: (_ for _ in ()).throw(OSError("flush failed")))
    with pytest.raises(OSError, match="flush failed"):
        app.close()
    assert closed == [True], "failed result persistence must not skip repository task drain"


def test_delayed_old_detach_cannot_expire_new_controller(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background", controller_pid=100, controller_start="owner")
    monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda pid: "owner")
    first = app.handle(
        {
            "command": "attach",
            "controller_pid": 100,
            "controller_start": "owner",
            "controller_generation": app.controller_generation,
        }
    )
    previous = first["status"]["worker"].get("controller_generation", 0)
    app.handle(
        {
            "command": "attach",
            "controller_pid": 100,
            "controller_start": "owner",
            "controller_generation": app.controller_generation,
        }
    )
    response = app.handle({"command": "detach", "controller_generation": previous})
    assert not response["ok"], "a delayed disable request must not detach the replacement plugin"
    assert app.detached_until is None


def test_reconnect_retries_transient_attach_timeout(tmp_path: Path, monkeypatch) -> None:
    runtime = RuntimeInfo(1, "test", 100, 123, "token", "instance", str(tmp_path), "background", "worker")
    monkeypatch.setattr(RepositoryWorkerClient, "_load_runtime", lambda path: runtime)
    monkeypatch.setattr(
        "endstone_endkeep.worker.client.process_identity", lambda pid: "worker" if pid == 100 else "owner"
    )
    monkeypatch.setattr("endstone_endkeep.worker.client.package_version", lambda: "test")
    status = {"worker": {"pid": 100, "controller_start": "owner", "controller_generation": 1}}
    missed = False

    def rpc(self, payload, *, timeout=None):
        nonlocal missed
        if payload["command"] == "attach" and not missed:
            missed = True
            raise WorkerTimeout("temporary attach timeout")
        return {"ok": True, "status": status}

    monkeypatch.setattr(RepositoryWorkerClient, "_rpc", rpc)
    client = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
    assert missed
    assert client.runtime.instance_id == runtime.instance_id


def test_failed_result_write_is_preserved_for_final_flush(tmp_path: Path, monkeypatch) -> None:
    storage = tmp_path / "backups"
    app = WorkerApplication(storage, priority="background")
    original_save = app._save_results
    first = True

    def fail_once(results, acked):
        nonlocal first
        if first:
            first = False
            raise OSError("temporary result write failure")
        original_save(results, acked)

    monkeypatch.setattr(app, "_save_results", fail_once)
    with pytest.raises(OSError, match="temporary result write failure"):
        app._queue_result({"kind": "maintenance", "request_id": "completed"})
    app.close()
    restored = WorkerApplication(storage, priority="background")
    assert restored.results[0]["payload"]["request_id"] == "completed"
    restored.close()


def test_worker_runtime_is_removed_even_when_final_flush_fails(tmp_path: Path, monkeypatch) -> None:
    from endstone_endkeep.worker import server

    path = tmp_path / "worker-runtime.json"
    monkeypatch.setattr(server._ThreadingServer, "serve_forever", lambda *args, **kwargs: None)
    monkeypatch.setattr(server.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(server.WorkerApplication, "close", lambda self: (_ for _ in ()).throw(OSError("flush failed")))
    with pytest.raises(OSError, match="flush failed"):
        server._run_worker(tmp_path, path, "background", 100, "owner", "test-launch")
    assert not path.exists(), "failed final flush must not leave runtime pointing at an exited worker"


def test_delayed_attach_retry_cannot_replace_successful_attachment(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background", controller_pid=100, controller_start="owner")
    monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda pid: "owner")
    request = {"command": "attach", "controller_pid": 100, "controller_start": "owner", "controller_generation": 0}
    response = app.handle(request)
    acquired = response["status"]["worker"]["controller_generation"]
    assert not app.handle(request)["ok"], "the delayed earlier attempt must not advance ownership again"
    assert app.handle({"command": "detach", "controller_generation": acquired})["ok"]
