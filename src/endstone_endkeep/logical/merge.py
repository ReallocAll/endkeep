from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable, Iterator

from .format import DeltaOperation, encode_uvarint


class MergeError(ValueError):
    """Raised when a DELTA cannot be validly applied to its parent state."""


_SENTINEL = object()


def apply_delta(
    state: Iterable[tuple[bytes, bytes]],
    delta: Iterable[DeltaOperation],
) -> Iterator[tuple[bytes, bytes]]:
    state_it = iter(state)
    delta_it = iter(delta)
    state_item = next(state_it, _SENTINEL)
    operation = next(delta_it, _SENTINEL)

    while state_item is not _SENTINEL or operation is not _SENTINEL:
        if operation is _SENTINEL:
            yield state_item
            state_item = next(state_it, _SENTINEL)
            continue
        if state_item is _SENTINEL:
            if operation.kind == "delete":
                raise MergeError(f"DELTA deletes absent key {operation.key!r}")
            assert operation.value is not None
            yield operation.key, operation.value
            operation = next(delta_it, _SENTINEL)
            continue

        state_key, state_value = state_item
        if state_key < operation.key:
            yield state_key, state_value
            state_item = next(state_it, _SENTINEL)
        elif operation.key < state_key:
            if operation.kind == "delete":
                raise MergeError(f"DELTA deletes absent key {operation.key!r}")
            assert operation.value is not None
            yield operation.key, operation.value
            operation = next(delta_it, _SENTINEL)
        else:
            if operation.kind == "put":
                assert operation.value is not None
                yield operation.key, operation.value
            state_item = next(state_it, _SENTINEL)
            operation = next(delta_it, _SENTINEL)


@dataclass(frozen=True)
class StateStats:
    records: int
    value_bytes: int
    sha256: str


def hash_state(state: Iterable[tuple[bytes, bytes]]) -> StateStats:
    hasher = hashlib.sha256()
    records = 0
    value_bytes = 0
    previous: bytes | None = None
    for key, value in state:
        if previous is not None and key <= previous:
            raise MergeError("state is not strictly increasing")
        hasher.update(encode_uvarint(len(key)))
        hasher.update(key)
        hasher.update(encode_uvarint(len(value)))
        hasher.update(value)
        previous = key
        records += 1
        value_bytes += len(value)
    return StateStats(records, value_bytes, hasher.hexdigest())
