from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .protocol import PROTOCOL_VERSION, RuntimeInfo, package_version, receive_message, send_message


class WorkerError(RuntimeError):
    """Raised when the repository worker cannot be contacted or controlled."""


class WorkerTimeout(WorkerError):
    """Raised when a bounded worker RPC does not answer before its deadline."""


class WorkerRejected(WorkerError):
    """The repository worker explicitly rejected the request."""


class WorkerRequestIndeterminate(WorkerError):
    """A management request may have been accepted despite a lost response."""


class RepositoryWorkerClient:
    START_TIMEOUT_SECONDS = 5.0
    RPC_TIMEOUT_SECONDS = 0.5
    POLL_TIMEOUT_SECONDS = 0.01
    ACK_TIMEOUT_SECONDS = 0.01
    AUTO_CONTROL_TIMEOUT_SECONDS = 0.02
    CONFIGURE_TIMEOUT_SECONDS = 30.0

    def __init__(self, runtime_path: Path, runtime: RuntimeInfo, *, desired_priority: str) -> None:
        self.runtime_path = runtime_path
        self.runtime = runtime
        self.desired_priority = desired_priority
        self._status: dict[str, Any] = {"job": {"state": "idle"}}

    @classmethod
    def connect_or_start(cls, storage_root: Path, *, priority: str) -> RepositoryWorkerClient:
        storage_root.mkdir(parents=True, exist_ok=True)
        runtime_path = storage_root / "worker-runtime.json"

        existing = cls._load_runtime(runtime_path)
        if existing is not None:
            if existing.protocol != PROTOCOL_VERSION:
                if cls._pid_alive(existing.pid):
                    raise WorkerError(
                        f"repository worker protocol mismatch: {existing.protocol} != {PROTOCOL_VERSION}; "
                        "restart the server after the active worker has exited"
                    )
                runtime_path.unlink(missing_ok=True)
                existing = None

        if existing is not None:
            client = cls(runtime_path, existing, desired_priority=priority)
            try:
                response = client._rpc({"command": "ping"})
            except Exception as exc:
                if cls._pid_alive(existing.pid):
                    raise WorkerError(
                        f"repository worker pid {existing.pid} is alive but not responding; "
                        "refusing to start a second worker"
                    ) from exc
                runtime_path.unlink(missing_ok=True)
            else:
                client._status = response["status"]
                if Path(existing.storage_root).resolve() != storage_root.resolve():
                    raise WorkerError("repository worker runtime points at a different storage root")

                needs_restart = existing.priority != priority or existing.version != package_version()
                can_restart = needs_restart and not client.busy
                if can_restart:
                    client.shutdown()
                    cls._wait_for_exit(existing.pid, runtime_path)
                else:
                    return client

        return cls._start(storage_root, runtime_path, priority)

    @classmethod
    def _start(cls, storage_root: Path, runtime_path: Path, priority: str) -> RepositoryWorkerClient:
        runtime_path.unlink(missing_ok=True)
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(sys.path)
        env.pop("LD_PRELOAD", None)

        command = [
            sys.executable,
            "-m",
            "endstone_endkeep.worker",
            "--storage-root",
            str(storage_root),
            "--runtime-file",
            str(runtime_path),
            "--priority",
            priority,
        ]
        kwargs: dict[str, Any] = {
            "env": env,
            "stdin": subprocess.DEVNULL,
            "close_fds": True,
        }
        if os.name == "nt":
            # Use a process group on Windows; start_new_session is POSIX-only.
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        log_path = storage_root / "worker.log"
        with log_path.open("ab", buffering=0) as log:
            kwargs["stdout"] = log
            kwargs["stderr"] = subprocess.STDOUT
            process = subprocess.Popen(command, **kwargs)

        threading.Thread(
            target=process.wait,
            name=f"endkeep-worker-reaper-{process.pid}",
            daemon=True,
        ).start()

        deadline = time.monotonic() + cls.START_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise WorkerError(f"repository worker exited during startup with code {process.returncode}")
            runtime = cls._load_runtime(runtime_path)
            if runtime is not None:
                client = cls(runtime_path, runtime, desired_priority=priority)
                try:
                    response = client._rpc({"command": "ping"})
                except Exception as exc:
                    last_error = exc
                else:
                    client._status = response["status"]
                    return client
            time.sleep(0.05)

        process.terminate()
        raise WorkerError("repository worker did not become ready before the startup deadline") from last_error

    @staticmethod
    def _load_runtime(path: Path) -> RuntimeInfo | None:
        try:
            return RuntimeInfo.load(path)
        except FileNotFoundError, OSError, ValueError, KeyError:
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if os.name == "nt":
            # os.kill(pid, 0) calls TerminateProcess on Windows: never use it
            # for liveness checks. Denied process access is treated as alive,
            # avoiding a second writer to an active repository.
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: PID absent
            try:
                # WAIT_OBJECT_0 means exited; WAIT_TIMEOUT means still active.
                # Treat WAIT_FAILED as alive to preserve single-writer safety.
                return kernel32.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel32.CloseHandle(handle)

        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    @classmethod
    def _wait_for_exit(cls, pid: int, runtime_path: Path) -> None:
        deadline = time.monotonic() + cls.START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if not cls._pid_alive(pid):
                runtime_path.unlink(missing_ok=True)
                return
            time.sleep(0.05)
        raise WorkerError(f"repository worker pid {pid} did not exit cleanly")

    def _rpc(self, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        request = dict(payload)
        request["protocol"] = PROTOCOL_VERSION
        request["token"] = self.runtime.token
        rpc_timeout = self.RPC_TIMEOUT_SECONDS if timeout is None else timeout
        try:
            with socket.create_connection(
                ("127.0.0.1", self.runtime.port),
                timeout=rpc_timeout,
            ) as sock:
                sock.settimeout(rpc_timeout)
                send_message(sock, request)
                response = receive_message(sock)
        except TimeoutError as exc:
            raise WorkerTimeout("repository worker RPC timed out") from exc
        except (OSError, ValueError, ConnectionError) as exc:
            raise WorkerError(f"repository worker RPC failed: {exc}") from exc

        if not response.get("ok", False):
            raise WorkerRejected(str(response.get("error", "repository worker rejected the request")))
        return response

    @property
    def status(self) -> dict[str, Any]:
        return self._status

    @property
    def busy(self) -> bool:
        return (
            bool(self._status.get("job_occupied", False))
            or self._status.get("job", {}).get("state") in ("running", "cancel_requested")
            or int(self._status.get("pending_results", 0)) > 0
            or bool(self._status.get("settings_deferred", False))
        )

    @property
    def current_request_id(self) -> str | None:
        value = self._status.get("current_request_id")
        return value if isinstance(value, str) and value else None

    @property
    def effective_priority(self) -> str:
        return self.runtime.priority

    @property
    def priority_deferred(self) -> bool:
        return self.runtime.priority != self.desired_priority

    @property
    def upgrade_deferred(self) -> bool:
        return self.runtime.version != package_version()

    def configure(self, settings: dict[str, Any]) -> dict[str, Any]:
        response = self._rpc(
            {"command": "configure", "settings": settings},
            timeout=self.CONFIGURE_TIMEOUT_SECONDS,
        )
        self._status = response["status"]
        return response.get("recovery", {})

    def refresh(self) -> dict[str, Any]:
        response = self._rpc({"command": "status"})
        self._status = response["status"]
        return self._status

    def start_maintenance(
        self,
        mode: str,
        *,
        request_id: str | None = None,
        timeout: float | None = None,
    ) -> bool:
        request: dict[str, Any] = {"command": "start_maintenance", "mode": mode}
        if request_id is not None:
            request["request_id"] = request_id
        response = self._rpc(request, timeout=timeout)
        self._status = response["status"]
        return bool(response["accepted"])

    def plan_mutation(self, operation: str, snapshot: str) -> dict[str, Any]:
        return self._rpc({"command": "plan_mutation", "operation": operation, "snapshot": snapshot})["plan"]

    def _start_management(self, payload: dict[str, Any]) -> bool:
        # On timeout, retry only the same immutable request ID. The worker
        # deduplicates accepted requests even after their result was ACKed.
        request_id = uuid.uuid4().hex
        request = {**payload, "request_id": request_id}
        for attempt in range(2):
            try:
                response = self._rpc(request)
            except WorkerRejected:
                raise
            except WorkerError as exc:
                if attempt == 0:
                    continue
                raise WorkerRequestIndeterminate(
                    f"Management request {request_id} has an unknown outcome. "
                    "Check /backup status and server logs before issuing a new request."
                ) from exc
            self._status = response["status"]
            return bool(response["accepted"])
        raise AssertionError("unreachable management retry loop")

    def start_mutation(self, operation: str, snapshot: str, *, expected_generation: int) -> bool:
        return self._start_management(
            {
                "command": "start_mutation",
                "operation": operation,
                "snapshot": snapshot,
                "expected_generation": expected_generation,
            }
        )

    def start_export(self, snapshot: str) -> bool:
        return self._start_management({"command": "start_export", "snapshot": snapshot})

    def start_pre_capture(
        self,
        *,
        required_bytes: int = 0,
        timeout: float | None = None,
    ) -> bool:
        response = self._rpc(
            {"command": "start_pre_capture", "required_bytes": required_bytes},
            timeout=timeout,
        )
        self._status = response["status"]
        return bool(response["accepted"])

    def start_verify(self, *, deep: bool) -> bool:
        response = self._rpc({"command": "start_verify", "deep": deep})
        accepted = bool(response["accepted"])
        if accepted:
            self._status = response["status"]
        return accepted

    def poll(self) -> tuple[int, dict[str, Any]] | None:
        response = self._rpc({"command": "poll"}, timeout=self.POLL_TIMEOUT_SECONDS)
        self._status = response["status"]
        envelope = response.get("result")
        if not isinstance(envelope, dict):
            return None
        result_id = int(envelope["id"])
        payload = envelope.get("payload")
        if not isinstance(payload, dict):
            raise WorkerError("repository worker returned an invalid result payload")
        return result_id, payload

    def ack_result(self, result_id: int) -> bool:
        response = self._rpc(
            {"command": "ack_result", "result_id": result_id},
            timeout=self.ACK_TIMEOUT_SECONDS,
        )
        self._status = response["status"]
        return bool(response["accepted"])

    def cancel(self) -> bool:
        response = self._rpc({"command": "cancel"})
        self._status = response["status"]
        return bool(response["accepted"])

    def list_snapshots(self) -> dict[str, Any]:
        return self._rpc({"command": "list_snapshots"})["repository"]

    def free_space_allows(self, additional_bytes: int = 0) -> bool:
        response = self._rpc({"command": "free_space_allows", "additional_bytes": additional_bytes})
        return bool(response["allowed"])

    def shutdown(self) -> None:
        self._rpc({"command": "shutdown"})

    def detach(self) -> None:
        # The worker intentionally survives plugin reloads. The next plugin
        # instance adopts the same runtime through worker-runtime.json.
        return None
