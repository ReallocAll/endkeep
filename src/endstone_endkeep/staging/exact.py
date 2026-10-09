from __future__ import annotations

import errno
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from threading import Event

from endstone_endkeep.bds.model import SnapshotManifest


class ExactStageError(RuntimeError):
    """Raised when an authoritative save-query entry cannot be staged exactly."""


@dataclass(frozen=True)
class StageResult:
    files: int
    bytes_copied: int
    elapsed_seconds: float


def _check_cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ExactStageError("staging cancelled")


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    target = root.joinpath(*relative.parts)
    root_resolved = root.resolve()
    target_parent = target.parent
    target_parent.mkdir(parents=True, exist_ok=True)
    try:
        parent_resolved = target_parent.resolve(strict=True)
    except OSError as exc:
        raise ExactStageError(f"cannot resolve destination parent for {relative}: {exc}") from exc
    if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
        raise ExactStageError(f"destination escapes staging root: {relative}")
    return target


def _open_source_linux(root: Path, relative: PurePosixPath) -> int:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(root, os.O_RDONLY | directory)
    current_fd = root_fd
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=current_fd)
            if current_fd != root_fd:
                os.close(current_fd)
            current_fd = next_fd
        fd = os.open(relative.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
    except Exception:
        if current_fd != root_fd:
            os.close(current_fd)
        os.close(root_fd)
        raise
    if current_fd != root_fd:
        os.close(current_fd)
    os.close(root_fd)
    return fd


def _open_source_portable(root: Path, relative: PurePosixPath) -> int:
    candidate = root.joinpath(*relative.parts)
    root_resolved = root.resolve(strict=True)
    try:
        current = root_resolved
        for component in relative.parts:
            current = current / component
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or (os.name == "nt" and current.is_junction()):
                raise ExactStageError(f"source path contains a symlink: {relative}")
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ExactStageError(f"cannot inspect source {relative}: {exc}") from exc
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ExactStageError(f"source escapes world root: {relative}")
    return os.open(resolved, os.O_RDONLY)


def _open_source(root: Path, relative: PurePosixPath) -> int:
    if not relative.parts or relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise ExactStageError(f"unsafe source path: {relative}")
    try:
        if os.name == "posix" and os.supports_dir_fd:
            return _open_source_linux(root, relative)
    except (NotImplementedError, OSError) as exc:
        if isinstance(exc, OSError) and exc.errno not in {
            errno.EINVAL,
            errno.ENOSYS,
            errno.ENOTSUP,
            errno.EOPNOTSUPP,
        }:
            raise ExactStageError(f"cannot securely open source {relative}: {exc}") from exc
    return _open_source_portable(root, relative)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise ExactStageError("short write while staging snapshot")
        view = view[written:]


def _copy_bounded(src_fd: int, dst_fd: int, size: int, cancel: Event | None) -> None:
    remaining = size
    use_copy_file_range = hasattr(os, "copy_file_range")
    while remaining:
        _check_cancelled(cancel)
        chunk = min(remaining, 8 * 1024 * 1024)
        if use_copy_file_range:
            try:
                copied = os.copy_file_range(src_fd, dst_fd, chunk)
            except OSError as exc:
                if exc.errno in {
                    errno.EXDEV,
                    errno.EINVAL,
                    errno.ENOSYS,
                    errno.ENOTSUP,
                    errno.EOPNOTSUPP,
                }:
                    use_copy_file_range = False
                    continue
                raise
            if copied == 0:
                raise ExactStageError(f"source ended with {remaining} snapshot bytes remaining")
            remaining -= copied
            continue

        data = os.read(src_fd, min(chunk, 1024 * 1024))
        if not data:
            raise ExactStageError(f"source ended with {remaining} snapshot bytes remaining")
        if len(data) > remaining:
            data = data[:remaining]
        _write_all(dst_fd, data)
        remaining -= len(data)


def stage_manifest(
    source_root: Path,
    destination_root: Path,
    manifest: SnapshotManifest,
    *,
    cancel: Event | None = None,
) -> StageResult:
    """Copy exactly the save-query byte limits and nothing else."""

    started = time.monotonic()
    destination_root.mkdir(parents=True, exist_ok=False)
    copied_bytes = 0

    try:
        for entry in manifest.entries:
            _check_cancelled(cancel)
            target = _safe_destination(destination_root, entry.path)
            src_fd = _open_source(source_root, entry.path)
            try:
                source_info = os.fstat(src_fd)
                if not stat.S_ISREG(source_info.st_mode):
                    raise ExactStageError(f"source is not a regular file: {entry.path}")
                if source_info.st_size < entry.snapshot_bytes:
                    raise ExactStageError(
                        f"source shorter than query limit for {entry.path}: "
                        f"{source_info.st_size} < {entry.snapshot_bytes}"
                    )

                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                dst_fd = os.open(target, flags, 0o600)
                try:
                    _copy_bounded(src_fd, dst_fd, entry.snapshot_bytes, cancel)
                finally:
                    os.close(dst_fd)
            finally:
                os.close(src_fd)

            final_size = target.stat().st_size
            if final_size != entry.snapshot_bytes:
                raise ExactStageError(
                    f"destination size mismatch for {entry.path}: {final_size} != {entry.snapshot_bytes}"
                )
            copied_bytes += final_size

        return StageResult(
            files=manifest.file_count,
            bytes_copied=copied_bytes,
            elapsed_seconds=time.monotonic() - started,
        )
    except Exception:
        raise
