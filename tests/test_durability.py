from __future__ import annotations

import os
from pathlib import Path

import pytest

import endstone_endkeep.offline.cli as offline_cli
from endstone_endkeep.repository.lock import RepositoryLock
from endstone_endkeep.repository.manifest import ManifestStore
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.staging.raw import RawSnapshotStore


@pytest.mark.skipif(os.name != "posix", reason="POSIX durability contract")
@pytest.mark.parametrize(
    "fsync_directory",
    [
        RawSnapshotStore._fsync_directory,
        ObjectStore._fsync_directory,
        ManifestStore.fsync_directory,
        RepositoryLock._fsync_directory,
    ],
)
def test_directory_fsync_failure_is_fatal_on_posix(tmp_path: Path, monkeypatch, fsync_directory) -> None:
    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        fsync_directory(tmp_path)


def test_restore_fsync_tree_persists_destination_parent(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "restored-world"
    destination.mkdir()
    (destination / "level.dat").write_bytes(b"data")

    directories: list[Path] = []
    monkeypatch.setattr(offline_cli, "_fsync_directory", directories.append)

    offline_cli._fsync_tree(destination)

    assert destination in directories
    assert destination.parent in directories
