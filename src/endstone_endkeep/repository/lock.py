from __future__ import annotations

import os
from pathlib import Path


class RepositoryLockError(RuntimeError):
    """Raised when another process/thread already owns the repository writer lock."""


class RepositoryLock:
    def __init__(self, repo_root: Path, *, blocking: bool = False) -> None:
        self.repo_root = repo_root
        self.path = repo_root / "LOCK"
        self.blocking = blocking
        self._fd: int | None = None

    def __enter__(self) -> RepositoryLock:
        self.repo_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "posix":
                import fcntl

                flags = fcntl.LOCK_EX
                if not self.blocking:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(fd, flags)
                except BlockingIOError as exc:
                    raise RepositoryLockError("repository is locked by another writer") from exc
            else:
                import msvcrt

                mode = msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(fd, mode, 1)
                except OSError as exc:
                    raise RepositoryLockError("repository is locked by another writer") from exc
        except Exception:
            os.close(fd)
            raise
        self._fd = fd
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        fd = self._fd
        self._fd = None
        if fd is None:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            else:
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)
