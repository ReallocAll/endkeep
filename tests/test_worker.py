from __future__ import annotations

import json
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
from endstone_endkeep.worker.server import WorkerApplication


def _poll(client):
    deadline = time.monotonic() + 2
    while True:
        try:
            return client.poll()
        except WorkerTimeout:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _ack(client, result_id):
    deadline = time.monotonic() + 2
    while True:
        try:
            return client.ack_result(result_id)
        except WorkerTimeout:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _shutdown(client):
    client._attach()
    client.shutdown()
    RepositoryWorkerClient._wait_for_exit(client.runtime.pid, client.runtime_path)


def test_attach_requires_proven_identity(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path, priority="background")
    monkeypatch.setattr("endstone_endkeep.worker.server.process_alive", lambda *_: True)
    monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda _: None)
    response = app.handle({"command": "attach", "controller": {"pid": 123, "identity": "claimed", "session": "s"}})
    assert not response["ok"]
    assert app.controller is None


def test_stale_session_cannot_shutdown_successor(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path, priority="background")
    monkeypatch.setattr("endstone_endkeep.worker.server.process_alive", lambda *_: True)
    monkeypatch.setattr("endstone_endkeep.worker.server.process_identity", lambda _: "verified")
    owner = {"pid": 123, "identity": "verified", "session": "new"}
    assert app.handle({"command": "attach", "controller": owner})["ok"]
    response = app.handle({"command": "shutdown", "controller": {**owner, "session": "old"}})
    assert not response["ok"]
    assert not app.shutdown_requested


def test_ack_write_failure_cannot_be_reported_as_success(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path, priority="background")
    app._queue_result({"kind": "pre_capture"})
    app._save_results()
    original = __import__("endstone_endkeep.worker.server", fromlist=["_write_json"])._write_json

    def fail(*_):
        raise OSError("injected journal write failure")

    monkeypatch.setattr("endstone_endkeep.worker.server._write_json", fail)
    with pytest.raises(OSError):
        app.handle({"command": "ack_result", "result_id": 1})
    with pytest.raises(OSError):
        app.handle({"command": "ack_result", "result_id": 1})
    monkeypatch.setattr("endstone_endkeep.worker.server._write_json", original)
    assert app.handle({"command": "ack_result", "result_id": 1})["accepted"]
    assert not WorkerApplication(tmp_path, priority="background").results


def test_deferred_config_error_does_not_kill_monitor(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path, priority="background")
    app.service = SimpleNamespace(busy=False, poll=lambda: None, close=lambda: None)
    app.pending_settings = {}

    def fail(_):
        raise OSError("injected recovery failure")

    monkeypatch.setattr(app, "_apply_settings_locked", fail)
    stopped = threading.Event()
    app.set_stop_callback(stopped.set)
    thread = threading.Thread(target=app.monitor, daemon=True)
    thread.start()
    assert stopped.wait(2), "configuration error killed lifecycle monitor"
    replay = WorkerApplication(tmp_path, priority="background")
    assert "injected recovery failure" in replay.results[0]["payload"]["error"]


def test_pre_capture_replay_does_not_start_unsolicited_capture() -> None:
    from endstone_endkeep.plugin import EndKeepPlugin

    calls = []
    plugin = SimpleNamespace(
        _pending_capture=False,
        _pending_capture_scheduled_for=None,
        _clock=None,
        _capture=SimpleNamespace(start_capture=lambda **kwargs: calls.append(kwargs)),
        _clear_pending_capture=lambda: None,
        logger=SimpleNamespace(error=lambda _: None),
    )
    EndKeepPlugin._handle_repository_result(plugin, {"kind": "pre_capture", "raw_limits": {}})
    assert not calls


def test_detach_hands_off_without_cancelling_and_results_survive_exit(tmp_path: Path, monkeypatch) -> None:
    app = WorkerApplication(tmp_path / "backups", priority="background")
    app._queue_result({"kind": "verify", "verify": {"deep": True}})
    response = app.handle({"command": "detach"})
    assert response["ok"], response
    stopped = threading.Event()
    app.set_stop_callback(stopped.set)
    monkeypatch.setattr("endstone_endkeep.worker.server._HANDOFF_GRACE_SECONDS", 0.1)
    monitor = threading.Thread(target=app.monitor, daemon=True)
    monitor.start()
    assert stopped.wait(3), "unacknowledged result prevented detached worker exit"
    app.close()
    restarted = WorkerApplication(tmp_path / "backups", priority="background")
    result = restarted.handle({"command": "poll"})["result"]
    assert result["payload"] == {"kind": "verify", "verify": {"deep": True}}
    assert restarted.handle({"command": "ack_result", "result_id": result["id"]})["accepted"]
    assert not WorkerApplication(tmp_path / "backups", priority="background").results


def test_worker_exits_after_controller_process_dies(tmp_path: Path) -> None:
    storage = tmp_path / "orphan"
    code = (
        "import sys; from pathlib import Path; "
        "from endstone_endkeep.worker.client import RepositoryWorkerClient; "
        "c=RepositoryWorkerClient.connect_or_start(Path(sys.argv[1]), priority='background'); "
        "print(c.runtime.pid, flush=True)"
    )
    controller = subprocess.run([sys.executable, "-c", code, str(storage)], capture_output=True, text=True, timeout=15)
    assert controller.returncode == 0, controller.stderr
    pid = int(controller.stdout.strip())
    try:
        deadline = time.monotonic() + 8
        while RepositoryWorkerClient._pid_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not RepositoryWorkerClient._pid_alive(pid), "worker survived its controller"
        assert not (storage / "worker-runtime.json").exists()
    finally:
        runtime = RepositoryWorkerClient._load_runtime(storage / "worker-runtime.json")
        if runtime is not None:
            _shutdown(RepositoryWorkerClient(storage / "worker-runtime.json", runtime, desired_priority="background"))


def test_transient_ping_timeout_reconnects_same_worker(tmp_path: Path, monkeypatch) -> None:
    from endstone_endkeep.worker.client import WorkerTimeout

    client = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
    original = RepositoryWorkerClient._rpc
    attempts = 0

    def rpc(self, payload, **kwargs):
        nonlocal attempts
        if payload["command"] == "ping":
            attempts += 1
            if attempts == 1:
                raise WorkerTimeout("injected short timeout")
        return original(self, payload, **kwargs)

    try:
        monkeypatch.setattr(RepositoryWorkerClient, "_rpc", rpc)
        adopted = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
        assert adopted.runtime.instance_id == client.runtime.instance_id
        assert attempts >= 2
    finally:
        _shutdown(client)


def test_second_worker_cannot_replace_runtime(tmp_path: Path) -> None:
    client = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
    try:
        before = (tmp_path / "worker-runtime.json").read_bytes()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "endstone_endkeep.worker",
                "--storage-root",
                str(tmp_path),
                "--runtime-file",
                str(tmp_path / "worker-runtime.json"),
                "--priority",
                "background",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert result.returncode != 0
        assert (tmp_path / "worker-runtime.json").read_bytes() == before
        assert client.refresh()["worker"]["pid"] == client.runtime.pid
    finally:
        _shutdown(client)


def test_reload_adopts_busy_worker_and_stale_detach_cannot_release_it(tmp_path: Path) -> None:
    first = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
    try:
        first.configure(_settings())
        assert first.start_verify(deep=True)
        first.detach()
        second = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
        assert second.runtime.instance_id == first.runtime.instance_id
        with pytest.raises(WorkerError, match="controller has changed"):
            first.detach()
        deadline = time.monotonic() + 5
        result = None
        while result is None and time.monotonic() < deadline:
            time.sleep(0.05)
            result = _poll(second)
        assert result is not None
        assert _ack(second, result[0])
        _shutdown(second)
    finally:
        runtime = RepositoryWorkerClient._load_runtime(tmp_path / "worker-runtime.json")
        if runtime is not None:
            _shutdown(RepositoryWorkerClient(tmp_path / "worker-runtime.json", runtime, desired_priority="background"))


def test_stale_runtime_pid_identity_does_not_touch_unrelated_process(tmp_path: Path) -> None:
    from dataclasses import replace

    first = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
    _shutdown(first)
    RepositoryWorkerClient._wait_for_exit(first.runtime.pid, tmp_path / "worker-runtime.json")
    stale = replace(first.runtime, pid=os.getpid(), process_identity="different-process-start", port=1)
    (tmp_path / "worker-runtime.json").write_text(json.dumps(stale.to_dict()))
    second = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
    try:
        assert second.runtime.pid != os.getpid()
        assert RepositoryWorkerClient._pid_alive(os.getpid())
    finally:
        _shutdown(second)


def test_unresponsive_worker_fails_bounded_without_replacement(tmp_path: Path, monkeypatch) -> None:
    from endstone_endkeep.worker.client import WorkerTimeout

    first = RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
    saved = (tmp_path / "worker-runtime.json").read_bytes()
    original = RepositoryWorkerClient._rpc

    def rpc(self, payload, **kwargs):
        if payload["command"] == "ping":
            raise WorkerTimeout("unresponsive")
        return original(self, payload, **kwargs)

    try:
        monkeypatch.setattr(RepositoryWorkerClient, "_rpc", rpc)
        monkeypatch.setattr(RepositoryWorkerClient, "START_TIMEOUT_SECONDS", 0.2)
        started = time.monotonic()
        with pytest.raises(WorkerError, match="ownership is unproven"):
            RepositoryWorkerClient.connect_or_start(tmp_path, priority="background")
        assert time.monotonic() - started < 1
        assert (tmp_path / "worker-runtime.json").read_bytes() == saved
        assert first.refresh()["worker"]["pid"] == first.runtime.pid
    finally:
        monkeypatch.undo()
        _shutdown(first)


def test_detached_busy_worker_drains_before_exit(tmp_path: Path, monkeypatch) -> None:
    from endstone_endkeep.repository.maintenance import RepositoryJobResult

    app = WorkerApplication(tmp_path, priority="background")
    service = SimpleNamespace(busy=True, poll=lambda: None)
    app.service = service
    app.handle({"command": "detach"})
    monkeypatch.setattr("endstone_endkeep.worker.server._HANDOFF_GRACE_SECONDS", 0.1)
    stopped = threading.Event()
    app.set_stop_callback(stopped.set)
    thread = threading.Thread(target=app.monitor, daemon=True)
    thread.start()
    assert not stopped.wait(0.3)
    with app._lock:
        service.busy = False
        service.poll = lambda: RepositoryJobResult(kind="verify")
    assert stopped.wait(2)
    replay = WorkerApplication(tmp_path, priority="background")
    assert replay.results[0]["payload"]["kind"] == "verify"


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
            maintenance_result = _poll(second)

        assert maintenance_result is not None
        maintenance_result_id, maintenance_payload = maintenance_result
        assert maintenance_payload["kind"] == "maintenance"
        assert maintenance_payload["request_id"] == request_id
        assert second.current_request_id == request_id
        assert _ack(second, maintenance_result_id) is True

        assert second.start_verify(deep=False)
        result = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and result is None:
            time.sleep(0.05)
            result = _poll(second)

        assert result is not None
        result_id, payload = result
        assert payload["kind"] == "verify"
        assert payload["verify"]["deep"] is False

        # Results are non-destructive until the controller confirms that it
        # handled them, so a short poll timeout cannot lose completion state.
        repeated = _poll(second)
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

        assert _ack(adopted_new_plugin, result_id) is True
        assert _poll(third) is None
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
                _shutdown(client)
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
            result = _poll(client)

        assert result is not None
        result_id, payload = result
        assert payload["kind"] == "maintenance"
        assert payload["maintenance"]["verify"]["deep"] is True
        assert _ack(client, result_id) is True
    finally:
        current = RepositoryWorkerClient._load_runtime(storage / "worker-runtime.json")
        if current is not None:
            cleanup = RepositoryWorkerClient(
                storage / "worker-runtime.json",
                current,
                desired_priority=current.priority,
            )
            try:
                _shutdown(cleanup)
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
