from __future__ import annotations

import os
from pathlib import Path

import pytest

import endstone_endkeep.offline.cli as offline_cli
from endstone_endkeep.staging.raw import RawSnapshotStore


@pytest.mark.skipif(os.name != "posix", reason="POSIX durability contract")
def test_raw_directory_fsync_failure_is_fatal(tmp_path: Path, monkeypatch) -> None:
    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        RawSnapshotStore._fsync_directory(tmp_path)


def test_restore_fsync_tree_persists_destination_parent(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "restored-world"
    destination.mkdir()
    (destination / "level.dat").write_bytes(b"data")

    directories: list[Path] = []
    monkeypatch.setattr(offline_cli, "_fsync_directory", directories.append)

    offline_cli._fsync_tree(destination)

    assert destination in directories
    assert destination.parent in directories
