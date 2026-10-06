from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from endstone_endkeep.repository.maintenance import RepositoryService


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
