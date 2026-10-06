from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .manifest import RepositoryManifest
from .objects import ObjectStore


@dataclass(frozen=True)
class GcResult:
    removed_objects: int
    removed_bytes: int
    retained_objects: int


def referenced_object_hashes(manifest: RepositoryManifest) -> set[str]:
    referenced: set[str] = set()
    for node in manifest.chain:
        referenced.add(node.object.logical_sha256)
        referenced.add(node.sidecar.logical_sha256)
    return referenced


def collect_orphan_objects(objects: ObjectStore, manifest: RepositoryManifest) -> GcResult:
    referenced = referenced_object_hashes(manifest)
    removed = 0
    removed_bytes = 0
    retained = 0
    modified_dirs: set[Path] = set()

    if not objects.objects_root.exists():
        return GcResult(0, 0, 0)

    for path in objects.objects_root.glob("*/*.zst"):
        logical_hash = path.stem
        if logical_hash in referenced:
            retained += 1
            continue
        size = path.stat().st_size
        path.unlink()
        removed += 1
        removed_bytes += size
        modified_dirs.add(path.parent)

    for directory in sorted(modified_dirs):
        ObjectStore._fsync_directory(directory)
        try:
            directory.rmdir()
        except OSError:
            pass
    if modified_dirs:
        ObjectStore._fsync_directory(objects.objects_root)

    return GcResult(removed, removed_bytes, retained)
