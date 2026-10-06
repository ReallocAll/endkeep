from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack

from endstone_endkeep.logical.format import iter_base, iter_delta
from endstone_endkeep.logical.merge import apply_delta

from .manifest import RepositoryManifest, SnapshotNode
from .objects import ObjectStore


class RepositoryReader:
    """Streaming materialization of authoritative logical state from BASE + DELTAs."""

    def __init__(self, objects: ObjectStore) -> None:
        self.objects = objects

    def iter_state(
        self,
        manifest: RepositoryManifest,
        *,
        snapshot: str | None = None,
    ) -> Iterator[tuple[bytes, bytes]]:
        if snapshot is None:
            target_index = len(manifest.chain) - 1
        else:
            target_index = self._index_of(manifest, snapshot)

        with ExitStack() as stack:
            base = manifest.chain[0]
            base_stream = stack.enter_context(self.objects.open_logical(base.object))
            state = iter_base(base_stream)

            for node in manifest.chain[1 : target_index + 1]:
                delta_stream = stack.enter_context(self.objects.open_logical(node.object))
                state = apply_delta(state, iter_delta(delta_stream))

            yield from state

    @staticmethod
    def _index_of(manifest: RepositoryManifest, snapshot: str) -> int:
        for index, node in enumerate(manifest.chain):
            if node.snapshot == snapshot:
                return index
        raise KeyError(f"snapshot not found in repository: {snapshot}")

    @staticmethod
    def node_for(manifest: RepositoryManifest, snapshot: str | None = None) -> SnapshotNode:
        if snapshot is None:
            return manifest.chain[-1]
        for node in manifest.chain:
            if node.snapshot == snapshot:
                return node
        raise KeyError(f"snapshot not found in repository: {snapshot}")
