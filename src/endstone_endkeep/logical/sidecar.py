from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from endstone_endkeep.bds.model import SnapshotEntry

from .format import LogicalFormatError, _read_exact, _read_uvarint, encode_uvarint


SIDECAR_MAGIC = b"ENDKEEP\x00SIDECAR\x01"


@dataclass(frozen=True)
class SidecarStats:
    files: int
    bytes: int


def write_sidecar(
    stream: BinaryIO,
    raw_snapshot_root: Path,
    entries: Iterable[SnapshotEntry],
) -> SidecarStats:
    stream.write(SIDECAR_MAGIC)
    ordered = sorted(entries, key=lambda entry: entry.path.as_posix())
    previous: str | None = None
    files = 0
    total_bytes = 0

    for entry in ordered:
        path_text = entry.path.as_posix()
        if previous is not None and path_text <= previous:
            raise LogicalFormatError("SIDECAR paths must be strictly increasing")
        encoded_path = path_text.encode("utf-8")
        stream.write(encode_uvarint(len(encoded_path)))
        stream.write(encoded_path)
        stream.write(encode_uvarint(entry.snapshot_bytes))

        path = raw_snapshot_root.joinpath(*entry.path.parts)
        with path.open("rb") as source:
            remaining = entry.snapshot_bytes
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise LogicalFormatError(f"SIDECAR source is short: {entry.path}")
                stream.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                # Raw files are already exact-byte staged. Extra bytes indicate the
                # staging invariant was violated rather than something to archive.
                raise LogicalFormatError(f"SIDECAR raw file has unexpected trailing bytes: {entry.path}")

        previous = path_text
        files += 1
        total_bytes += entry.snapshot_bytes

    return SidecarStats(files, total_bytes)


def extract_sidecar(
    stream: BinaryIO,
    destination_root: Path,
    *,
    strip_prefix: str | None = None,
) -> SidecarStats:
    if _read_exact(stream, len(SIDECAR_MAGIC)) != SIDECAR_MAGIC:
        raise LogicalFormatError("invalid SIDECAR magic/version")

    root = destination_root.resolve()
    previous: str | None = None
    files = 0
    total_bytes = 0

    while True:
        first = stream.read(1)
        if not first:
            break
        path_len = _read_uvarint(stream, first=first)
        raw_path = _read_exact(stream, path_len)
        try:
            path_text = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LogicalFormatError("SIDECAR contains a non-UTF-8 path") from exc
        path = PurePosixPath(path_text)
        if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
            raise LogicalFormatError(f"unsafe SIDECAR path: {path_text!r}")
        if previous is not None and path_text <= previous:
            raise LogicalFormatError("SIDECAR paths are not strictly increasing")

        if strip_prefix is not None:
            if not path.parts or path.parts[0] != strip_prefix:
                raise LogicalFormatError(
                    f"SIDECAR path {path_text!r} is outside expected world prefix {strip_prefix!r}"
                )
            relative_parts = path.parts[1:]
            if not relative_parts:
                raise LogicalFormatError(f"SIDECAR path names the world directory itself: {path_text!r}")
        else:
            relative_parts = path.parts

        size = _read_uvarint(stream)
        target = destination_root.joinpath(*relative_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        parent = target.parent.resolve()
        if parent != root and root not in parent.parents:
            raise LogicalFormatError(f"SIDECAR path escapes restore root: {path_text!r}")

        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            remaining = size
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise LogicalFormatError(f"truncated SIDECAR file: {path_text!r}")
                view = memoryview(chunk)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError(f"short write restoring SIDECAR file: {path_text!r}")
                    view = view[written:]
                remaining -= len(chunk)
            os.fsync(fd)
        finally:
            os.close(fd)

        previous = path_text
        files += 1
        total_bytes += size

    return SidecarStats(files, total_bytes)
