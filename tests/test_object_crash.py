from __future__ import annotations

from pathlib import Path

import pytest

from endstone_endkeep.logical.format import write_base
from endstone_endkeep.repository.manifest import ManifestStore
from endstone_endkeep.repository.objects import ObjectStore
from endstone_endkeep.repository.recovery import StartupRecovery


class InjectedCrash(RuntimeError):
    pass


@pytest.mark.parametrize(
    ("point", "published"),
    [
        ("after_object_fsync", False),
        ("after_object_rename", True),
        ("after_object_dir_fsync", True),
    ],
)
def test_object_crash_leaves_no_partial_authoritative_state(
    tmp_path: Path,
    point: str,
    published: bool,
) -> None:
    storage = tmp_path / "backups"
    repo = storage / "repo"

    def hook(current: str) -> None:
        if current == point:
            raise InjectedCrash(point)

    objects = ObjectStore(
        repo,
        compression_level=6,
        compression_threads=1,
        fault_hook=hook,
    )
    with pytest.raises(InjectedCrash):
        objects.create(lambda stream: write_base(stream, [(b"a", b"1")]))

    normal_objects = ObjectStore(repo, compression_level=6, compression_threads=1)
    StartupRecovery(storage, ManifestStore(repo), normal_objects).run()
    published_files = list((repo / "objects").glob("*/*.zst"))
    assert bool(published_files) is published
    assert not list((repo / ".incoming").glob("*"))
    assert not (repo / "HEAD").exists()
