from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from .manifest import ManifestStore, RepositoryManifest


FaultHook = Callable[[str], None]


class RepositoryTransaction:
    """Publish a manifest generation and HEAD after all referenced objects are durable."""

    def __init__(self, store: ManifestStore, fault_hook: FaultHook | None = None) -> None:
        self.store = store
        self.fault_hook = fault_hook or (lambda _point: None)

    def commit(self, manifest: RepositoryManifest) -> None:
        self.store.prepare()
        expected = self.store.next_generation()
        if manifest.generation != expected:
            raise ValueError(
                f"manifest generation must be next authoritative generation: {manifest.generation} != {expected}"
            )

        manifest_path = self.store.path_for(manifest.generation)
        manifest_part = manifest_path.with_suffix(".json.part")
        if manifest_path.exists() or manifest_part.exists():
            raise FileExistsError(f"manifest generation already exists: {manifest.generation}")

        self._write_fsync(manifest_part, self.store.encode(manifest))
        self.fault_hook("after_manifest_fsync")
        os.rename(manifest_part, manifest_path)
        self.fault_hook("after_manifest_rename")
        ManifestStore.fsync_directory(self.store.manifests_root)
        self.fault_hook("after_manifest_dir_fsync")

        head_part = self.store.repo_root / "HEAD.part"
        if head_part.exists():
            head_part.unlink()
        self._write_fsync(head_part, f"{manifest.generation}\n".encode("ascii"))
        self.fault_hook("after_head_fsync")
        os.replace(head_part, self.store.head_path)
        ManifestStore.fsync_directory(self.store.repo_root)
        self.fault_hook("after_commit")

    @staticmethod
    def _write_fsync(path: Path, payload: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError(f"short write to {path}")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
