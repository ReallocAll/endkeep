from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from endstone_endkeep.logical.amulet_reader import iter_visible_state
from endstone_endkeep.logical.diff import DiffStats, semantic_diff
from endstone_endkeep.logical.format import BaseStats, DeltaStats, write_base, write_delta
from endstone_endkeep.logical.sidecar import SidecarStats, write_sidecar
from endstone_endkeep.staging.clone import clone_world, remove_processing_clone
from endstone_endkeep.staging.metadata import RawSnapshotMetadata, load_raw_snapshot

from .manifest import ManifestStore, RepositoryManifest, SnapshotNode
from .objects import ObjectMetadata, ObjectStore
from .reader import RepositoryReader
from .transaction import RepositoryTransaction


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

    def logicalize(self, raw_path: Path) -> LogicalizeResult:
        started = time.monotonic()
        raw = load_raw_snapshot(raw_path)
        current_manifest = self.manifests.load_current()

        clone_world(raw.path, raw.manifest.world_name, self.work_root, raw.snapshot_id)
        db_path = self.work_root / raw.snapshot_id / raw.manifest.world_name / "db"
        try:
            if current_manifest is None:
                node, diff_counts = self._create_base(raw, db_path)
                new_chain = (node,)
            else:
                if any(existing.snapshot == raw.snapshot_id for existing in current_manifest.chain):
                    raise ValueError(f"snapshot already committed: {raw.snapshot_id}")
                node, diff_counts = self._create_delta(raw, db_path, current_manifest)
                new_chain = (*current_manifest.chain, node)

            generation = 1 if current_manifest is None else current_manifest.generation + 1
            new_manifest = RepositoryManifest(generation=generation, chain=tuple(new_chain))
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
    ) -> tuple[SnapshotNode, tuple[int, int, int, int]]:
        with iter_visible_state(db_path) as current:
            object_meta, stats = self.objects.create(lambda stream: write_base(stream, current))
        assert isinstance(stats, BaseStats)
        sidecar_meta, _sidecar_stats = self._create_sidecar(raw)
        return (
            SnapshotNode(
                snapshot=raw.snapshot_id,
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
    ) -> tuple[SnapshotNode, tuple[int, int, int, int]]:
        stats = DiffStats()
        previous = self.reader.iter_state(current_manifest)
        with iter_visible_state(db_path) as current:
            object_meta, delta_stats = self.objects.create(
                lambda stream: write_delta(stream, semantic_diff(previous, current, stats))
            )
        assert isinstance(delta_stats, DeltaStats)
        if delta_stats.records != stats.delta_records:
            raise RuntimeError("semantic diff stats do not match DELTA writer stats")

        sidecar_meta, _sidecar_stats = self._create_sidecar(raw)
        return (
            SnapshotNode(
                snapshot=raw.snapshot_id,
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
    ) -> tuple[ObjectMetadata, SidecarStats]:
        metadata, stats = self.objects.create(
            lambda stream: write_sidecar(stream, raw.path, raw.manifest.sidecar_entries)
        )
        return metadata, stats
