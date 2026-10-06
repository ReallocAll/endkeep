from __future__ import annotations

from dataclasses import dataclass

from endstone_endkeep.logical.merge import hash_state

from .gc import referenced_object_hashes
from .manifest import ManifestStore, RepositoryManifest
from .objects import ObjectMetadata, ObjectStore
from .reader import RepositoryReader


class VerificationError(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifyReport:
    generation: int | None
    snapshots: int
    referenced_objects: int
    orphan_objects: int
    deep: bool


class RepositoryVerifier:
    def __init__(self, manifests: ManifestStore, objects: ObjectStore) -> None:
        self.manifests = manifests
        self.objects = objects
        self.reader = RepositoryReader(objects)

    def verify(self, *, deep: bool = False) -> VerifyReport:
        manifest = self.manifests.load_current()
        if manifest is None:
            return VerifyReport(None, 0, 0, self._count_object_files(), deep)

        self._verify_structural(manifest)
        referenced = referenced_object_hashes(manifest)
        existing = {path.stem for path in self.objects.objects_root.glob("*/*.zst")}
        orphans = existing - referenced

        if deep:
            self._verify_deep(manifest)

        return VerifyReport(
            generation=manifest.generation,
            snapshots=len(manifest.chain),
            referenced_objects=len(referenced),
            orphan_objects=len(orphans),
            deep=deep,
        )

    def _verify_structural(self, manifest: RepositoryManifest) -> None:
        manifest.validate_chain()
        for node in manifest.chain:
            self._verify_metadata(node.object)
            self._verify_metadata(node.sidecar)

    def _verify_metadata(self, metadata: ObjectMetadata) -> None:
        if metadata.logical_bytes < 0 or metadata.compressed_bytes <= 0:
            raise VerificationError("object metadata contains invalid sizes")
        path = self.objects.path_for(metadata.logical_sha256)
        if not path.is_file():
            raise VerificationError(f"referenced object is missing: {metadata.logical_sha256}")
        if path.stat().st_size != metadata.compressed_bytes:
            raise VerificationError(
                f"compressed size mismatch for {metadata.logical_sha256}: "
                f"{path.stat().st_size} != {metadata.compressed_bytes}"
            )

    def _verify_deep(self, manifest: RepositoryManifest) -> None:
        checked: set[str] = set()
        for node in manifest.chain:
            for metadata in (node.object, node.sidecar):
                if metadata.logical_sha256 not in checked:
                    self.objects.verify(metadata)
                    checked.add(metadata.logical_sha256)

        for node in manifest.chain:
            stats = hash_state(self.reader.iter_state(manifest, snapshot=node.snapshot))
            if (
                stats.sha256 != node.state_sha256
                or stats.records != node.records
                or stats.value_bytes != node.value_bytes
            ):
                raise VerificationError(f"state digest mismatch at snapshot {node.snapshot}")

    def _count_object_files(self) -> int:
        if not self.objects.objects_root.exists():
            return 0
        return sum(1 for _ in self.objects.objects_root.glob("*/*.zst"))
