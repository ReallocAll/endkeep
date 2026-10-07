from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from endstone_endkeep.logical.amulet_reader import iter_visible_state
from endstone_endkeep.logical.diff import DiffStats, semantic_diff
from endstone_endkeep.logical.format import BaseStats, DeltaStats, write_base, write_delta
from endstone_endkeep.logical.sidecar import SidecarStats, write_sidecar
from endstone_endkeep.staging.clone import clone_world, remove_processing_clone
from endstone_endkeep.staging.metadata import RawSnapshotMetadata, load_raw_snapshot

from .manifest import ManifestStore, RepositoryManifest, SnapshotNode, snapshot_order_key
from .objects import ObjectMetadata, ObjectStore
from .reader import RepositoryReader
from .transaction import RepositoryTransaction

type LogicalizeProgress = Callable[[str, int | None, int | None, str | None, str | None], None]
type CancelCheck = Callable[[], None]


def delta_progress_counts(stats: DiffStats, previous_total: int) -> tuple[int, int]:
    """Return processed merge keys and a monotonic estimated total for DELTA progress."""

    previous_consumed = stats.unchanged + stats.changed + stats.deleted
    processed = previous_consumed + stats.inserted
    estimated_total = previous_total + stats.inserted
    return processed, estimated_total


@dataclass(frozen=True)
class LogicalizeResult:
    snapshot_id: str
    node_type: str
    records: int
    value_bytes: int
    unchanged: int
    changed: int
    inserted: int
    deleted: int
    logical_bytes: int
    compressed_bytes: int
    elapsed_seconds: float
    raw_deleted: bool


class Logicalizer:
    """Convert one durable raw snapshot into the crash-safe logical repository."""

    def __init__(
        self,
        storage_root: Path,
        *,
        compression_level: int,
        compression_threads: int,
    ) -> None:
        self.storage_root = storage_root
        self.repo_root = storage_root / "repo"
        self.work_root = storage_root / "work"
        self.manifests = ManifestStore(self.repo_root)
        self.objects = ObjectStore(
            self.repo_root,
            compression_level=compression_level,
            compression_threads=compression_threads,
        )
        self.reader = RepositoryReader(self.objects)

    def logicalize(
        self,
        raw_path: Path,
        *,
        progress: LogicalizeProgress | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> LogicalizeResult:
        started = time.monotonic()
        raw = load_raw_snapshot(raw_path)
        current_manifest = self.manifests.load_current()
        self._checkpoint(cancel_check)

        if current_manifest is not None:
            tail = current_manifest.chain[-1]
            if snapshot_order_key(raw.captured_at, raw.snapshot_id) <= snapshot_order_key(
                tail.captured_at,
                tail.snapshot,
            ):
                raise ValueError(f"raw snapshot {raw.snapshot_id} is not newer than repository tail {tail.snapshot}")

        self._report(progress, "clone", None, None, None, raw.snapshot_id)
        clone_world(raw.path, raw.manifest.world_name, self.work_root, raw.snapshot_id)
        self._checkpoint(cancel_check)

        db_path = self.work_root / raw.snapshot_id / raw.manifest.world_name / "db"
        try:
            if current_manifest is None:
                node, diff_counts = self._create_base(
                    raw,
                    db_path,
                    progress=progress,
                    cancel_check=cancel_check,
                )
                new_chain = (node,)
            else:
                if current_manifest.chain[0].world_name != raw.manifest.world_name:
                    raise ValueError(
                        f"raw snapshot world {raw.manifest.world_name!r} does not match repository "
                        f"world {current_manifest.chain[0].world_name!r}"
                    )
                if any(existing.snapshot == raw.snapshot_id for existing in current_manifest.chain):
                    raise ValueError(f"snapshot already committed: {raw.snapshot_id}")
                node, diff_counts = self._create_delta(
                    raw,
                    db_path,
                    current_manifest,
                    progress=progress,
                    cancel_check=cancel_check,
                )
                new_chain = (*current_manifest.chain, node)

            self._checkpoint(cancel_check)
            generation = 1 if current_manifest is None else current_manifest.generation + 1
            new_manifest = RepositoryManifest(generation=generation, chain=tuple(new_chain))
            self._report(progress, "commit", None, None, None, raw.snapshot_id)
            RepositoryTransaction(self.manifests).commit(new_manifest)
        finally:
            remove_processing_clone(self.work_root, raw.snapshot_id)

        raw_deleted = True
        try:
            shutil.rmtree(raw.path)
        except OSError:
            raw_deleted = False

        return LogicalizeResult(
            snapshot_id=raw.snapshot_id,
            node_type=node.type,
            records=node.records,
            value_bytes=node.value_bytes,
            unchanged=diff_counts[0],
            changed=diff_counts[1],
            inserted=diff_counts[2],
            deleted=diff_counts[3],
            logical_bytes=node.object.logical_bytes,
            compressed_bytes=node.object.compressed_bytes,
            elapsed_seconds=time.monotonic() - started,
            raw_deleted=raw_deleted,
        )

    def _create_base(
        self,
        raw: RawSnapshotMetadata,
        db_path: Path,
        *,
        progress: LogicalizeProgress | None,
        cancel_check: CancelCheck | None,
    ) -> tuple[SnapshotNode, tuple[int, int, int, int]]:
        completed = 0

        def advance(amount: int) -> None:
            nonlocal completed
            completed += amount
            self._report(progress, "scan+compress", completed, None, "records", raw.snapshot_id)
            self._checkpoint(cancel_check)

        self._report(progress, "scan+compress", 0, None, "records", raw.snapshot_id)
        with iter_visible_state(db_path) as current:
            object_meta, stats = self.objects.create(
                lambda stream: write_base(
                    stream,
                    current,
                    progress=advance if progress is not None or cancel_check is not None else None,
                )
            )
        assert isinstance(stats, BaseStats)
        self._checkpoint(cancel_check)
        sidecar_meta, _sidecar_stats = self._create_sidecar(
            raw,
            progress=progress,
            cancel_check=cancel_check,
        )
        return (
            SnapshotNode(
                snapshot=raw.snapshot_id,
                world_name=raw.manifest.world_name,
                type="base",
                object=object_meta,
                sidecar=sidecar_meta,
                state_sha256=stats.state_sha256,
                records=stats.records,
                value_bytes=stats.value_bytes,
                captured_at=raw.captured_at,
            ),
            (0, 0, stats.records, 0),
        )

    def _create_delta(
        self,
        raw: RawSnapshotMetadata,
        db_path: Path,
        current_manifest: RepositoryManifest,
        *,
        progress: LogicalizeProgress | None,
        cancel_check: CancelCheck | None,
    ) -> tuple[SnapshotNode, tuple[int, int, int, int]]:
        stats = DiffStats()
        previous = self.reader.iter_state(current_manifest)
        previous_total = current_manifest.chain[-1].records

        def advance(_amount: int) -> None:
            processed, estimated_total = delta_progress_counts(stats, previous_total)
            self._report(
                progress,
                "diff+compress",
                processed,
                estimated_total,
                "keys",
                raw.snapshot_id,
            )
            self._checkpoint(cancel_check)

        self._report(
            progress,
            "diff+compress",
            0,
            previous_total,
            "keys",
            raw.snapshot_id,
        )
        with iter_visible_state(db_path) as current:
            object_meta, delta_stats = self.objects.create(
                lambda stream: write_delta(
                    stream,
                    semantic_diff(
                        previous,
                        current,
                        stats,
                        progress=advance if progress is not None or cancel_check is not None else None,
                    ),
                )
            )
        assert isinstance(delta_stats, DeltaStats)
        if delta_stats.records != stats.delta_records:
            raise RuntimeError("semantic diff stats do not match DELTA writer stats")

        self._checkpoint(cancel_check)
        sidecar_meta, _sidecar_stats = self._create_sidecar(
            raw,
            progress=progress,
            cancel_check=cancel_check,
        )
        return (
            SnapshotNode(
                snapshot=raw.snapshot_id,
                world_name=raw.manifest.world_name,
                type="delta",
                object=object_meta,
                sidecar=sidecar_meta,
                state_sha256=stats.state_sha256,
                records=stats.current_records,
                value_bytes=stats.current_value_bytes,
                captured_at=raw.captured_at,
            ),
            (stats.unchanged, stats.changed, stats.inserted, stats.deleted),
        )

    def _create_sidecar(
        self,
        raw: RawSnapshotMetadata,
        *,
        progress: LogicalizeProgress | None,
        cancel_check: CancelCheck | None,
    ) -> tuple[ObjectMetadata, SidecarStats]:
        self._report(progress, "sidecar", None, None, None, raw.snapshot_id)
        self._checkpoint(cancel_check)
        metadata, stats = self.objects.create(
            lambda stream: write_sidecar(stream, raw.path, raw.manifest.sidecar_entries)
        )
        self._checkpoint(cancel_check)
        return metadata, stats

    @staticmethod
    def _report(
        progress: LogicalizeProgress | None,
        detail: str,
        current: int | None,
        total: int | None,
        unit: str | None,
        snapshot: str | None,
    ) -> None:
        if progress is not None:
            progress(detail, current, total, unit, snapshot)

    @staticmethod
    def _checkpoint(cancel_check: CancelCheck | None) -> None:
        if cancel_check is not None:
            cancel_check()
