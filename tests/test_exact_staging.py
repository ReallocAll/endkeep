from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest

from endstone_endkeep.bds.model import SnapshotEntry, SnapshotManifest
from endstone_endkeep.staging.exact import ExactStageError, stage_manifest


def test_exact_prefix_copy(tmp_path: Path) -> None:
    source = tmp_path / "worlds"
    file_path = source / "level" / "db" / "active.log"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"authoritative-prefix" + b"mutable-tail")

    manifest = SnapshotManifest(
        "level",
        (SnapshotEntry(PurePosixPath("level/db/active.log"), len(b"authoritative-prefix")),),
    )
    destination = tmp_path / "stage"
    result = stage_manifest(source, destination, manifest)

    assert result.files == 1
    assert result.bytes_copied == len(b"authoritative-prefix")
    assert (destination / "level/db/active.log").read_bytes() == b"authoritative-prefix"


def test_reject_short_source(tmp_path: Path) -> None:
    source = tmp_path / "worlds"
    file_path = source / "level" / "level.dat"
    file_path.parent.mkdir(parents=True)
    file_path.write_bytes(b"abc")
    manifest = SnapshotManifest(
        "level",
        (SnapshotEntry(PurePosixPath("level/level.dat"), 4),),
    )
    with pytest.raises(ExactStageError):
        stage_manifest(source, tmp_path / "stage", manifest)


@pytest.mark.skipif(os.name != "posix", reason="symlink traversal test requires POSIX semantics")
def test_reject_source_symlink(tmp_path: Path) -> None:
    source = tmp_path / "worlds"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "level.dat").write_bytes(b"abc")
    (source / "level").mkdir(parents=True)
    os.symlink(outside, source / "level" / "escape")

    manifest = SnapshotManifest(
        "level",
        (SnapshotEntry(PurePosixPath("level/escape/level.dat"), 3),),
    )
    with pytest.raises(ExactStageError):
        stage_manifest(source, tmp_path / "stage", manifest)
