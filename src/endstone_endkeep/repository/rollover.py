from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from endstone_endkeep.logical.format import BaseStats, write_base

from .manifest import ManifestStore, RepositoryManifest, SnapshotNode
from .objects import ObjectStore
from .reader import RepositoryReader
from .transaction import RepositoryTransaction


@dataclass(frozen=True)
class RolloverResult:
    old_generation: int
    new_generation: int
    absorbed_snapshots: tuple[str, ...]
    new_base_snapshot: str
    logical_bytes: int
    compressed_bytes: int


class Rollover:
    def __init__(
        self,
        manifests: ManifestStore,
        objects: ObjectStore,
        *,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.manifests = manifests
        self.objects = objects
        self.reader = RepositoryReader(objects)
        self.fault_hook = fault_hook

    def run(self, manifest: RepositoryManifest, first_retained_index: int) -> tuple[RepositoryManifest, RolloverResult]:
        if first_retained_index <= 0:
            raise ValueError("rollover requires a non-empty prefix to absorb")
        if first_retained_index >= len(manifest.chain):
            raise ValueError("rollover must retain at least one snapshot")

        target = manifest.chain[first_retained_index]
        state = self.reader.iter_state(manifest, snapshot=target.snapshot)
        object_meta, stats = self.objects.create(lambda stream: write_base(stream, state))
        assert isinstance(stats, BaseStats)

        if (
            stats.state_sha256 != target.state_sha256
            or stats.records != target.records
            or stats.value_bytes != target.value_bytes
        ):
            raise RuntimeError("rollover materialized state does not match retained snapshot identity")

        new_base = SnapshotNode(
            snapshot=target.snapshot,
            type="base",
            object=object_meta,
            sidecar=target.sidecar,
            state_sha256=target.state_sha256,
            records=target.records,
            value_bytes=target.value_bytes,
            captured_at=target.captured_at,
        )
        new_manifest = RepositoryManifest(
            generation=manifest.generation + 1,
            chain=(new_base, *manifest.chain[first_retained_index + 1 :]),
        )
        RepositoryTransaction(self.manifests, fault_hook=self.fault_hook).commit(new_manifest)

        return (
            new_manifest,
            RolloverResult(
                old_generation=manifest.generation,
                new_generation=new_manifest.generation,
                absorbed_snapshots=tuple(node.snapshot for node in manifest.chain[:first_retained_index]),
                new_base_snapshot=target.snapshot,
                logical_bytes=object_meta.logical_bytes,
                compressed_bytes=object_meta.compressed_bytes,
            ),
        )
