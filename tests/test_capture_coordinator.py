from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import endstone_endkeep.coordinator as coordinator_module
from endstone_endkeep.bds.model import SnapshotEntry, SnapshotManifest
from endstone_endkeep.coordinator import CaptureCoordinator
from endstone_endkeep.staging.exact import StageResult
from endstone_endkeep.staging.raw import StagedRawSnapshot


class _Logger:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.criticals: list[str] = []
        self.infos: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def critical(self, message: str) -> None:
        self.criticals.append(message)

    def info(self, message: str) -> None:
        self.infos.append(message)


class _Plugin:
    def __init__(self) -> None:
        self.server = object()
        self.logger = _Logger()


class _FakeAdapter:
    def __init__(self, *, query_error: Exception | None = None, resume_failures: int = 0) -> None:
        self._held = False
        self.query_error = query_error
        self.resume_failures = resume_failures
        self.resume_calls = 0

    @property
    def is_held(self) -> bool:
        return self._held

    def hold(self):
        self._held = True
        return SimpleNamespace(dispatched=True, messages=(), errors=())

    def query(self):
        if self.query_error is not None:
            raise self.query_error
        return SimpleNamespace(dispatched=True, messages=(), errors=())

    def resume(self):
        self.resume_calls += 1
        if self.resume_calls <= self.resume_failures:
            return SimpleNamespace(dispatched=False, messages=(), errors=("transient",))
        self._held = False
        return SimpleNamespace(dispatched=True, messages=(), errors=())

    def best_effort_resume(self) -> bool:
        result = self.resume()
        return result.dispatched and not result.errors


class _RawStore:
    def __init__(self, publish_path: Path) -> None:
        self.publish_path = publish_path
        self.published: list[StagedRawSnapshot] = []

    def publish(self, staged: StagedRawSnapshot) -> Path:
        self.published.append(staged)
        return self.publish_path


def _coordinator(monkeypatch, tmp_path: Path, adapter: _FakeAdapter) -> tuple[CaptureCoordinator, _Plugin, _RawStore]:
    plugin = _Plugin()
    raw_store = _RawStore(tmp_path / "raw" / "snapshot")
    monkeypatch.setattr(coordinator_module, "BdsSaveAdapter", lambda _server: adapter)
    coordinator = CaptureCoordinator(plugin, raw_store)
    return coordinator, plugin, raw_store


def test_held_failure_retries_resume_and_blocks_new_capture(monkeypatch, tmp_path: Path) -> None:
    adapter = _FakeAdapter(query_error=RuntimeError("query failed"), resume_failures=1)
    coordinator, plugin, _raw_store = _coordinator(monkeypatch, tmp_path, adapter)
    try:
        assert coordinator.start_capture(scheduled_for=None)

        coordinator.pump()

        assert coordinator.status().state == "resume_retry"
        assert coordinator.status().held
        assert coordinator.busy
        assert not coordinator.start_capture(scheduled_for=None)
        assert plugin.logger.criticals

        coordinator.pump()

        assert coordinator.status().state == "idle"
        assert not coordinator.status().held
        assert not coordinator.busy
        assert adapter.resume_calls == 2
    finally:
        coordinator.close()


def test_staged_snapshot_is_published_only_after_resume_retry_succeeds(monkeypatch, tmp_path: Path) -> None:
    adapter = _FakeAdapter(resume_failures=1)
    coordinator, _plugin, raw_store = _coordinator(monkeypatch, tmp_path, adapter)
    manifest = SnapshotManifest(
        "level",
        (SnapshotEntry(PurePosixPath("level/level.dat"), 3),),
    )
    staged = StagedRawSnapshot(
        snapshot_id="20261006-120000",
        incoming_path=tmp_path / "incoming",
        manifest=manifest,
        captured_at="2026-10-06T12:00:00+08:00",
        scheduled_for=None,
        stage=StageResult(files=1, bytes_copied=3, elapsed_seconds=0.01),
    )

    future: Future[StagedRawSnapshot] = Future()
    future.set_result(staged)
    adapter._held = True
    coordinator._state = "staging"
    coordinator._future = future

    try:
        coordinator.pump()
        assert coordinator.status().state == "resume_retry"
        assert coordinator.status().held
        assert raw_store.published == []

        coordinator.pump()
        assert coordinator.status().state == "publishing"
        assert not coordinator.status().held

        assert coordinator._future is not None
        coordinator._future.result(timeout=1)
        coordinator.pump()

        assert coordinator.status().state == "idle"
        assert raw_store.published == [staged]
    finally:
        coordinator.close()
