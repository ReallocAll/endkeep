from __future__ import annotations

from datetime import datetime
from pathlib import Path

from endstone_endkeep.config import EndKeepConfig
from endstone_endkeep.scheduler import EndKeepScheduler, SchedulerState


def _config() -> EndKeepConfig:
    return EndKeepConfig.from_mapping(
        {
            "enabled": True,
            "capture": {"times": ["12:00", "16:30", "20:30", "23:45"]},
            "maintenance": {"times": ["06:00", "18:30"]},
            "raw": {"max_pending": 18, "max_age_days": 3},
            "logical": {"compression_level": 6, "compression_threads": 4},
            "retention": {"keep_days": 7, "keep_last": 28},
            "storage": {"path": "backups", "min_free_space_gib": 5},
        }
    )


def test_one_trigger_per_local_date_and_reload(tmp_path: Path) -> None:
    events = []
    state_path = tmp_path / "scheduler-state.json"
    scheduler = EndKeepScheduler(_config(), SchedulerState(state_path), events.append)
    now = datetime(2026, 10, 6, 12, 0, 1)

    fired = scheduler.tick(now)
    assert len(fired) == 1
    assert fired[0].kind == "capture"
    assert scheduler.tick(now.replace(second=40)) == []

    # Reconstructing after a plugin reload must not fire the same local minute/date again.
    after_reload = EndKeepScheduler(_config(), SchedulerState(state_path), events.append)
    assert after_reload.tick(now.replace(second=50)) == []


def test_no_catch_up(tmp_path: Path) -> None:
    events = []
    scheduler = EndKeepScheduler(
        _config(),
        SchedulerState(tmp_path / "scheduler-state.json"),
        events.append,
    )
    assert scheduler.tick(datetime(2026, 10, 6, 9, 0)) == []
    assert events == []


def test_maintenance_modes(tmp_path: Path) -> None:
    events = []
    scheduler = EndKeepScheduler(
        _config(),
        SchedulerState(tmp_path / "scheduler-state.json"),
        events.append,
    )
    first = scheduler.tick(datetime(2026, 10, 6, 6, 0))[0]
    second = scheduler.tick(datetime(2026, 10, 6, 18, 30))[0]
    assert first.maintenance_mode == "FULL"
    assert second.maintenance_mode == "LOGIC_ONLY"
