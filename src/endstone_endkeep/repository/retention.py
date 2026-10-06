from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .manifest import RepositoryManifest


@dataclass(frozen=True)
class RetentionDecision:
    first_retained_index: int
    removed_snapshots: tuple[str, ...]
    retained_snapshots: tuple[str, ...]


def select_retention(
    manifest: RepositoryManifest,
    *,
    keep_days: int,
    keep_last: int,
    now: datetime | None = None,
) -> RetentionDecision:
    """Retain the union of age-based and last-N policies.

    Both policies produce suffixes in chronological chain order, so their union is
    another suffix and rollover only needs to absorb one chain prefix.
    """

    if keep_days < 0 or keep_last < 0:
        raise ValueError("retention values cannot be negative")
    if keep_days == 0 and keep_last == 0:
        raise ValueError("retention must keep at least one recovery point")

    chain = manifest.chain
    current = now or datetime.now().astimezone()
    if current.tzinfo is None:
        current = current.astimezone()

    age_index = len(chain)
    if keep_days > 0:
        cutoff = current - timedelta(days=keep_days)
        for index, node in enumerate(chain):
            captured = datetime.fromisoformat(node.captured_at)
            if captured.tzinfo is None:
                captured = captured.astimezone()
            if captured >= cutoff:
                age_index = index
                break

    last_index = len(chain)
    if keep_last > 0:
        last_index = max(0, len(chain) - keep_last)

    first = min(age_index, last_index)
    if first >= len(chain):
        first = len(chain) - 1

    return RetentionDecision(
        first_retained_index=first,
        removed_snapshots=tuple(node.snapshot for node in chain[:first]),
        retained_snapshots=tuple(node.snapshot for node in chain[first:]),
    )
