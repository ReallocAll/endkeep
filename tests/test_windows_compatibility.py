"""Platform safety regression tests for the Python/Windows compatibility PR."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from endstone_endkeep.bds.model import SnapshotEntry, SnapshotManifest
from endstone_endkeep.staging.clone import CloneError, clone_world
from endstone_endkeep.staging.exact import ExactStageError, stage_manifest
from endstone_endkeep.worker.client import RepositoryWorkerClient


def test_worker_pid_probe_does_not_terminate_this_process() -> None:
    # On Windows, os.kill(os.getpid(), 0) would terminate the test runner.
    assert RepositoryWorkerClient._pid_alive(os.getpid())
    assert not RepositoryWorkerClient._pid_alive(0)


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions only")
def test_windows_junctions_cannot_escape_staging_or_clone(tmp_path: Path) -> None:
    world = tmp_path / "worlds" / "level"
    world.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "level.dat").write_bytes(b"outside")

    junction = world / "escape"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode:
        pytest.skip(f"junction creation unavailable: {created.stderr}")
    assert junction.is_junction()

    manifest = SnapshotManifest(
        "level",
        (SnapshotEntry(PurePosixPath("level/escape/level.dat"), 7),),
    )
    with pytest.raises(ExactStageError):
        stage_manifest(tmp_path / "worlds", tmp_path / "stage", manifest)
    with pytest.raises(CloneError):
        clone_world(tmp_path / "worlds", "level", tmp_path / "work", "snapshot")


def test_binary_staging_and_clone_are_byte_exact(tmp_path: Path) -> None:
    # LevelDB contains arbitrary binary bytes; Windows CRT text mode may
    # truncate at Ctrl-Z or change CRLF unless O_BINARY is explicit.
    source = tmp_path / "source"
    source_file = source / "level" / "db" / "000004.ldb"
    source_file.parent.mkdir(parents=True)
    payload = bytes(range(256)) * 2 + b"\r\n\x1a\x00\xff"
    source_file.write_bytes(payload)

    clone = clone_world(source, "level", tmp_path / "work", "snapshot")
    assert (clone / "db" / "000004.ldb").read_bytes() == payload

    manifest = SnapshotManifest(
        "level",
        (SnapshotEntry(PurePosixPath("level/db/000004.ldb"), len(payload)),),
    )
    stage_manifest(source, tmp_path / "stage", manifest)
    assert (tmp_path / "stage" / "level" / "db" / "000004.ldb").read_bytes() == payload


def test_worker_process_liveness_tracks_real_exit() -> None:
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read(1)"],
        stdin=subprocess.PIPE,
    )
    try:
        assert RepositoryWorkerClient._pid_alive(process.pid)
        assert process.stdin is not None
        process.stdin.write(b"x")
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert not RepositoryWorkerClient._pid_alive(process.pid)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
