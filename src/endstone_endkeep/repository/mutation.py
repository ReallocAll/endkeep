from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from endstone_endkeep.logical.diff import DiffStats, semantic_diff
from endstone_endkeep.logical.format import write_delta
from endstone_endkeep.logical.merge import hash_state

from .manifest import ManifestStore, RepositoryManifest
from .objects import ObjectStore
from .reader import RepositoryReader
from .rollover import Rollover
from .transaction import RepositoryTransaction


class SnapshotMutator:
    """Offline-only snapshot edits. The caller must hold the repository lock."""

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

    def delete(self, manifest: RepositoryManifest, snapshot: str) -> RepositoryManifest:
        self._assert_current(manifest)
        index = self.reader._index_of(manifest, snapshot)
        if len(manifest.chain) == 1:
            raise ValueError("cannot delete the only recovery point")

        if index == 0:
            return self._rollover(manifest, 1)

        if index == len(manifest.chain) - 1:
            updated = RepositoryManifest(manifest.generation + 1, manifest.chain[:-1])
        else:
            previous = manifest.chain[index - 1]
            successor = manifest.chain[index + 1]
            stats = DiffStats()

            # Both iterators are streaming. No complete LevelDB state is held in memory.
            object_meta, _delta_stats = self.objects.create(
                lambda stream: write_delta(
                    stream,
                    semantic_diff(
                        self.reader.iter_state(manifest, snapshot=previous.snapshot),
                        self.reader.iter_state(manifest, snapshot=successor.snapshot),
                        stats,
                    ),
                )
            )
            self._check_stats(
                successor,
                stats.state_sha256,
                stats.current_records,
                stats.current_value_bytes,
            )
            self.objects.verify(object_meta)

            bridge = replace(successor, object=object_meta)
            updated = RepositoryManifest(
                manifest.generation + 1,
                (*manifest.chain[:index], bridge, *manifest.chain[index + 2 :]),
            )

            # Replay the newly built chain, not the original successor delta.
            replay = hash_state(self.reader.iter_state(updated, snapshot=successor.snapshot))
            self._check_stats(successor, replay.sha256, replay.records, replay.value_bytes)

        RepositoryTransaction(self.manifests, fault_hook=self.fault_hook).commit(updated)
        return updated

    def rollover(self, manifest: RepositoryManifest, snapshot: str) -> RepositoryManifest:
        self._assert_current(manifest)
        index = self.reader._index_of(manifest, snapshot)
        if index == 0:
            raise ValueError("selected snapshot is already the BASE; no older recovery points to discard")
        return self._rollover(manifest, index)

    def _rollover(self, manifest: RepositoryManifest, index: int) -> RepositoryManifest:
        updated, _result = Rollover(
            self.manifests,
            self.objects,
            fault_hook=self.fault_hook,
        ).run(manifest, index)
        # Rollover verifies the streamed BASE identity before publication.
        return updated

    def _assert_current(self, manifest: RepositoryManifest) -> None:
        if self.manifests.load_current() != manifest:
            raise RuntimeError("repository HEAD changed since the mutation was planned")

    @staticmethod
    def _check_stats(node, digest: str, records: int, value_bytes: int) -> None:
        if (
            digest != node.state_sha256
            or records != node.records
            or value_bytes != node.value_bytes
        ):
            raise RuntimeError(
                f"rebuilt state does not match snapshot {node.snapshot}: "
                f"sha256={digest} records={records} value_bytes={value_bytes}"
            )
