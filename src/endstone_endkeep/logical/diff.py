from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Iterator
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
    *,
    progress: Callable[[int], None] | None = None,
    progress_interval: int = 16384,
) -> Iterator[DeltaOperation]:
    """Streaming sorted-key semantic diff from previous state to current state."""

    if progress_interval <= 0:
        raise ValueError("progress_interval must be positive")

    previous_it = iter(previous)
    current_it = iter(current)
    previous_item = next(previous_it, _SENTINEL)
    current_item = next(current_it, _SENTINEL)
    previous_key_seen: bytes | None = None
    current_key_seen: bytes | None = None
    pending_progress = 0

    while previous_item is not _SENTINEL or current_item is not _SENTINEL:
        pending_progress += 1
        if progress is not None and pending_progress >= progress_interval:
            progress(pending_progress)
            pending_progress = 0

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

    if progress is not None and pending_progress:
        progress(pending_progress)


def compose_delta_pair(
    previous: Iterable[tuple[bytes, bytes]],
    first: Iterable[DeltaOperation],
    second: Iterable[DeltaOperation],
    stats: DiffStats,
) -> Iterator[DeltaOperation]:
    """Build the minimal previous-to-successor DELTA in one bounded-memory pass.

    The two input DELTAs apply in order. Each key is processed once against its
    original value, so an insertion followed by a deletion emits no operation.
    """

    missing = object()
    previous_it = iter(previous)
    first_it = iter(first)
    second_it = iter(second)
    previous_item = next(previous_it, missing)
    first_item = next(first_it, missing)
    second_item = next(second_it, missing)
    last_key: bytes | None = None

    while previous_item is not missing or first_item is not missing or second_item is not missing:
        keys = []
        if previous_item is not missing:
            keys.append(previous_item[0])
        if first_item is not missing:
            keys.append(first_item.key)
        if second_item is not missing:
            keys.append(second_item.key)
        key = min(keys)
        if last_key is not None and key <= last_key:
            raise ValueError("bridge inputs are not in strictly increasing key order")
        last_key = key

        original = missing
        if previous_item is not missing and previous_item[0] == key:
            original = previous_item[1]
            previous_item = next(previous_it, missing)

        operations = []
        if first_item is not missing and first_item.key == key:
            operations.append(first_item)
            first_item = next(first_it, missing)
        if second_item is not missing and second_item.key == key:
            operations.append(second_item)
            second_item = next(second_it, missing)

        result = original
        for operation in operations:
            if operation.kind == "delete":
                if result is missing:
                    raise ValueError(f"DELTA deletes absent key {key!r}")
                result = missing
            elif operation.kind == "put":
                if not isinstance(operation.value, bytes):
                    raise ValueError(f"DELTA put has invalid value for key {key!r}")
                result = operation.value
            else:
                raise ValueError(f"invalid DELTA operation: {operation.kind!r}")

        if result is not missing:
            stats.observe_current(key, result)

        if original is missing:
            if result is not missing:
                stats.inserted += 1
                yield DeltaOperation.put(key, result)
        elif result is missing:
            stats.deleted += 1
            yield DeltaOperation.delete(key)
        elif original == result:
            stats.unchanged += 1
        else:
            stats.changed += 1
            yield DeltaOperation.put(key, result)
