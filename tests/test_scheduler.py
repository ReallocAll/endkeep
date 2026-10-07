from __future__ import annotations

import json
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
            "worker": {"priority": "background"},
            "verify": {"mode": "normal"},
        }
    )


def _accepting_dispatch(events):
    def dispatch(event):
        events.append(event)
        return True

    return dispatch


def test_one_trigger_per_local_date_and_reload(tmp_path: Path) -> None:
    events = []
    state_path = tmp_path / "scheduler-state.json"
    scheduler = EndKeepScheduler(_config(), SchedulerState(state_path), _accepting_dispatch(events))
    now = datetime(2026, 10, 6, 12, 0, 1)

    fired = scheduler.tick(now)
    assert len(fired) == 1
    assert fired[0].kind == "capture"
    assert scheduler.tick(now.replace(second=40)) == []

    # Reconstructing after a plugin reload must not fire the same local minute/date again.
    after_reload = EndKeepScheduler(_config(), SchedulerState(state_path), _accepting_dispatch(events))
    assert after_reload.tick(now.replace(second=50)) == []


def test_rejected_capture_is_not_marked_triggered(tmp_path: Path) -> None:
    attempts = []

    def reject(event):
        attempts.append(event)
        return False

    state_path = tmp_path / "scheduler-state.json"
    scheduler = EndKeepScheduler(_config(), SchedulerState(state_path), reject)
    now = datetime(2026, 10, 6, 12, 0, 1)

    assert scheduler.tick(now) == []
    assert scheduler.tick(now.replace(second=40)) == []
    assert len(attempts) == 2


def test_no_catch_up(tmp_path: Path) -> None:
    events = []
    scheduler = EndKeepScheduler(
        _config(),
        SchedulerState(tmp_path / "scheduler-state.json"),
        _accepting_dispatch(events),
    )
    assert scheduler.tick(datetime(2026, 10, 6, 9, 0)) == []
    assert events == []


def test_maintenance_modes(tmp_path: Path) -> None:
    events = []
    scheduler = EndKeepScheduler(
        _config(),
        SchedulerState(tmp_path / "scheduler-state.json"),
        _accepting_dispatch(events),
    )
    first = scheduler.tick(datetime(2026, 10, 6, 6, 0))[0]
    second = scheduler.tick(datetime(2026, 10, 6, 18, 30))[0]
    assert first.maintenance_mode == "FULL"
    assert second.maintenance_mode == "LOGIC_ONLY"


def test_busy_maintenance_is_persisted_and_coalesced(tmp_path: Path) -> None:
    state_path = tmp_path / "scheduler-state.json"
    scheduler = EndKeepScheduler(_config(), SchedulerState(state_path), lambda _event: False)

    accepted = scheduler.tick(datetime(2026, 10, 6, 18, 30))
    assert len(accepted) == 1
    assert scheduler.pending_maintenance is not None
    assert scheduler.pending_maintenance.maintenance_mode == "LOGIC_ONLY"

    # A later FULL request upgrades the single pending slot instead of appending a queue.
    accepted = scheduler.tick(datetime(2026, 10, 7, 6, 0))
    assert len(accepted) == 1
    assert scheduler.pending_maintenance is not None
    assert scheduler.pending_maintenance.maintenance_mode == "FULL"

    # Reload/restart preserves an already accepted pending maintenance job.
    reloaded = EndKeepScheduler(_config(), SchedulerState(state_path), lambda _event: False)
    assert reloaded.pending_maintenance is not None
    assert reloaded.pending_maintenance.maintenance_mode == "FULL"

    # A later LOGIC_ONLY window cannot downgrade the queued FULL.
    accepted = reloaded.tick(datetime(2026, 10, 7, 18, 30))
    assert len(accepted) == 1
    assert reloaded.pending_maintenance is not None
    assert reloaded.pending_maintenance.maintenance_mode == "FULL"


def test_dispatch_pending_remains_durable_until_matching_completion(tmp_path: Path) -> None:
    state_path = tmp_path / "scheduler-state.json"
    scheduler = EndKeepScheduler(_config(), SchedulerState(state_path), lambda _event: False)
    scheduler.tick(datetime(2026, 10, 6, 18, 30))

    assert scheduler.dispatch_pending() is None
    assert scheduler.pending_maintenance is not None

    events = []
    accepting = EndKeepScheduler(_config(), SchedulerState(state_path), _accepting_dispatch(events))
    dispatched = accepting.dispatch_pending()
    assert dispatched is not None
    assert dispatched.maintenance_mode == "LOGIC_ONLY"
    assert dispatched.request_id is not None
    assert accepting.pending_maintenance is not None
    assert len(events) == 1

    reloaded = EndKeepScheduler(_config(), SchedulerState(state_path), lambda _event: False)
    assert reloaded.pending_maintenance is not None
    assert reloaded.complete_pending("maintenance:wrong") is False
    assert reloaded.pending_maintenance is not None
    assert reloaded.complete_pending(dispatched.request_id) is True
    assert reloaded.pending_maintenance is None


def test_queue_and_trigger_ledger_are_persisted_in_one_atomic_save(tmp_path: Path) -> None:
    state_path = tmp_path / "scheduler-state.json"
    state = SchedulerState(state_path)
    save_calls = 0
    original_save = state._save_atomic

    def counted_save() -> None:
        nonlocal save_calls
        save_calls += 1
        original_save()

    state._save_atomic = counted_save  # type: ignore[method-assign]
    scheduler = EndKeepScheduler(_config(), state, lambda _event: False)
    accepted = scheduler.tick(datetime(2026, 10, 6, 18, 30))

    assert len(accepted) == 1
    assert save_calls == 1
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["maintenance"]["18:30"] == "2026-10-06"
    assert persisted["pending_maintenance"]["request_id"] == accepted[0].request_id
