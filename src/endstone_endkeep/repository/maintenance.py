from __future__ import annotations

import shutil
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .gc import GcResult, collect_orphan_objects
from .lock import RepositoryLock
from .logicalize import LogicalizeResult, Logicalizer
from .manifest import ManifestStore
from .objects import ObjectStore
from .raw_queue import RawLimitResult, RawQueue
from .retention import RetentionDecision, select_retention
from .rollover import Rollover, RolloverResult
from .verify import RepositoryVerifier, VerifyReport


MaintenanceMode = Literal["FULL", "LOGIC_ONLY"]
JobKind = Literal["maintenance", "pre_capture", "verify"]


@dataclass(frozen=True)
class DrainFailure:
    snapshot_id: str
    error: str


@dataclass(frozen=True)
class MaintenanceResult:
    mode: MaintenanceMode
    committed: tuple[LogicalizeResult, ...]
    failures: tuple[DrainFailure, ...]
    retention: RetentionDecision | None
    rollover: RolloverResult | None
    gc: GcResult | None
    verify: VerifyReport | None


@dataclass(frozen=True)
class RepositoryJobResult:
    kind: JobKind
    maintenance: MaintenanceResult | None = None
    raw_limits: RawLimitResult | None = None
    verify: VerifyReport | None = None


class RepositoryService:
    """Single-worker repository executor. No Endstone/BDS API is called here."""

    def __init__(
        self,
        storage_root: Path,
        *,
        compression_level: int,
        compression_threads: int,
        max_pending: int,
        max_age_days: int,
        min_free_space_gib: int,
        keep_days: int,
        keep_last: int,
    ) -> None:
        self.storage_root = storage_root
        self.repo_root = storage_root / "repo"
        self.logicalizer = Logicalizer(
            storage_root,
            compression_level=compression_level,
            compression_threads=compression_threads,
        )
        self.manifests: ManifestStore = self.logicalizer.manifests
        self.objects: ObjectStore = self.logicalizer.objects
        self.queue = RawQueue(
            storage_root,
            max_pending=max_pending,
            max_age_days=max_age_days,
            min_free_space_gib=min_free_space_gib,
        )
        self.keep_days = keep_days
        self.keep_last = keep_last
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="endkeep-repository")
        self._future: Future[RepositoryJobResult] | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        return self._future is not None and not self._future.done()

    def start_maintenance(self, mode: MaintenanceMode) -> bool:
        if self._closed or self.busy:
            return False
        self._future = self._executor.submit(self._run_maintenance, mode)
        return True

    def start_pre_capture(self) -> bool:
        if self._closed or self.busy:
            return False
        self._future = self._executor.submit(self._run_pre_capture)
        return True

    def start_verify(self) -> bool:
        if self._closed or self.busy:
            return False
        self._future = self._executor.submit(self._run_verify)
        return True

    def poll(self) -> RepositoryJobResult | None:
        future = self._future
        if future is None or not future.done():
            return None
        self._future = None
        return future.result()

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run_pre_capture(self) -> RepositoryJobResult:
        with RepositoryLock(self.repo_root):
            result = self.queue.enforce_before_capture(self.logicalizer.logicalize)
        return RepositoryJobResult(kind="pre_capture", raw_limits=result)

    def _run_verify(self) -> RepositoryJobResult:
        with RepositoryLock(self.repo_root):
            report = RepositoryVerifier(self.manifests, self.objects).verify(deep=True)
        return RepositoryJobResult(kind="verify", verify=report)

    def _run_maintenance(self, mode: MaintenanceMode) -> RepositoryJobResult:
        with RepositoryLock(self.repo_root):
            committed, failures = self._drain_pending()
            retention = None
            rollover = None
            gc = None
            verify = None

            if mode == "FULL":
                manifest = self.manifests.load_current()
                if manifest is not None:
                    retention = select_retention(
                        manifest,
                        keep_days=self.keep_days,
                        keep_last=self.keep_last,
                    )
                    if retention.first_retained_index > 0:
                        manifest, rollover = Rollover(self.manifests, self.objects).run(
                            manifest,
                            retention.first_retained_index,
                        )
                    gc = collect_orphan_objects(self.objects, manifest)
                self._cleanup_stale_work()
                verify = RepositoryVerifier(self.manifests, self.objects).verify(deep=False)

            result = MaintenanceResult(
                mode=mode,
                committed=tuple(committed),
                failures=tuple(failures),
                retention=retention,
                rollover=rollover,
                gc=gc,
                verify=verify,
            )
        return RepositoryJobResult(kind="maintenance", maintenance=result)

    def _drain_pending(self) -> tuple[list[LogicalizeResult], list[DrainFailure]]:
        committed: list[LogicalizeResult] = []
        failures: list[DrainFailure] = []
        attempted: set[str] = set()

        while True:
            pending = [item for item in self.queue.pending() if item.snapshot_id not in attempted]
            if not pending:
                break
            item = pending[0]
            attempted.add(item.snapshot_id)
            try:
                committed.append(self.logicalizer.logicalize(item.path))
            except Exception as exc:
                failures.append(DrainFailure(item.snapshot_id, str(exc)))
        return committed, failures

    def _cleanup_stale_work(self) -> None:
        for root in (self.storage_root / "work", self.repo_root / ".incoming"):
            if not root.exists():
                continue
            for child in root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
