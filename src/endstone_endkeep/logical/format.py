from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import BinaryIO, Iterable, Iterator, Literal


BASE_MAGIC = b"ENDKEEP\x00BASE\x01"
DELTA_MAGIC = b"ENDKEEP\x00DELTA\x01"

DeleteKind = Literal["delete"]
PutKind = Literal["put"]


class LogicalFormatError(ValueError):
    """Raised when a canonical logical object is malformed or non-canonical."""


@dataclass(frozen=True)
class DeltaOperation:
    kind: DeleteKind | PutKind
    key: bytes
    value: bytes | None = None

    @classmethod
    def delete(cls, key: bytes) -> DeltaOperation:
        return cls("delete", key, None)

    @classmethod
    def put(cls, key: bytes, value: bytes) -> DeltaOperation:
        return cls("put", key, value)


@dataclass(frozen=True)
class BaseStats:
    records: int
    value_bytes: int
    state_sha256: str


@dataclass(frozen=True)
class DeltaStats:
    records: int
    puts: int
    deletes: int


def encode_uvarint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint cannot encode a negative value")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_uvarint(stream: BinaryIO, *, first: bytes | None = None) -> int:
    encoded = bytearray()
    shift = 0
    byte = first
    for _ in range(10):
        if byte is None:
            byte = stream.read(1)
        if not byte:
            raise LogicalFormatError("truncated varint")
        current = byte[0]
        encoded.append(current)
        value_part = current & 0x7F
        if shift >= 64 and value_part:
            raise LogicalFormatError("varint exceeds 64-bit range")
        shift_value = value_part << shift
        if not current & 0x80:
            value = sum((part & 0x7F) << (7 * index) for index, part in enumerate(encoded))
            if encode_uvarint(value) != bytes(encoded):
                raise LogicalFormatError("non-canonical varint encoding")
            return value
        shift += 7
        byte = None
    raise LogicalFormatError("varint is too long")


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = stream.read(size - len(chunks))
        if not chunk:
            raise LogicalFormatError(f"truncated logical object: wanted {size} bytes")
        chunks.extend(chunk)
    return bytes(chunks)


def _record_parts(key: bytes, value: bytes) -> tuple[bytes, bytes, bytes, bytes]:
    return encode_uvarint(len(key)), key, encode_uvarint(len(value)), value


def _update_state_hash(hasher, key: bytes, value: bytes) -> None:
    for part in _record_parts(key, value):
        hasher.update(part)


def write_base(stream: BinaryIO, records: Iterable[tuple[bytes, bytes]]) -> BaseStats:
    stream.write(BASE_MAGIC)
    previous: bytes | None = None
    count = 0
    value_bytes = 0
    state_hasher = hashlib.sha256()

    for key, value in records:
        if not isinstance(key, bytes) or not isinstance(value, bytes):
            raise TypeError("BASE keys and values must be bytes")
        if previous is not None and key <= previous:
            raise LogicalFormatError("BASE records must be in strictly increasing key order")
        parts = _record_parts(key, value)
        for part in parts:
            stream.write(part)
        _update_state_hash(state_hasher, key, value)
        previous = key
        count += 1
        value_bytes += len(value)

    return BaseStats(count, value_bytes, state_hasher.hexdigest())


def iter_base(stream: BinaryIO) -> Iterator[tuple[bytes, bytes]]:
    if _read_exact(stream, len(BASE_MAGIC)) != BASE_MAGIC:
        raise LogicalFormatError("invalid BASE magic/version")
    previous: bytes | None = None
    while True:
        first = stream.read(1)
        if not first:
            return
        key_len = _read_uvarint(stream, first=first)
        key = _read_exact(stream, key_len)
        value_len = _read_uvarint(stream)
        value = _read_exact(stream, value_len)
        if previous is not None and key <= previous:
            raise LogicalFormatError("BASE records are not strictly increasing")
        previous = key
        yield key, value


def write_delta(stream: BinaryIO, operations: Iterable[DeltaOperation]) -> DeltaStats:
    stream.write(DELTA_MAGIC)
    previous: bytes | None = None
    count = puts = deletes = 0

    for operation in operations:
        key = operation.key
        if not isinstance(key, bytes):
            raise TypeError("DELTA keys must be bytes")
        if previous is not None and key <= previous:
            raise LogicalFormatError("DELTA records must be in strictly increasing key order")

        if operation.kind == "delete":
            if operation.value is not None:
                raise LogicalFormatError("DELETE operation must not contain a value")
            stream.write(b"\x00")
            stream.write(encode_uvarint(len(key)))
            stream.write(key)
            deletes += 1
        elif operation.kind == "put":
            if not isinstance(operation.value, bytes):
                raise LogicalFormatError("PUT operation requires a bytes value")
            stream.write(b"\x01")
            for part in _record_parts(key, operation.value):
                stream.write(part)
            puts += 1
        else:
            raise LogicalFormatError(f"unknown DELTA operation: {operation.kind!r}")

        previous = key
        count += 1

    return DeltaStats(count, puts, deletes)


def iter_delta(stream: BinaryIO) -> Iterator[DeltaOperation]:
    if _read_exact(stream, len(DELTA_MAGIC)) != DELTA_MAGIC:
        raise LogicalFormatError("invalid DELTA magic/version")
    previous: bytes | None = None

    while True:
        opcode = stream.read(1)
        if not opcode:
            return
        if opcode not in (b"\x00", b"\x01"):
            raise LogicalFormatError(f"invalid DELTA opcode: {opcode.hex()}")

        key_len = _read_uvarint(stream)
        key = _read_exact(stream, key_len)
        if previous is not None and key <= previous:
            raise LogicalFormatError("DELTA records are not strictly increasing")

        if opcode == b"\x00":
            operation = DeltaOperation.delete(key)
        else:
            value_len = _read_uvarint(stream)
            operation = DeltaOperation.put(key, _read_exact(stream, value_len))

        previous = key
        yield operation
