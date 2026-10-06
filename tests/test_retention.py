from __future__ import annotations

from datetime import UTC, datetime, timedelta

from endstone_endkeep.repository.manifest import RepositoryManifest, SnapshotNode
from endstone_endkeep.repository.objects import ObjectMetadata
from endstone_endkeep.repository.retention import select_retention


_META = ObjectMetadata("a" * 64, "b" * 64, 1, 1, "zstd", 6)


def _node(index: int, captured_at: datetime, node_type: str) -> SnapshotNode:
    return SnapshotNode(
        snapshot=f"s{index}",
        world_name="level",
        type=node_type,  # type: ignore[arg-type]
        object=_META,
        sidecar=_META,
        state_sha256="c" * 64,
        records=1,
        value_bytes=1,
        captured_at=captured_at.isoformat(),
    )


def test_retention_is_union_of_age_and_last_n() -> None:
    now = datetime(2026, 10, 6, 12, tzinfo=UTC)
    chain = tuple(
        _node(index, now - timedelta(days=9 - index), "base" if index == 0 else "delta")
        for index in range(10)
    )
    manifest = RepositoryManifest(1, chain)

    decision = select_retention(manifest, keep_days=3, keep_last=5, now=now)
    # keep_last=5 starts at s5; keep_days=3 starts later, so union starts at s5.
    assert decision.first_retained_index == 5
    assert decision.removed_snapshots == ("s0", "s1", "s2", "s3", "s4")
    assert decision.retained_snapshots == ("s5", "s6", "s7", "s8", "s9")
