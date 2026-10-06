from __future__ import annotations

from io import BytesIO

import pytest

from endstone_endkeep.logical.diff import DiffStats, semantic_diff
from endstone_endkeep.logical.format import (
    DeltaOperation,
    LogicalFormatError,
    encode_uvarint,
    iter_base,
    iter_delta,
    write_base,
    write_delta,
)
from endstone_endkeep.logical.merge import apply_delta


def test_varint_boundaries() -> None:
    assert encode_uvarint(0) == b"\x00"
    assert encode_uvarint(127) == b"\x7f"
    assert encode_uvarint(128) == b"\x80\x01"
    assert encode_uvarint(16384) == b"\x80\x80\x01"


def test_base_roundtrip_and_sorted_invariant() -> None:
    records = [(b"a", b"1"), (b"b", b""), (b"c", b"three")]
    stream = BytesIO()
    stats = write_base(stream, records)
    assert stats.records == 3
    assert stats.value_bytes == 6
    stream.seek(0)
    assert list(iter_base(stream)) == records

    with pytest.raises(LogicalFormatError):
        write_base(BytesIO(), [(b"b", b"1"), (b"a", b"2")])


def test_delta_roundtrip() -> None:
    operations = [
        DeltaOperation.delete(b"a"),
        DeltaOperation.put(b"b", b"2"),
        DeltaOperation.put(b"c", b"3"),
    ]
    stream = BytesIO()
    stats = write_delta(stream, operations)
    assert (stats.records, stats.puts, stats.deletes) == (3, 2, 1)
    stream.seek(0)
    assert list(iter_delta(stream)) == operations


def test_semantic_diff_and_merge() -> None:
    previous = [(b"a", b"1"), (b"b", b"2"), (b"d", b"4")]
    current = [(b"b", b"2"), (b"c", b"3"), (b"d", b"changed")]
    stats = DiffStats()
    delta = list(semantic_diff(previous, current, stats))

    assert stats.unchanged == 1
    assert stats.changed == 1
    assert stats.inserted == 1
    assert stats.deleted == 1
    assert list(apply_delta(previous, delta)) == current
