from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from endstone_endkeep.staging.metadata import load_raw_snapshot

from .manifest import ManifestError, ManifestStore, RepositoryManifest
from .objects import ObjectStore

_MANIFEST_RE = re.compile(r"^manifest-(\d{8})\.json$")


@dataclass(frozen=True)
class RecoveryReport:
    recovered_head: int | None
    cleaned_raw_incoming: int
    cleaned_work: int
    cleaned_repo_incoming: int
    removed_committed_raw: int
    pending_raw: tuple[str, ...]


class StartupRecovery:
    """Fast startup recovery; deliberately avoids deep object decompression."""

    def __init__(self, storage_root: Path, manifests: ManifestStore, objects: ObjectStore) -> None:
        self.storage_root = storage_root
        self.raw_root = storage_root / "raw"
        self.work_root = storage_root / "work"
        self.manifests = manifests
        self.objects = objects

    def run(self) -> RecoveryReport:
        cleaned_raw = self._clear_children(self.raw_root / ".incoming")
        cleaned_work = self._clear_children(self.work_root)
        cleaned_repo = self._clear_children(self.manifests.repo_root / ".incoming")
        cleaned_repo += self._clean_transaction_parts()

        manifest, recovered_head = self._recover_authoritative_manifest()
        committed = set() if manifest is None else {node.snapshot for node in manifest.chain}

        removed_committed = 0
        pending: list[tuple[str, str]] = []
        if self.raw_root.exists():
            for path in self.raw_root.iterdir():
                if not path.is_dir() or path.name == ".incoming":
                    continue
                if path.name in committed:
                    shutil.rmtree(path)
                    removed_committed += 1
                    continue
                try:
                    metadata = load_raw_snapshot(path)
                    pending.append((metadata.captured_at, metadata.snapshot_id))
                except Exception:
                    # Do not delete an uncommitted raw snapshot merely because its
                    # metadata is malformed; this needs administrator attention.
                    pending.append(("", path.name))

        pending.sort()
        return RecoveryReport(
            recovered_head=recovered_head,
            cleaned_raw_incoming=cleaned_raw,
            cleaned_work=cleaned_work,
            cleaned_repo_incoming=cleaned_repo,
            removed_committed_raw=removed_committed,
            pending_raw=tuple(snapshot for _, snapshot in pending),
        )

    def _recover_authoritative_manifest(self) -> tuple[RepositoryManifest | None, int | None]:
        head_generation: int | None = None
        if self.manifests.head_path.exists():
            try:
                head_generation = int(self.manifests.head_path.read_text(encoding="ascii").strip())
            except OSError, ValueError:
                head_generation = None

        generations = self._generation_numbers()
        if not generations:
            if self.manifests.head_path.exists():
                raise ManifestError("HEAD is invalid and no valid manifest generation exists")
            return None, None

        # HEAD, not the newest manifest file, is the commit point. A crashed
        # transaction may leave a complete generation on disk without publishing it.
        # Preserve the old recovery chain and discard those unpublished manifests.
        if head_generation is not None:
            try:
                current = self.manifests.load_generation(head_generation)
                if self._manifest_structurally_valid(current):
                    self.manifests.discard_unpublished()
                    return current, None
            except ManifestError, OSError, ValueError:
                pass

        # Only recover from the newest structurally valid generation when HEAD
        # itself is missing, unreadable, or references invalid data.
        for generation in reversed(generations):
            try:
                candidate = self.manifests.load_generation(generation)
            except Exception:
                continue
            if not self._manifest_structurally_valid(candidate):
                continue
            if head_generation != generation:
                self._write_head(generation)
                return candidate, generation
            return candidate, None

        raise ManifestError("repository contains manifests but none are structurally valid")

    def _manifest_structurally_valid(self, manifest: RepositoryManifest) -> bool:
        try:
            manifest.validate_chain()
            for node in manifest.chain:
                for metadata in (node.object, node.sidecar):
                    path = self.objects.path_for(metadata.logical_sha256)
                    if not path.is_file() or path.stat().st_size != metadata.compressed_bytes:
                        return False
            return True
        except Exception:
            return False

    def _generation_numbers(self) -> list[int]:
        if not self.manifests.manifests_root.exists():
            return []
        result = []
        for path in self.manifests.manifests_root.iterdir():
            match = _MANIFEST_RE.match(path.name)
            if match:
                result.append(int(match.group(1)))
        return sorted(result)

    def _write_head(self, generation: int) -> None:
        self.manifests.repo_root.mkdir(parents=True, exist_ok=True)
        part = self.manifests.repo_root / "HEAD.recovery.part"
        part.unlink(missing_ok=True)
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            payload = f"{generation}\n".encode("ascii")
            os.write(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(part, self.manifests.head_path)
        ManifestStore.fsync_directory(self.manifests.repo_root)

    def _clean_transaction_parts(self) -> int:
        count = 0
        candidates = [
            self.manifests.repo_root / "HEAD.part",
            self.manifests.repo_root / "HEAD.recovery.part",
        ]
        if self.manifests.manifests_root.exists():
            candidates.extend(self.manifests.manifests_root.glob("manifest-*.json.part"))
        for path in candidates:
            if path.exists():
                path.unlink()
                count += 1
        return count

    @staticmethod
    def _clear_children(path: Path) -> int:
        if not path.exists():
            return 0
        count = 0
        for child in path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink(missing_ok=True)
            count += 1
        return count
