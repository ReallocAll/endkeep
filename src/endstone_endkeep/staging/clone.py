from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

_FICLONE = 0x40049409


class CloneError(RuntimeError):
    pass


def _copy_reflink_or_bytes(source: Path, destination: Path) -> None:
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    destination_fd = os.open(
        destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    try:
        cloned = False
        if os.name == "posix":
            try:
                import fcntl

                fcntl.ioctl(destination_fd, _FICLONE, source_fd)
                cloned = True
            except OSError:
                cloned = False

        if not cloned:
            while True:
                chunk = os.read(source_fd, 1024 * 1024)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError("short write while cloning raw snapshot")
                    view = view[written:]
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def clone_world(
    raw_snapshot_root: Path,
    world_name: str,
    work_root: Path,
    snapshot_id: str,
) -> Path:
    source = raw_snapshot_root / world_name
    if not source.is_dir():
        raise CloneError(f"raw world directory is missing: {source}")

    job_root = work_root / snapshot_id
    destination = job_root / world_name
    if job_root.exists():
        raise CloneError(f"processing clone already exists: {job_root}")
    destination.mkdir(parents=True)

    try:
        for current, dirnames, filenames in os.walk(source, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(source)
            destination_dir = destination / relative
            destination_dir.mkdir(parents=True, exist_ok=True)

            for dirname in dirnames:
                child = current_path / dirname
                info = child.lstat()
                if stat.S_ISLNK(info.st_mode) or (os.name == "nt" and child.is_junction()):
                    raise CloneError(f"raw snapshot contains directory symlink: {child}")

            for filename in filenames:
                source_file = current_path / filename
                info = source_file.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise CloneError(f"raw snapshot contains non-regular file: {source_file}")
                _copy_reflink_or_bytes(source_file, destination_dir / filename)
        return destination
    except Exception:
        shutil.rmtree(job_root, ignore_errors=True)
        raise


def remove_processing_clone(work_root: Path, snapshot_id: str) -> None:
    shutil.rmtree(work_root / snapshot_id, ignore_errors=True)
