from __future__ import annotations

import re
import shutil
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .gc import GcResult, collect_orphan_objects
from .lock import RepositoryLock
from .logicalize import Logicalizer, LogicalizeResult
from .manifest import ManifestStore
from .mutation import SnapshotMutator
from .objects import ObjectStore
from .reader import RepositoryReader
from .progress import JobCancelled, ProgressTracker
from .raw_queue import RawLimitResult, RawQueue
from .retention import RetentionDecision, select_retention
from .rollover import Rollover, RolloverResult
from .verify import RepositoryVerifier, VerifyReport

MaintenanceMode = Literal["FULL", "LOGIC_ONLY"]
JobKind = Literal["maintenance", "pre_capture", "verify", "mutation", "export", "cancelled"]


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
    cancelled_kind: str | None = None
    mutation: dict[str, Any] | None = None
    export: dict[str, Any] | None = None


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
        verify_mode: str,
        tracker: ProgressTracker | None = None,
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
        if verify_mode not in ("normal", "deep"):
            raise ValueError(f"invalid FULL verification mode: {verify_mode}")
        self.verify_mode = verify_mode
        self.tracker = tracker or ProgressTracker()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="endkeep-repository")
        self._future: Future[RepositoryJobResult] | None = None
        self._closed = False

    @property
    def busy(self) -> bool:
        # A completed future remains occupied until poll() consumes it. This
        # closes the completion-delivery window between Future.done() and the
        # worker server moving the result into its explicit ACK queue.
        return self._future is not None

    def start_maintenance(self, mode: MaintenanceMode) -> bool:
        if self._closed or self.busy:
            return False
        stages = (
            ("Clone", "Logicalize", "Sidecar", "Commit", "Retention", "Rollover", "GC", "Verify", "Finalize")
            if mode == "FULL"
            else ("Clone", "Logicalize", "Sidecar", "Commit", "Finalize")
        )
        return self._submit("maintenance", stages, self._run_maintenance, mode, mode=mode)

    def start_pre_capture(self, *, required_bytes: int = 0) -> bool:
        if self._closed or self.busy:
            return False
        return self._submit(
            "pre_capture",
            ("Limits", "Clone", "Logicalize", "Sidecar", "Commit", "Finalize"),
            self._run_pre_capture,
            required_bytes,
        )

    def start_verify(self, *, deep: bool = False) -> bool:
        if self._closed or self.busy:
            return False
        stages = ("Objects", "States", "Finalize") if deep else ("Structure", "Finalize")
        return self._submit("verify", stages, self._run_verify, deep, mode="deep" if deep else "normal")

    def plan_mutation(self, operation: str, snapshot: str) -> dict[str, Any]:
        """Read-only preview of a destructive operation under the repository lock."""
        with RepositoryLock(self.repo_root):
            manifest = self.manifests.load_current()
            if manifest is None:
                raise ValueError("repository is empty")
            index = RepositoryReader._index_of(manifest, snapshot)
            if operation == "delete":
                if len(manifest.chain) == 1:
                    raise ValueError("cannot delete the only recovery point")
                removed = [snapshot]
                if index == 0:
                    detail = f"replace BASE with {manifest.chain[1].snapshot}"
                elif index == len(manifest.chain) - 1:
                    detail = "remove tail DELTA"
                else:
                    detail = f"bridge DELTA {manifest.chain[index - 1].snapshot} -> {manifest.chain[index + 1].snapshot}"
            elif operation == "rollover":
                if index == 0:
                    raise ValueError("selected snapshot is already the BASE")
                removed = [node.snapshot for node in manifest.chain[:index]]
                detail = f"promote {snapshot} to BASE"
            else:
                raise ValueError(f"unknown mutation: {operation}")
            return {
                "generation": manifest.generation,
                "operation": operation,
                "snapshot": snapshot,
                "detail": detail,
                "removed": removed,
                "remaining": len(manifest.chain) - len(removed),
            }

    def start_mutation(self, operation: str, snapshot: str, expected_generation: int) -> bool:
        if self._closed or self.busy:
            return False
        if operation not in ("delete", "rollover") or not snapshot or expected_generation < 0:
            raise ValueError("invalid repository mutation request")
        accepted = self._submit(
            "mutation", ("Validate", "Rewrite", "Finalize"),
            self._run_mutation, operation, snapshot, expected_generation, mode=operation,
        )
        # SnapshotMutator owns its atomic commit sequence; it has no cooperative checkpoints.
        self.tracker.update(cancelable=False)
        return accepted

    def _run_mutation(self, operation: str, snapshot: str, expected_generation: int) -> RepositoryJobResult:
        with RepositoryLock(self.repo_root):
            self.tracker.update(stage="Validate", detail="checking repository generation", cancelable=False)
            manifest = self.manifests.load_current()
            if manifest is None:
                raise ValueError("repository is empty")
            if manifest.generation != expected_generation:
                raise RuntimeError(
                    f"repository changed since preview: generation {manifest.generation} != {expected_generation}"
                )
            self.tracker.update(stage="Rewrite", detail=f"{operation} {snapshot}", cancelable=False)
            mutator = SnapshotMutator(self.manifests, self.objects)
            updated = (
                mutator.delete(manifest, snapshot)
                if operation == "delete"
                else mutator.rollover(manifest, snapshot)
            )
            self.tracker.update(stage="Finalize", detail="manifest committed", cancelable=False)
            result = {
                "operation": operation,
                "snapshot": snapshot,
                "generation": updated.generation,
                "remaining": len(updated.chain),
                "base": updated.chain[0].snapshot,
            }
        return RepositoryJobResult(kind="mutation", mutation=result)

    def start_export(self, snapshot: str) -> bool:
        if self._closed or self.busy:
            return False
        # Never accept a destination path from an in-game command.
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", snapshot):
            raise ValueError("export requires an exact snapshot ID (YYYYMMDD-HHMMSS)")
        accepted = self._submit("export", ("Restore", "Finalize"), self._run_export, snapshot)
        self.tracker.update(cancelable=False)
        return accepted

    def _run_export(self, snapshot: str) -> RepositoryJobResult:
        from endstone_endkeep.offline.cli import command_restore

        exports = self.storage_root / "exports"
        if exports.is_symlink():
            raise ValueError("export directory must not be a symbolic link")
        # An export must never touch the currently running Bedrock world.
        world_root = (Path.cwd() / "worlds").resolve()
        destination = (exports / snapshot).resolve()
        if destination == world_root or world_root in destination.parents:
            raise ValueError("export destination is inside active worlds directory")
        if destination.exists():
            raise FileExistsError(f"export already exists: {destination}")
        self.tracker.update(
            stage="Restore", snapshot=snapshot,
            detail="verifying and rebuilding a separate world", cancelable=False,
        )
        # command_restore locks the repository, verifies dependencies and restored state,
        # and deletes a partial export if recovery fails. No BDS API or active world is used.
        command_restore(self.repo_root, destination, snapshot, show_progress=False)
        self.tracker.update(stage="Finalize", detail="export complete", cancelable=False)
        return RepositoryJobResult(
            kind="export", export={"snapshot": snapshot, "destination": str(destination)}
        )

    def request_cancel(self) -> bool:
        if self._closed or not self.busy:
            return False
        # These operations have no cooperative checkpoints: never acknowledge a
        # cancellation that cannot be honored.
        if self.tracker.snapshot().kind in ("mutation", "export"):
            return False
        return self.tracker.request_cancel()

    def poll(self) -> RepositoryJobResult | None:
        future = self._future
        if future is None or not future.done():
            return None
        self._future = None
        return future.result()

    def close(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _submit(
        self,
        kind: str,
        stages: tuple[str, ...],
        function,
        *args,
        mode: str | None = None,
    ) -> bool:
        self.tracker.begin(kind, stages, mode=mode)
        self._future = self._executor.submit(self._execute_job, kind, function, *args)
        return True

    def _execute_job(self, kind: str, function, *args) -> RepositoryJobResult:
        try:
            return function(*args)
        except JobCancelled:
            return RepositoryJobResult(kind="cancelled", cancelled_kind=kind)
        finally:
            self.tracker.finish()

    def _checkpoint(self) -> None:
        self.tracker.checkpoint()

    def _logical_progress(
        self,
        detail: str,
        current: int | None,
        total: int | None,
        unit: str | None,
        snapshot: str | None,
    ) -> None:
        self._checkpoint()
        stage = {
            "clone": "Clone",
            "scan+compress": "Logicalize",
            "diff+compress": "Logicalize",
            "sidecar": "Sidecar",
            "commit": "Commit",
        }.get(detail, "Logicalize")
        committing = stage == "Commit"
        self.tracker.update(
            stage=stage,
            current=current,
            total=total,
            unit=unit,
            snapshot=snapshot,
            detail=detail,
            approximate=detail == "diff+compress",
            cancelable=not committing,
        )
        if not committing:
            self._checkpoint()

    def _run_pre_capture(self, required_bytes: int) -> RepositoryJobResult:
        self.tracker.update(stage="Limits", detail="checking raw queue limits")
        self._checkpoint()

        def logicalize(path: Path) -> None:
            self.logicalizer.logicalize(
                path,
                progress=self._logical_progress,
                cancel_check=self._checkpoint,
            )
            self._checkpoint()

        with RepositoryLock(self.repo_root):
            result = self.queue.enforce_before_capture(
                logicalize,
                required_bytes=required_bytes,
            )
        self.tracker.update(stage="Finalize", detail="pre-capture checks complete", cancelable=True)
        self._checkpoint()
        return RepositoryJobResult(kind="pre_capture", raw_limits=result)

    def _run_verify(self, deep: bool) -> RepositoryJobResult:
        verifier = RepositoryVerifier(self.manifests, self.objects)
        if not deep:
            self.tracker.update(stage="Structure", detail="checking repository structure")
            self._checkpoint()
            with RepositoryLock(self.repo_root):
                report = verifier.verify(deep=False)
            self.tracker.update(stage="Finalize", detail="verification complete")
            return RepositoryJobResult(kind="verify", verify=report)

        object_done = 0

        def object_progress(_node, role, _metadata, current: int, total: int) -> None:
            nonlocal object_done
            self._checkpoint()
            if current == total:
                object_done += 1
            self.tracker.update(
                stage="Objects",
                current=object_done,
                total=None,
                unit="objects",
                detail=f"verifying {role.lower()} object",
            )

        def state_progress(node, current: int, total: int) -> None:
            self._checkpoint()
            self.tracker.update(
                stage="States",
                current=current,
                total=total,
                unit="records",
                snapshot=node.snapshot,
                detail="verifying logical state",
            )

        with RepositoryLock(self.repo_root):
            report = verifier.verify(
                deep=True,
                object_progress=object_progress,
                state_progress=state_progress,
            )
        self.tracker.update(stage="Finalize", detail="verification complete")
        self._checkpoint()
        return RepositoryJobResult(kind="verify", verify=report)

    def _run_maintenance(self, mode: MaintenanceMode) -> RepositoryJobResult:
        with RepositoryLock(self.repo_root):
            committed, failures = self._drain_pending()
            retention = None
            rollover = None
            gc = None
            verify = None

            if mode == "FULL":
                self._checkpoint()
                self.tracker.update(
                    stage="Retention",
                    current=None,
                    total=None,
                    unit=None,
                    detail="selecting retention",
                )
                manifest = self.manifests.load_current()
                if manifest is not None:
                    retention = select_retention(
                        manifest,
                        keep_days=self.keep_days,
                        keep_last=self.keep_last,
                    )
                    self._checkpoint()

                    self.tracker.update(stage="Rollover", detail="checking BASE rollover")
                    if retention.first_retained_index > 0:

                        def rollover_progress(current: int, total: int) -> None:
                            self._checkpoint()
                            self.tracker.update(
                                stage="Rollover",
                                current=current,
                                total=total,
                                unit="records",
                                detail="materializing new BASE",
                                cancelable=True,
                            )

                        def rollover_phase(phase: str) -> None:
                            if phase == "commit":
                                self._checkpoint()
                                self.tracker.update(
                                    stage="Rollover",
                                    current=None,
                                    total=None,
                                    unit=None,
                                    detail="committing new BASE",
                                    cancelable=False,
                                )
                            else:
                                self.tracker.update(
                                    stage="Rollover",
                                    detail="materializing new BASE",
                                    cancelable=True,
                                )

                        manifest, rollover = Rollover(self.manifests, self.objects).run(
                            manifest,
                            retention.first_retained_index,
                            progress=rollover_progress,
                            cancel_check=self._checkpoint,
                            phase=rollover_phase,
                        )
                        self.tracker.update(
                            stage="Rollover",
                            detail="BASE rollover committed",
                            cancelable=True,
                        )
                    self._checkpoint()

                    def gc_progress(current: int, total: int) -> None:
                        self._checkpoint()
                        self.tracker.update(
                            stage="GC",
                            current=current,
                            total=total,
                            unit="objects",
                            detail="collecting orphan objects",
                            cancelable=True,
                        )

                    self.tracker.update(stage="GC", detail="collecting orphan objects")
                    gc = collect_orphan_objects(
                        self.objects,
                        manifest,
                        progress=gc_progress,
                        cancel_check=self._checkpoint,
                    )
                    self._checkpoint()

                self._cleanup_stale_work()
                verifier = RepositoryVerifier(self.manifests, self.objects)
                if self.verify_mode == "deep":
                    object_done = 0

                    def object_progress(_node, role, _metadata, current: int, total: int) -> None:
                        nonlocal object_done
                        self._checkpoint()
                        if current == total:
                            object_done += 1
                        self.tracker.update(
                            stage="Verify",
                            current=object_done,
                            total=None,
                            unit="objects",
                            detail=f"deep verifying {role.lower()} object",
                        )

                    def state_progress(node, current: int, total: int) -> None:
                        self._checkpoint()
                        self.tracker.update(
                            stage="Verify",
                            current=current,
                            total=total,
                            unit="records",
                            snapshot=node.snapshot,
                            detail="deep verifying logical state",
                        )

                    self.tracker.update(stage="Verify", detail="deep verifying repository")
                    verify = verifier.verify(
                        deep=True,
                        object_progress=object_progress,
                        state_progress=state_progress,
                    )
                else:
                    self.tracker.update(stage="Verify", detail="checking repository structure")
                    verify = verifier.verify(deep=False)
                self._checkpoint()

            self.tracker.update(stage="Finalize", detail="maintenance complete")
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
            self._checkpoint()
            pending = [item for item in self.queue.pending() if item.snapshot_id not in attempted]
            if not pending:
                break
            item = pending[0]
            attempted.add(item.snapshot_id)
            try:
                committed.append(
                    self.logicalizer.logicalize(
                        item.path,
                        progress=self._logical_progress,
                        cancel_check=self._checkpoint,
                    )
                )
            except JobCancelled:
                raise
            except Exception as exc:
                failures.append(DrainFailure(item.snapshot_id, str(exc)))
                # Repository chain order is authoritative. Never skip a failed
                # older raw and commit a newer snapshot ahead of it.
                break
            self.tracker.update(
                stage="Logicalize",
                current=None,
                total=None,
                unit=None,
                snapshot=item.snapshot_id,
                detail="snapshot committed",
                cancelable=True,
            )
        return committed, failures

    def _cleanup_stale_work(self) -> None:
        for root in (self.storage_root / "work", self.repo_root / ".incoming"):
            if not root.exists():
                continue
            for child in root.iterdir():
                self._checkpoint()
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child)
                else:
                    child.unlink(missing_ok=True)
