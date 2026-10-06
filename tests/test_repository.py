from __future__ import annotations

from pathlib import Path

from endstone_endkeep.logical.format import iter_base, write_base
from endstone_endkeep.repository.manifest import ManifestStore, RepositoryManifest, SnapshotNode
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.repository.transaction import RepositoryTransaction


def test_object_store_and_manifest_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    objects = ObjectStore(repo, compression_level=6, compression_threads=1)
    metadata, stats = objects.create(lambda stream: write_base(stream, [(b"a", b"1")]))
    objects.verify(metadata)

    with objects.open_logical(metadata) as stream:
        assert list(iter_base(stream)) == [(b"a", b"1")]

    node = SnapshotNode(
        snapshot="20261006-120000",
        type="base",
        object=metadata,
        sidecar=metadata,
        state_sha256=stats.state_sha256,
        records=stats.records,
        value_bytes=stats.value_bytes,
        captured_at="2026-10-06T12:00:00+08:00",
    )
    store = ManifestStore(repo)
    manifest = RepositoryManifest(generation=1, chain=(node,))
    RepositoryTransaction(store).commit(manifest)

    loaded = store.load_current()
    assert loaded == manifest
    assert (repo / "HEAD").read_text().strip() == "1"
