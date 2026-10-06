from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING, Literal

from .bds.commands import BdsSaveAdapter
from .bds.model import SnapshotManifest
from .bds.query_parser import QueryManifestError, QueryManifestParser
from .staging.raw import RawSnapshotStore, StagedRawSnapshot

if TYPE_CHECKING:
    from endstone.plugin import Plugin


CaptureState = Literal["idle", "querying", "staging", "resume_retry", "publishing"]


@dataclass(frozen=True)
class CaptureStatus:
    state: CaptureState
    held: bool
    scheduled_for: str | None


class CaptureCoordinator:
    """Main-thread BDS state machine with exact staging/durability on one worker."""

    QUERY_TIMEOUT_SECONDS = 15.0
    QUERY_RETRY_TICKS = 10

    def __init__(
        self,
        plugin: Plugin,
        raw_store: RawSnapshotStore,
        *,
        space_guard: Callable[[int], bool] | None = None,
        space_recovery: Callable[[int, str | None], bool] | None = None,
    ) -> None:
        self._plugin = plugin
        self._adapter = BdsSaveAdapter(plugin.server)
        self._raw_store = raw_store
        self._query_failures = 0
        self._query_retry_ticks = 0
        self._space_guard = space_guard
        self._space_recovery = space_recovery
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="endkeep-capture")
        self._state: CaptureState = "idle"
        self._scheduled_for: str | None = None
        self._deadline = 0.0
        self._hold_started = 0.0
        self._hold_elapsed = 0.0
        self._cancel = Event()
        self._future: Future | None = None
        self._manifest: SnapshotManifest | None = None
        self._staged: StagedRawSnapshot | None = None
        self._space_retry_bytes: int | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._state != "idle" or self._adapter.is_held

    def status(self) -> CaptureStatus:
        return CaptureStatus(
            state=self._state,
            held=self._adapter.is_held,
            scheduled_for=self._scheduled_for,
        )

    def start_capture(self, *, scheduled_for: str | None) -> bool:
        """Begin capture on the Endstone main thread; returns False if already busy."""

        if self._closed:
            raise RuntimeError("capture coordinator is closed")
        if self.busy:
            return False

        self._scheduled_for = scheduled_for
        self._cancel = Event()
        self._manifest = None
        self._staged = None
        self._future = None
        self._space_retry_bytes = None
        self._query_failures = 0
        self._query_retry_ticks = 0

        hold_started = time.monotonic()
        try:
            self._adapter.hold()
        except Exception as exc:
            self._plugin.logger.error(f"CAPTURE FAILURE: save hold failed: {exc}")
            self._reset()
            return False

        now = time.monotonic()
        self._hold_started = hold_started
        self._deadline = now + self.QUERY_TIMEOUT_SECONDS
        self._state = "querying"
        return True

    def pump(self) -> None:
        """Advance capture state. Must be called from the Endstone main thread."""

        if self._closed or self._state == "idle":
            return
        if self._state == "querying":
            self._pump_query()
        elif self._state == "staging":
            self._pump_staging()
        elif self._state == "resume_retry":
            self._pump_resume_retry()
        elif self._state == "publishing":
            self._pump_publish()

    def close(self) -> None:
        self._closed = True
        self._cancel.set()
        if self._adapter.is_held:
            if not self._adapter.best_effort_resume():
                self._plugin.logger.critical(
                    "CAPTURE FAILURE: plugin disabled while BDS save was held and save resume failed"
                )
        # Do not let an old capture worker race a freshly reloaded plugin's
        # startup cleanup. Cancellation is checked between bounded copy chunks;
        # publication, if already unheld, is allowed to finish before teardown.
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _pump_query(self) -> None:
        now = time.monotonic()
        if now >= self._deadline:
            self._fail_held(f"save query timed out after {self._query_failures} unsuccessful attempt(s)")
            return
        if self._query_retry_ticks > 0:
            self._query_retry_ticks -= 1
            if self._query_retry_ticks > 0:
                return

        try:
            capture = self._adapter.query()
        except Exception as exc:
            self._schedule_query_retry(f"save query raised: {exc}")
            return

        try:
            manifest = QueryManifestParser.parse_messages((*capture.messages, *capture.errors))
        except QueryManifestError:
            # Endstone's dispatch_command() return value reflects command success,
            # not whether the vanilla command reached BDS. A false result is expected
            # while save query is waiting for the held save to become ready.
            self._schedule_query_retry("save query did not return a valid manifest")
            return

        if self._space_guard is not None and not self._space_guard(manifest.total_bytes):
            self._defer_for_space(manifest.total_bytes)
            return

        self._manifest = manifest
        self._future = self._executor.submit(
            self._raw_store.stage,
            manifest,
            scheduled_for=self._scheduled_for,
            cancel=self._cancel,
        )
        self._state = "staging"

    def _schedule_query_retry(self, _reason: str) -> None:
        self._query_failures += 1
        self._query_retry_ticks = self.QUERY_RETRY_TICKS

    def _defer_for_space(self, required_bytes: int) -> None:
        self._space_retry_bytes = required_bytes
        if self._resume_after_stage():
            self._start_space_recovery()
            return

        self._state = "resume_retry"
        self._plugin.logger.critical(
            "CAPTURE FAILURE: insufficient free space and save resume failed; retrying resume before cleanup"
        )

    def _pump_staging(self) -> None:
        future = self._future
        if future is None or not future.done():
            return

        try:
            staged = future.result()
        except Exception as exc:
            self._fail_held(f"exact-byte staging failed: {exc}")
            return

        self._staged = staged
        if self._resume_after_stage():
            self._start_publish()
        else:
            self._state = "resume_retry"
            self._plugin.logger.critical(
                "CAPTURE FAILURE: exact staging completed but save resume failed; retrying resume"
            )

    def _pump_resume_retry(self) -> None:
        if not self._resume_after_stage():
            return
        if self._staged is not None:
            self._start_publish()
        elif self._space_retry_bytes is not None:
            self._start_space_recovery()
        else:
            self._reset()

    def _resume_after_stage(self) -> bool:
        try:
            result = self._adapter.resume()
        except Exception as exc:
            self._plugin.logger.error(f"CAPTURE FAILURE: save resume raised: {exc}")
            return False
        if not result.dispatched or result.errors:
            return False
        if self._hold_started > 0:
            self._hold_elapsed = time.monotonic() - self._hold_started
        return True

    def _start_space_recovery(self) -> None:
        required_bytes = self._space_retry_bytes
        scheduled_for = self._scheduled_for
        self._reset()
        if required_bytes is None:
            return
        if self._space_recovery is None or not self._space_recovery(required_bytes, scheduled_for):
            self._plugin.logger.error(
                "CAPTURE FAILURE: insufficient free space and emergency raw cleanup could not be scheduled"
            )

    def _start_publish(self) -> None:
        staged = self._staged
        if staged is None:
            self._fail_unheld("internal error: staged snapshot missing before publish")
            return
        self._future = self._executor.submit(self._raw_store.publish, staged)
        self._state = "publishing"

    def _pump_publish(self) -> None:
        future = self._future
        if future is None or not future.done():
            return
        staged = self._staged
        try:
            final_path = future.result()
        except Exception as exc:
            self._plugin.logger.error(f"CAPTURE FAILURE: raw durability/publication failed after resume: {exc}")
            self._reset()
            return

        if staged is not None:
            self._plugin.logger.info(
                f"Snapshot {staged.snapshot_id} captured: files={staged.stage.files} "
                f"bytes={staged.stage.bytes_copied} hold={self._hold_elapsed:.3f}s "
                f"stage_copy={staged.stage.elapsed_seconds:.3f}s raw={final_path}"
            )
        self._reset()

    def _fail_held(self, reason: str) -> None:
        self._cancel.set()
        resumed = self._adapter.best_effort_resume()
        if not resumed:
            self._plugin.logger.critical(
                f"CAPTURE FAILURE: {reason}; best-effort save resume failed; retrying save resume"
            )
            self._future = None
            self._manifest = None
            self._staged = None
            self._state = "resume_retry"
            return

        self._plugin.logger.error(f"CAPTURE FAILURE: {reason}")
        self._reset()

    def _fail_unheld(self, reason: str) -> None:
        self._plugin.logger.error(f"CAPTURE FAILURE: {reason}")
        self._reset()

    def _reset(self) -> None:
        self._state = "idle"
        self._scheduled_for = None
        self._deadline = 0.0
        self._hold_started = 0.0
        self._hold_elapsed = 0.0
        self._future = None
        self._manifest = None
        self._staged = None
        self._space_retry_bytes = None
        self._query_failures = 0
        self._query_retry_ticks = 0
