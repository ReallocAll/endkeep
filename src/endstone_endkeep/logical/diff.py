from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from .format import DeltaOperation, encode_uvarint


@dataclass
class DiffStats:
    unchanged: int = 0
    changed: int = 0
    inserted: int = 0
    deleted: int = 0
    current_records: int = 0
    current_value_bytes: int = 0
    _state_hasher: object = field(default_factory=hashlib.sha256, repr=False)

    @property
    def delta_records(self) -> int:
        return self.changed + self.inserted + self.deleted

    @property
    def state_sha256(self) -> str:
        return self._state_hasher.hexdigest()  # type: ignore[attr-defined]

    def observe_current(self, key: bytes, value: bytes) -> None:
        hasher = self._state_hasher
        hasher.update(encode_uvarint(len(key)))  # type: ignore[attr-defined]
        hasher.update(key)  # type: ignore[attr-defined]
        hasher.update(encode_uvarint(len(value)))  # type: ignore[attr-defined]
        hasher.update(value)  # type: ignore[attr-defined]
        self.current_records += 1
        self.current_value_bytes += len(value)


_SENTINEL = object()


def semantic_diff(
    previous: Iterable[tuple[bytes, bytes]],
    current: Iterable[tuple[bytes, bytes]],
    stats: DiffStats,
) -> Iterator[DeltaOperation]:
    """Streaming sorted-key semantic diff from previous state to current state."""

    previous_it = iter(previous)
    current_it = iter(current)
    previous_item = next(previous_it, _SENTINEL)
    current_item = next(current_it, _SENTINEL)
    previous_key_seen: bytes | None = None
    current_key_seen: bytes | None = None

    while previous_item is not _SENTINEL or current_item is not _SENTINEL:
        if previous_item is not _SENTINEL:
            previous_key, previous_value = previous_item
            if previous_key_seen is not None and previous_key <= previous_key_seen:
                raise ValueError("previous state is not strictly sorted")
        if current_item is not _SENTINEL:
            current_key, current_value = current_item
            if current_key_seen is not None and current_key <= current_key_seen:
                raise ValueError("current state is not strictly sorted")

        if previous_item is _SENTINEL:
            stats.observe_current(current_key, current_value)
            stats.inserted += 1
            current_key_seen = current_key
            yield DeltaOperation.put(current_key, current_value)
            current_item = next(current_it, _SENTINEL)
            continue

        if current_item is _SENTINEL:
            stats.deleted += 1
            previous_key_seen = previous_key
            yield DeltaOperation.delete(previous_key)
            previous_item = next(previous_it, _SENTINEL)
            continue

        if previous_key < current_key:
            stats.deleted += 1
            previous_key_seen = previous_key
            yield DeltaOperation.delete(previous_key)
            previous_item = next(previous_it, _SENTINEL)
        elif current_key < previous_key:
            stats.observe_current(current_key, current_value)
            stats.inserted += 1
            current_key_seen = current_key
            yield DeltaOperation.put(current_key, current_value)
            current_item = next(current_it, _SENTINEL)
        else:
            stats.observe_current(current_key, current_value)
            if previous_value == current_value:
                stats.unchanged += 1
            else:
                stats.changed += 1
                yield DeltaOperation.put(current_key, current_value)
            previous_key_seen = previous_key
            current_key_seen = current_key
            previous_item = next(previous_it, _SENTINEL)
            current_item = next(current_it, _SENTINEL)
