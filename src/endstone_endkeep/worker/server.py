from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any

from endstone_endkeep.repository.lock import RepositoryLock
from endstone_endkeep.repository.maintenance import RepositoryService
from endstone_endkeep.repository.progress import ProgressTracker
from endstone_endkeep.repository.recovery import StartupRecovery

from .protocol import PROTOCOL_VERSION, RuntimeInfo, package_version, receive_message, send_message

_IDLE_EXIT_SECONDS = 300.0
_PRIORITY_NICE = {
    "conservative": 15,
    "background": 8,
    "balanced": 3,
    "throughput": 0,
}


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class WorkerApplication:
    def __init__(self, storage_root: Path, *, priority: str) -> None:
        self.storage_root = storage_root
        self.priority = priority
        self.tracker = ProgressTracker()
        self.service: RepositoryService | None = None
        self.settings: dict[str, Any] | None = None
        self.pending_settings: dict[str, Any] | None = None
        self.results: deque[dict[str, Any]] = deque()
        self._next_result_id = 1
        self._last_acked_result_id = 0
        self._active_request_id: str | None = None
        self.last_contact = time.monotonic()
        self.shutdown_requested = False
        self._lock = threading.RLock()
        self._stop_callback = lambda: None

    def set_stop_callback(self, callback) -> None:
        self._stop_callback = callback

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self.last_contact = time.monotonic()
            command = request.get("command")
            if command == "ping":
                return {"ok": True, "status": self._status_locked()}
            if command == "configure":
                return self._configure_locked(request.get("settings"))
            if command == "status":
                return {"ok": True, "status": self._status_locked(include_repository=True)}
            if command == "start_maintenance":
                if not self._ready_for_new_job_locked():
                    return {"ok": True, "accepted": False, "status": self._status_locked()}
                service = self._require_service()
                mode = str(request.get("mode"))
                if mode not in ("FULL", "LOGIC_ONLY"):
                    return {"ok": False, "error": f"invalid maintenance mode: {mode}"}
                request_id = request.get("request_id")
                if request_id is not None and (not isinstance(request_id, str) or not request_id):
                    return {"ok": False, "error": "maintenance request_id must be a non-empty string"}
                accepted = service.start_maintenance(mode)
                if accepted:
                    self._active_request_id = request_id
                return {"ok": True, "accepted": accepted, "status": self._status_locked()}
            if command == "start_pre_capture":
                if not self._ready_for_new_job_locked():
                    return {"ok": True, "accepted": False, "status": self._status_locked()}
                service = self._require_service()
                required_bytes = int(request.get("required_bytes", 0))
                if required_bytes < 0:
                    return {"ok": False, "error": "required_bytes cannot be negative"}
                accepted = service.start_pre_capture(required_bytes=required_bytes)
                return {"ok": True, "accepted": accepted, "status": self._status_locked()}
            if command == "start_verify":
                if not self._ready_for_new_job_locked():
                    return {"ok": True, "accepted": False, "status": self._status_locked()}
                service = self._require_service()
                accepted = service.start_verify(deep=bool(request.get("deep", False)))
                return {"ok": True, "accepted": accepted, "status": self._status_locked()}
            if command == "poll":
                result = self.results[0] if self.results else None
                return {"ok": True, "result": result, "status": self._status_locked()}
            if command == "ack_result":
                result_id = int(request.get("result_id", 0))
                if result_id <= self._last_acked_result_id:
                    accepted = True
                elif self.results and int(self.results[0]["id"]) == result_id:
                    self.results.popleft()
                    self._last_acked_result_id = result_id
                    accepted = True
                else:
                    accepted = False
                return {"ok": True, "accepted": accepted, "status": self._status_locked()}
            if command == "cancel":
                service = self._require_service()
                accepted = service.request_cancel()
                return {"ok": True, "accepted": accepted, "status": self._status_locked()}
            if command == "list_snapshots":
                service = self._require_service()
                manifest = service.manifests.load_current()
                if manifest is None:
                    repository = {"generation": None, "snapshots": []}
                else:
                    repository = {
                        "generation": manifest.generation,
                        "snapshots": [
                            {
                                "snapshot": node.snapshot,
                                "type": node.type,
                                "captured_at": node.captured_at,
                                "records": node.records,
                                "state_sha256": node.state_sha256,
                            }
                            for node in manifest.chain
                        ],
                    }
                return {"ok": True, "repository": repository}
            if command == "free_space_allows":
                service = self._require_service()
                additional_bytes = int(request.get("additional_bytes", 0))
                return {"ok": True, "allowed": service.queue.free_space_allows(additional_bytes)}
            if command == "shutdown":
                service = self.service
                if service is not None and service.busy:
                    return {"ok": False, "error": "repository worker is busy"}
                if self.results:
                    return {"ok": False, "error": "repository worker has unacknowledged results"}
                self.shutdown_requested = True
                return {"ok": True, "status": self._status_locked()}
            return {"ok": False, "error": f"unknown worker command: {command!r}"}

    def _configure_locked(self, raw_settings: Any) -> dict[str, Any]:
        if not isinstance(raw_settings, dict):
            return {"ok": False, "error": "worker settings must be an object"}
        settings = self._normalize_settings(raw_settings)

        if self.service is not None and self.settings == settings and self.pending_settings is None:
            return {"ok": True, "recovery": {}, "deferred": False, "status": self._status_locked()}
        if self.service is not None and self.service.busy:
            self.pending_settings = settings
            return {"ok": True, "recovery": {}, "deferred": True, "status": self._status_locked()}

        recovery = self._apply_settings_locked(settings)
        return {
            "ok": True,
            "recovery": recovery,
            "deferred": False,
            "status": self._status_locked(),
        }

    @staticmethod
    def _normalize_settings(raw_settings: dict[str, Any]) -> dict[str, int]:
        return {
            "compression_level": int(raw_settings["compression_level"]),
            "compression_threads": int(raw_settings["compression_threads"]),
            "max_pending": int(raw_settings["max_pending"]),
            "max_age_days": int(raw_settings["max_age_days"]),
            "min_free_space_gib": int(raw_settings["min_free_space_gib"]),
            "keep_days": int(raw_settings["keep_days"]),
            "keep_last": int(raw_settings["keep_last"]),
        }

    def _apply_settings_locked(self, settings: dict[str, int]) -> dict[str, Any]:
        if self.service is not None:
            self.service.close()

        service = RepositoryService(
            self.storage_root,
            compression_level=settings["compression_level"],
            compression_threads=settings["compression_threads"],
            max_pending=settings["max_pending"],
            max_age_days=settings["max_age_days"],
            min_free_space_gib=settings["min_free_space_gib"],
            keep_days=settings["keep_days"],
            keep_last=settings["keep_last"],
            tracker=self.tracker,
        )
        try:
            with RepositoryLock(service.repo_root):
                recovery = StartupRecovery(
                    self.storage_root,
                    service.manifests,
                    service.objects,
                ).run()
        except Exception:
            service.close()
            raise

        self.service = service
        self.settings = dict(settings)
        self.pending_settings = None
        return asdict(recovery)

    def _ready_for_new_job_locked(self) -> bool:
        service = self.service
        return service is not None and not service.busy and not self.results and self.pending_settings is None

    def _require_service(self) -> RepositoryService:
        if self.service is None:
            raise RuntimeError("repository worker has not been configured")
        return self.service

    def _status_locked(self, *, include_repository: bool = False) -> dict[str, Any]:
        service = self.service
        status = {
            "worker": {
                "pid": os.getpid(),
                "priority": self.priority,
                "alive": True,
            },
            "job": self.tracker.snapshot().to_dict(),
            "job_occupied": service is not None and service.busy,
            "current_request_id": self._current_request_id_locked(),
            "settings_deferred": self.pending_settings is not None,
            "pending_results": len(self.results),
        }
        if include_repository:
            status["repository"] = self._repository_status_locked()
        return status

    def _current_request_id_locked(self) -> str | None:
        if self._active_request_id is not None:
            return self._active_request_id
        if not self.results:
            return None
        payload = self.results[0].get("payload")
        if not isinstance(payload, dict):
            return None
        request_id = payload.get("request_id")
        return request_id if isinstance(request_id, str) and request_id else None

    def _repository_status_locked(self) -> dict[str, Any]:
        generation = None
        snapshots = 0
        raw_pending = None
        service = self.service
        if service is not None:
            try:
                manifest = service.manifests.load_current()
                if manifest is not None:
                    generation = manifest.generation
                    snapshots = len(manifest.chain)
            except Exception:
                pass
            try:
                raw_pending = len(service.queue.pending())
            except Exception:
                raw_pending = None
        return {
            "generation": generation,
            "snapshots": snapshots,
            "raw_pending": raw_pending,
        }

    def monitor(self) -> None:
        while True:
            time.sleep(0.1)
            with self._lock:
                service = self.service
                if service is not None:
                    try:
                        result = service.poll()
                    except Exception as exc:
                        payload: dict[str, Any] = {"kind": "error", "error": str(exc)}
                        if self._active_request_id is not None:
                            payload["request_id"] = self._active_request_id
                        self._queue_result(payload)
                        self._active_request_id = None
                    else:
                        if result is not None:
                            payload = asdict(result)
                            if self._active_request_id is not None:
                                payload["request_id"] = self._active_request_id
                            self._queue_result(payload)
                            self._active_request_id = None

                service = self.service
                if self.pending_settings is not None and service is not None and not service.busy and not self.results:
                    self._apply_settings_locked(self.pending_settings)
                    service = self.service

                busy = service is not None and service.busy
                idle_too_long = time.monotonic() - self.last_contact >= _IDLE_EXIT_SECONDS
                should_stop = self.shutdown_requested or (idle_too_long and not busy and not self.results)
            if should_stop:
                self._stop_callback()
                return

    def _queue_result(self, payload: dict[str, Any]) -> None:
        self.results.append(
            {
                "id": self._next_result_id,
                "payload": payload,
            }
        )
        self._next_result_id += 1

    def close(self) -> None:
        with self._lock:
            service = self.service
            self.service = None
        if service is not None:
            service.close()


class _RequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        app: WorkerApplication = self.server.app  # type: ignore[attr-defined]
        token: str = self.server.token  # type: ignore[attr-defined]
        try:
            request = receive_message(self.request)
            if request.get("protocol") != PROTOCOL_VERSION:
                response = {"ok": False, "error": "worker protocol mismatch"}
            elif not secrets.compare_digest(str(request.get("token", "")), token):
                response = {"ok": False, "error": "worker authentication failed"}
            else:
                response = app.handle(request)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        try:
            send_message(self.request, response)
        except OSError:
            # A deliberately short controller poll may time out and close the
            # loopback socket before this response is ready. Results themselves
            # remain queued until an explicit acknowledgement.
            pass


def _apply_priority(priority: str) -> None:
    if priority not in _PRIORITY_NICE:
        raise ValueError(f"invalid worker priority: {priority}")
    if os.name == "posix":
        nice = _PRIORITY_NICE[priority]
        if nice:
            os.nice(nice)

    if not sys.platform.startswith("linux"):
        return
    ionice = shutil.which("ionice")
    if ionice is None:
        return

    command: list[str] | None
    if priority == "conservative":
        command = [ionice, "-c", "3", "-p", str(os.getpid())]
    elif priority == "background":
        command = [ionice, "-c", "2", "-n", "7", "-p", str(os.getpid())]
    elif priority == "balanced":
        command = [ionice, "-c", "2", "-n", "4", "-p", str(os.getpid())]
    else:
        command = None

    if command is not None:
        subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def _write_runtime(path: Path, runtime: RuntimeInfo) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(runtime.to_dict(), indent=2, sort_keys=True) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    if os.name == "posix":
        os.chmod(path, 0o600)
    if os.name == "posix":
        fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _remove_runtime(path: Path, instance_id: str) -> None:
    try:
        current = RuntimeInfo.load(path)
    except Exception:
        return
    if current.instance_id != instance_id:
        return
    path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="EndKeep repository worker")
    parser.add_argument("--storage-root", required=True)
    parser.add_argument("--runtime-file", required=True)
    parser.add_argument(
        "--priority",
        required=True,
        choices=tuple(_PRIORITY_NICE),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    storage_root = Path(args.storage_root).resolve()
    runtime_file = Path(args.runtime_file).resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    _apply_priority(args.priority)

    token = secrets.token_hex(32)
    instance_id = uuid.uuid4().hex
    app = WorkerApplication(storage_root, priority=args.priority)

    with _ThreadingServer(("127.0.0.1", 0), _RequestHandler) as server:
        server.app = app  # type: ignore[attr-defined]
        server.token = token  # type: ignore[attr-defined]
        app.set_stop_callback(server.shutdown)
        runtime = RuntimeInfo(
            protocol=PROTOCOL_VERSION,
            version=package_version(),
            pid=os.getpid(),
            port=int(server.server_address[1]),
            token=token,
            instance_id=instance_id,
            storage_root=str(storage_root),
            priority=args.priority,
        )
        _write_runtime(runtime_file, runtime)

        monitor = threading.Thread(target=app.monitor, name="endkeep-worker-monitor", daemon=True)
        monitor.start()
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            app.close()
            _remove_runtime(runtime_file, instance_id)
    return 0
