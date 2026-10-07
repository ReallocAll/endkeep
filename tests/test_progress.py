from __future__ import annotations

import time

import pytest

from endstone_endkeep.repository.progress import JobCancelled, ProgressTracker


def test_progress_tracker_reports_pipeline_and_cooperative_cancel() -> None:
    tracker = ProgressTracker()
    tracker.begin("maintenance", ("Logicalize", "Finalize"), mode="LOGIC_ONLY")
    tracker.update(
        stage="Logicalize",
        current=16384,
        total=32768,
        unit="records",
        snapshot="20261007-203000",
        detail="diff+compress",
    )

    status = tracker.snapshot()
    assert status.state == "running"
    assert status.kind == "maintenance"
    assert status.mode == "LOGIC_ONLY"
    assert status.stages == ("Logicalize", "Finalize")
    assert status.stage_index == 0
    assert status.current == 16384
    assert status.total == 32768
    assert status.snapshot == "20261007-203000"
    assert status.elapsed_seconds >= 0.0

    assert tracker.request_cancel() is True
    assert tracker.snapshot().state == "cancel_requested"
    with pytest.raises(JobCancelled):
        tracker.checkpoint()


def test_cancel_request_waits_through_non_cancelable_atomic_stage() -> None:
    tracker = ProgressTracker()
    tracker.begin("maintenance", ("Logicalize", "Finalize"), mode="LOGIC_ONLY")
    tracker.update(stage="Logicalize", detail="commit", cancelable=False)

    assert tracker.request_cancel() is True
    tracker.checkpoint()

    tracker.update(stage="Finalize", detail="post-commit", cancelable=True)
    with pytest.raises(JobCancelled):
        tracker.checkpoint()


def test_finish_resets_progress_state() -> None:
    tracker = ProgressTracker()
    tracker.begin("verify", ("Structure", "Finalize"), mode="normal")
    time.sleep(0.001)
    tracker.finish()

    status = tracker.snapshot()
    assert status.state == "idle"
    assert status.kind is None
    assert status.stages == ()
    assert status.elapsed_seconds == 0.0


def test_stage_elapsed_resets_when_stage_changes(monkeypatch) -> None:
    clock = [10.0]
    monkeypatch.setattr("endstone_endkeep.repository.progress.time.monotonic", lambda: clock[0])

    tracker = ProgressTracker()
    tracker.begin("maintenance", ("Clone", "Logicalize", "Finalize"), mode="LOGIC_ONLY")

    clock[0] = 15.0
    tracker.update(stage="Clone", detail="clone")
    assert tracker.snapshot().stage_elapsed_seconds == 5.0

    clock[0] = 20.0
    tracker.update(stage="Logicalize", current=1000, unit="records", detail="scan+compress")
    assert tracker.snapshot().stage_elapsed_seconds == 0.0

    clock[0] = 25.0
    status = tracker.snapshot()
    assert status.stage_elapsed_seconds == 5.0
    assert status.elapsed_seconds == 15.0
