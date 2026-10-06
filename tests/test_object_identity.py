from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path

import zstandard as zstd

from endstone_endkeep.logical.format import write_base
from endstone_endkeep.repository.objects import ObjectStore


def test_existing_logical_object_reuses_nonidentical_zstd_frame(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    store = ObjectStore(repo, compression_level=6, compression_threads=1)
    store.prepare()

    logical = BytesIO()
    write_base(logical, [(b"key", b"value" * 4096)])
    payload = logical.getvalue()
    logical_sha = hashlib.sha256(payload).hexdigest()

    target = store.path_for(logical_sha)
    target.parent.mkdir(parents=True)
    # Deliberately use a valid but byte-different zstd frame. Object identity is
    # the canonical logical SHA, not the compressed representation.
    existing_frame = zstd.ZstdCompressor(level=1, write_checksum=True).compress(payload)
    target.write_bytes(existing_frame)

    metadata, _stats = store.create(
        lambda stream: write_base(stream, [(b"key", b"value" * 4096)])
    )

    assert metadata.logical_sha256 == logical_sha
    assert metadata.compressed_sha256 == hashlib.sha256(existing_frame).hexdigest()
    assert metadata.compressed_bytes == len(existing_frame)
    assert target.read_bytes() == existing_frame
    store.verify(metadata)
