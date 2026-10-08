from __future__ import annotations

from random import Random

import pytest

from endstone_endkeep.logical.diff import DiffStats, compose_delta_pair, semantic_diff
from endstone_endkeep.logical.format import DeltaOperation
from endstone_endkeep.logical.merge import apply_delta


def test_composed_delta_matches_direct_semantic_diff() -> None:
    rng = Random(20261008)

    def state() -> list[tuple[bytes, bytes]]:
        return [
            (f"key-{index:02d}".encode(), bytes((rng.randrange(256),)))
            for index in range(40)
            if rng.random() < 0.7
        ]

    cases = [(state(), state(), state()) for _ in range(100)]
    before_full = [(f"key-{index:04d}".encode(), b"value") for index in range(2000)]
    cases.extend(
        [
            ([], [], []),
            ([], [(b"a", b"1")], []),
            ([(b"a", b"1")], [], [(b"z", b"3")]),
            (before_full, [], [(b"zz", b"after")]),
        ]
    )

    for previous, middle, successor in cases:
        first = list(semantic_diff(previous, middle, DiffStats()))
        second = list(semantic_diff(middle, successor, DiffStats()))
        expected_stats = DiffStats()
        expected = list(semantic_diff(previous, successor, expected_stats))
        actual_stats = DiffStats()
        actual = list(compose_delta_pair(previous, first, second, actual_stats))

        assert actual == expected
        assert actual_stats.state_sha256 == expected_stats.state_sha256
        assert actual_stats.current_records == expected_stats.current_records
        assert actual_stats.current_value_bytes == expected_stats.current_value_bytes
        assert actual_stats.delta_records == expected_stats.delta_records
        assert list(apply_delta(previous, actual)) == successor


@pytest.mark.parametrize(
    ("previous", "first", "second"),
    [
        ([], [DeltaOperation.delete(b"a")], []),
        ([], [], [DeltaOperation.delete(b"a")]),
        ([(b"a", b"1")], [DeltaOperation.delete(b"a")], [DeltaOperation.delete(b"a")]),
    ],
)
def test_composed_delta_rejects_deletion_of_absent_key(
    previous: list[tuple[bytes, bytes]],
    first: list[DeltaOperation],
    second: list[DeltaOperation],
) -> None:
    with pytest.raises(ValueError, match="deletes absent key"):
        list(compose_delta_pair(previous, first, second, DiffStats()))
