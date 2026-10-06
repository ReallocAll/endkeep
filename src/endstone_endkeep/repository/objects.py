from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, TypeVar

import zstandard as zstd

_T = TypeVar("_T")


@dataclass(frozen=True)
class ObjectMetadata:
    logical_sha256: str
    compressed_sha256: str
    logical_bytes: int
    compressed_bytes: int
    codec: str
    compression_level: int

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> ObjectMetadata:
        return cls(
            logical_sha256=str(raw["logical_sha256"]),
            compressed_sha256=str(raw["compressed_sha256"]),
            logical_bytes=int(raw["logical_bytes"]),
            compressed_bytes=int(raw["compressed_bytes"]),
            codec=str(raw["codec"]),
            compression_level=int(raw["compression_level"]),
        )


class ObjectStoreError(RuntimeError):
    """Raised when immutable logical-object storage violates an integrity invariant."""


class _HashingSink:
    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.hasher = hashlib.sha256()
        self.bytes = 0

    def write(self, data: bytes) -> int:
        written = self.stream.write(data)
        if written is None:
            written = len(data)
        if written != len(data):
            raise OSError("short write to compressed object")
        self.hasher.update(data)
        self.bytes += len(data)
        return written

    def flush(self) -> None:
        self.stream.flush()

    def close(self) -> None:
        # zstandard may close its destination wrapper. The real file descriptor is
        # owned by ObjectWriter and must remain open for fsync.
        self.flush()

    def writable(self) -> bool:
        return True


class ObjectWriter:
    def __init__(self, path: Path, *, level: int, threads: int) -> None:
        self.path = path
        self.level = level
        self.threads = threads
        self._file = path.open("xb")
        self._compressed_sink = _HashingSink(self._file)
        self._compressor = zstd.ZstdCompressor(level=level, threads=threads).stream_writer(
            self._compressed_sink,
            closefd=False,
        )
        self._logical_hasher = hashlib.sha256()
        self._logical_bytes = 0
        self._finished = False

    def write(self, data: bytes) -> int:
        if self._finished:
            raise ValueError("cannot write to a finished object")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("logical object writes must be bytes-like")
        raw = bytes(data)
        self._logical_hasher.update(raw)
        self._logical_bytes += len(raw)
        written = self._compressor.write(raw)
        if written is None:
            return len(raw)
        return written

    def finish(self) -> ObjectMetadata:
        if self._finished:
            raise ValueError("object already finished")
        self._compressor.close()
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()
        self._finished = True
        return ObjectMetadata(
            logical_sha256=self._logical_hasher.hexdigest(),
            compressed_sha256=self._compressed_sink.hasher.hexdigest(),
            logical_bytes=self._logical_bytes,
            compressed_bytes=self._compressed_sink.bytes,
            codec="zstd",
            compression_level=self.level,
        )

    def abort(self) -> None:
        if not self._finished:
            try:
                self._compressor.close()
            except Exception:
                pass
            try:
                self._file.close()
            except Exception:
                pass
            self._finished = True
        self.path.unlink(missing_ok=True)


class ObjectStore:
    def __init__(
        self,
        repo_root: Path,
        *,
        compression_level: int,
        compression_threads: int,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.objects_root = repo_root / "objects"
        self.incoming_root = repo_root / ".incoming"
        self.compression_level = compression_level
        self.compression_threads = compression_threads
        self.fault_hook = fault_hook or (lambda _point: None)

    def prepare(self) -> None:
        self.objects_root.mkdir(parents=True, exist_ok=True)
        self.incoming_root.mkdir(parents=True, exist_ok=True)

    def create(self, builder: Callable[[BinaryIO], _T]) -> tuple[ObjectMetadata, _T]:
        """Stream one canonical logical object, verify it, then publish it immutably."""

        self.prepare()
        incoming = self.incoming_root / f"object-{uuid.uuid4().hex}.part"
        writer = ObjectWriter(
            incoming,
            level=self.compression_level,
            threads=self.compression_threads,
        )
        try:
            result = builder(writer)
            metadata = writer.finish()
            self.fault_hook("after_object_fsync")
            self.verify_path(incoming, metadata)

            target = self.path_for(metadata.logical_sha256)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                existing = self._compressed_digest(target)
                if (
                    existing[0] != metadata.compressed_sha256
                    or existing[1] != metadata.compressed_bytes
                ):
                    raise ObjectStoreError(
                        "logical object already exists with different compressed bytes; "
                        "refusing to overwrite immutable object"
                    )
                incoming.unlink()
            else:
                os.rename(incoming, target)
                self.fault_hook("after_object_rename")
                self._fsync_directory(target.parent)
                self.fault_hook("after_object_dir_fsync")
            return metadata, result
        except Exception:
            writer.abort()
            raise

    def path_for(self, logical_sha256: str) -> Path:
        if len(logical_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in logical_sha256):
            raise ObjectStoreError(f"invalid logical SHA256: {logical_sha256!r}")
        return self.objects_root / logical_sha256[:2] / f"{logical_sha256}.zst"

    @contextmanager
    def open_logical(self, metadata: ObjectMetadata) -> Generator[BinaryIO]:
        if metadata.codec != "zstd":
            raise ObjectStoreError(f"unsupported object codec: {metadata.codec}")
        path = self.path_for(metadata.logical_sha256)
        source = path.open("rb")
        reader = zstd.ZstdDecompressor().stream_reader(source, closefd=False)
        try:
            yield reader
        finally:
            reader.close()
            source.close()

    def verify(self, metadata: ObjectMetadata) -> None:
        self.verify_path(self.path_for(metadata.logical_sha256), metadata)

    @staticmethod
    def verify_path(path: Path, metadata: ObjectMetadata) -> None:
        compressed_hasher = hashlib.sha256()
        logical_hasher = hashlib.sha256()
        compressed_bytes = 0
        logical_bytes = 0

        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                compressed_hasher.update(chunk)
                compressed_bytes += len(chunk)

        with path.open("rb") as source:
            reader = zstd.ZstdDecompressor().stream_reader(source)
            try:
                while True:
                    chunk = reader.read(1024 * 1024)
                    if not chunk:
                        break
                    logical_hasher.update(chunk)
                    logical_bytes += len(chunk)
            finally:
                reader.close()

        actual = (
            logical_hasher.hexdigest(),
            compressed_hasher.hexdigest(),
            logical_bytes,
            compressed_bytes,
        )
        expected = (
            metadata.logical_sha256,
            metadata.compressed_sha256,
            metadata.logical_bytes,
            metadata.compressed_bytes,
        )
        if actual != expected:
            raise ObjectStoreError(f"object verification failed for {path}")

    @staticmethod
    def _compressed_digest(path: Path) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                size += len(chunk)
        return hasher.hexdigest(), size

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
