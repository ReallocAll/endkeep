from __future__ import annotations

import json
import os
import uuid
from pathlib import Path


class RepositoryHealth:
    """Durable fail-closed state after a repository verification failure."""

    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root
        self.path = storage_root / "repository-health.json"
        self.reason: str | None = None
        if self.path.is_symlink():
            raise RuntimeError("repository health marker must not be a symlink")
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("status") != "FAILED" or not isinstance(data.get("reason"), str):
                raise ValueError("invalid health marker schema")
            if not data["reason"]:
                raise ValueError("empty failure reason")
            self.reason = data["reason"]
        except (OSError, ValueError, TypeError, AttributeError):
            # Corrupt health metadata must NEVER be interpreted as healthy.
            self.reason = "repository health marker is unreadable or invalid"

    @property
    def failed(self) -> bool:
        return self.reason is not None

    def status(self) -> dict[str, str | None]:
        return {"status": "FAILED" if self.failed else "HEALTHY", "reason": self.reason}

    def require_healthy(self) -> None:
        if self.reason is not None:
            raise RuntimeError(
                f"REPOSITORY FAILED: {self.reason}; repository writes and GC are blocked "
                "until a successful /backup verify deep"
            )

    def fail(self, error: BaseException) -> None:
        reason = f"{type(error).__name__}: {error}"
        self.reason = reason
        self.storage_root.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_root / f".repository-health-{uuid.uuid4().hex}.part"
        payload = json.dumps({"status": "FAILED", "reason": reason}, sort_keys=True) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self._fsync_root()
        finally:
            temporary.unlink(missing_ok=True)

    def clear_after_deep_verify(self) -> None:
        if self.path.is_symlink():
            raise RuntimeError("repository health marker must not be a symlink")
        if self.path.exists():
            self.path.unlink()
            self._fsync_root()
        self.reason = None

    def _fsync_root(self) -> None:
        try:
            fd = os.open(self.storage_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            if os.name == "posix":
                raise
            return
        try:
            try:
                os.fsync(fd)
            except OSError:
                if os.name == "posix":
                    raise
        finally:
            os.close(fd)
