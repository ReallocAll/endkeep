from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from endstone_endkeep.repository.maintenance import RepositoryService
from endstone_endkeep.repository.raw_queue import PendingRaw, RawQueue


def test_drain_stops_at_first_failed_oldest_snapshot() -> None:
    older = SimpleNamespace(snapshot_id="older", path=Path("/raw/older"))
    newer = SimpleNamespace(snapshot_id="newer", path=Path("/raw/newer"))

    service = object.__new__(RepositoryService)
    service.queue = SimpleNamespace(pending=lambda: [older, newer])
    calls: list[str] = []

    def logicalize(path: Path):
        calls.append(path.name)
        if path.name == "older":
            raise RuntimeError("transient failure")
        return path.name

    service.logicalizer = SimpleNamespace(logicalize=logicalize)

    committed, failures = service._drain_pending()

    assert committed == []
    assert calls == ["older"]
    assert len(failures) == 1
    assert failures[0].snapshot_id == "older"


def test_pre_capture_uses_authoritative_required_bytes(monkeypatch, tmp_path: Path) -> None:
    queue = RawQueue(
        tmp_path,
        max_pending=18,
        max_age_days=3,
        min_free_space_gib=1,
    )
    pending = [
        PendingRaw(
            path=tmp_path / "raw" / "older",
            snapshot_id="older",
            captured_at=datetime(2026, 10, 6, 12, tzinfo=UTC),
            metadata_valid=True,
        )
    ]
    free_bytes = [queue.min_free_bytes + 100]
    required_bytes = 200

    monkeypatch.setattr(queue, "pending", lambda: list(pending))
    monkeypatch.setattr(
        queue,
        "free_space_allows",
        lambda additional_bytes=0: free_bytes[0] - additional_bytes >= queue.min_free_bytes,
    )

    def logicalize(_path: Path) -> None:
        pending.clear()
        free_bytes[0] += 500

    result = queue.enforce_before_capture(
        logicalize,
        required_bytes=required_bytes,
        now=datetime(2026, 10, 6, 13, tzinfo=UTC),
    )

    assert result.logicalized == ("older",)
    assert result.dropped == ()
    assert result.blocked_for_space is False
