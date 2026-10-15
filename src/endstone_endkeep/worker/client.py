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

from .process import process_identity
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
        self._controller_generation: int | None = None

    @classmethod
    def connect_or_start(cls, storage_root: Path, *, priority: str) -> RepositoryWorkerClient:
        storage_root.mkdir(parents=True, exist_ok=True)
        runtime_path = storage_root / "worker-runtime.json"
        existing = cls._load_runtime(runtime_path)
        if existing is not None:
            if Path(existing.storage_root).resolve() != storage_root.resolve():
                raise WorkerError("repository worker runtime points at a different storage root")
            if existing.protocol != PROTOCOL_VERSION and cls._runtime_alive(existing):
                raise WorkerError(
                    f"repository worker protocol mismatch: {existing.protocol} != {PROTOCOL_VERSION}; "
                    "the previous worker must finish before starting another"
                )
            if existing.protocol == PROTOCOL_VERSION:
                client = cls(runtime_path, existing, desired_priority=priority)
                last_error: Exception | None = None
                for attempt in range(4):
                    try:
                        response = client._rpc({"command": "ping"}, timeout=1.0)
                    except WorkerError as exc:
                        last_error = exc
                        if not cls._runtime_alive(existing):
                            break
                        if attempt < 3:
                            time.sleep(0.2)
                        continue
                    status = response["status"]
                    worker = status.get("worker", {})
                    if int(worker.get("pid", 0)) != existing.pid:
                        raise WorkerError("repository runtime PID does not match the responding worker")
                    if existing.process_start and process_identity(existing.pid) != existing.process_start:
                        raise WorkerError("repository runtime process identity changed during reconnect")
                    client._status = status
                    if "controller_start" in worker:
                        identity = process_identity(os.getpid())
                        if identity is None:
                            raise WorkerError("cannot identify the current controller process")
                        try:
                            response = client._rpc(
                                {
                                    "command": "attach",
                                    "controller_pid": os.getpid(),
                                    "controller_start": identity,
                                    "controller_generation": worker.get("controller_generation"),
                                },
                                timeout=1.0,
                            )
                        except WorkerError as exc:
                            if isinstance(exc, WorkerRejected):
                                if str(exc) == "repository worker is exiting":
                                    cls._wait_for_exit(existing.pid, runtime_path, process_start=existing.process_start)
                                    break
                                if str(exc) != "repository worker controller ownership changed":
                                    raise
                            last_error = exc
                            if not cls._runtime_alive(existing):
                                break
                            if attempt < 3:
                                time.sleep(0.2)
                            continue
                        client._status = response["status"]
                        client._controller_generation = client._status["worker"].get("controller_generation")
                    needs_restart = existing.priority != priority or existing.version != package_version()
                    if needs_restart and not client.busy:
                        client.shutdown()
                        cls._wait_for_exit(existing.pid, runtime_path, process_start=existing.process_start)
                        break
                    return client
                else:
                    raise WorkerError(
                        f"repository worker pid {existing.pid} remains unresponsive after four RPC attempts; "
                        f"identity={existing.process_start or 'legacy/unknown'}, last_error={last_error}; "
                        "refusing concurrent repository access"
                    ) from last_error
                if cls._runtime_alive(existing):
                    raise WorkerError(
                        f"repository worker pid {existing.pid} is still active after reconnect attempts; "
                        f"last_error={last_error}; refusing a second writer"
                    ) from last_error

        return cls._start(storage_root, runtime_path, priority)

    @classmethod
    def _runtime_alive(cls, runtime: RuntimeInfo) -> bool:
        observed = process_identity(runtime.pid)
        if runtime.process_start and observed is not None:
            return observed == runtime.process_start
        if runtime.process_start and sys.platform.startswith("linux"):
            return False
        return cls._pid_alive(runtime.pid)

    @classmethod
    def _start(cls, storage_root: Path, runtime_path: Path, priority: str) -> RepositoryWorkerClient:
        identity = process_identity(os.getpid())
        launch_id = uuid.uuid4().hex
        if identity is None:
            raise WorkerError("cannot identify the current controller process")
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
            "--controller-pid",
            str(os.getpid()),
            "--controller-start",
            identity,
            "--launch-id",
            launch_id,
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

        if os.name != "nt":
            threading.Thread(
                target=process.wait,
                name=f"endkeep-worker-reaper-{process.pid}",
                daemon=True,
            ).start()

        deadline = time.monotonic() + cls.START_TIMEOUT_SECONDS
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            runtime = cls._load_runtime(runtime_path)
            if process.poll() is not None and (runtime is None or runtime.launch_id != launch_id):
                raise WorkerError(f"repository worker launcher exited during startup with code {process.returncode}")
            if runtime is not None and runtime.launch_id == launch_id:
                if runtime.process_start and process_identity(runtime.pid) != runtime.process_start:
                    raise WorkerError("newly launched worker process identity cannot be verified")
                client = cls(runtime_path, runtime, desired_priority=priority)
                try:
                    response = client._rpc({"command": "ping"})
                except Exception as exc:
                    last_error = exc
                else:
                    client._status = response["status"]
                    client._controller_generation = client._status["worker"].get("controller_generation")
                    return client
            time.sleep(0.05)

        process.terminate()
        try:
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-2500:]
        except OSError:
            detail = "<worker log unavailable>"
        observed = cls._load_runtime(runtime_path)
        raise WorkerError(
            f"repository worker did not become ready before the startup deadline; "
            f"child_pid={process.pid} runtime_exists={runtime_path.exists()} "
            f"runtime_pid={observed.pid if observed else None} "
            f"last_rpc_error={last_error!r}; worker log tail: {detail}"
        ) from last_error

    @staticmethod
    def _load_runtime(path: Path) -> RuntimeInfo | None:
        try:
            return RuntimeInfo.load(path)
        except (FileNotFoundError, OSError, ValueError, KeyError):
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
    def _wait_for_exit(cls, pid: int, runtime_path: Path, *, process_start: str = "") -> None:
        deadline = time.monotonic() + cls.START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            observed = process_identity(pid)
            if (
                (process_start and observed is not None and observed != process_start)
                or (process_start and observed is None and sys.platform.startswith("linux"))
                or not cls._pid_alive(pid)
            ):
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
        # A reload can reclaim the worker within the handoff window; the worker
        # also watches the BDS process identity to detect an actual server exit.
        try:
            self._rpc({"command": "detach", "controller_generation": self._controller_generation}, timeout=0.5)
        except WorkerRejected as exc:
            if "unknown worker command" not in str(exc):
                raise
