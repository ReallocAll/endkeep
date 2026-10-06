from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from .objects import ObjectMetadata

NodeType = Literal["base", "delta"]


class ManifestError(RuntimeError):
    """Raised when repository manifest state is missing, malformed, or inconsistent."""


def snapshot_order_key(captured_at: str, snapshot: str) -> tuple[float, str]:
    try:
        captured = datetime.fromisoformat(captured_at)
    except ValueError as exc:
        raise ManifestError(f"invalid captured_at timestamp: {captured_at!r}") from exc
    if captured.tzinfo is None:
        captured = captured.astimezone()
    return captured.timestamp(), snapshot


@dataclass(frozen=True)
class SnapshotNode:
    snapshot: str
    world_name: str
    type: NodeType
    object: ObjectMetadata
    sidecar: ObjectMetadata
    state_sha256: str
    records: int
    value_bytes: int
    captured_at: str

    def to_dict(self) -> dict:
        return {
            "snapshot": self.snapshot,
            "world_name": self.world_name,
            "type": self.type,
            "object": self.object.to_dict(),
            "sidecar": self.sidecar.to_dict(),
            "state_sha256": self.state_sha256,
            "records": self.records,
            "value_bytes": self.value_bytes,
            "captured_at": self.captured_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SnapshotNode:
        node_type = str(raw["type"])
        if node_type not in ("base", "delta"):
            raise ManifestError(f"invalid node type: {node_type!r}")
        return cls(
            snapshot=str(raw["snapshot"]),
            world_name=str(raw["world_name"]),
            type=node_type,  # type: ignore[arg-type]
            object=ObjectMetadata.from_dict(raw["object"]),
            sidecar=ObjectMetadata.from_dict(raw["sidecar"]),
            state_sha256=str(raw["state_sha256"]),
            records=int(raw["records"]),
            value_bytes=int(raw["value_bytes"]),
            captured_at=str(raw["captured_at"]),
        )


@dataclass(frozen=True)
class RepositoryManifest:
    generation: int
    chain: tuple[SnapshotNode, ...]
    schema: int = 1

    def __post_init__(self) -> None:
        if self.schema != 1:
            raise ManifestError(f"unsupported repository schema: {self.schema}")
        if self.generation <= 0:
            raise ManifestError("manifest generation must be positive")
        self.validate_chain()

    def validate_chain(self) -> None:
        if not self.chain:
            raise ManifestError("repository chain cannot be empty")
        if self.chain[0].type != "base":
            raise ManifestError("chain[0] must be a BASE")
        if any(node.type != "delta" for node in self.chain[1:]):
            raise ManifestError("chain[1:] must contain only DELTAs")
        snapshots = [node.snapshot for node in self.chain]
        if len(set(snapshots)) != len(snapshots):
            raise ManifestError("repository chain contains duplicate snapshot IDs")
        world_names = {node.world_name for node in self.chain}
        if "" in world_names or len(world_names) != 1:
            raise ManifestError("repository chain must contain exactly one non-empty world_name")

        previous_order: tuple[float, str] | None = None
        for node in self.chain:
            order = snapshot_order_key(node.captured_at, node.snapshot)
            if previous_order is not None and order <= previous_order:
                raise ManifestError("repository chain is not in chronological snapshot order")
            previous_order = order

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "generation": self.generation,
            "chain": [node.to_dict() for node in self.chain],
        }

    @classmethod
    def from_dict(cls, raw: dict) -> RepositoryManifest:
        if not isinstance(raw, dict) or not isinstance(raw.get("chain"), list):
            raise ManifestError("manifest must contain a chain array")
        return cls(
            schema=int(raw["schema"]),
            generation=int(raw["generation"]),
            chain=tuple(SnapshotNode.from_dict(node) for node in raw["chain"]),
        )


class ManifestStore:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.manifests_root = repo_root / "manifests"
        self.head_path = repo_root / "HEAD"

    def prepare(self) -> None:
        repo_created = not self.repo_root.exists()
        manifests_created = not self.manifests_root.exists()
        self.manifests_root.mkdir(parents=True, exist_ok=True)
        if repo_created:
            self.fsync_directory(self.repo_root.parent)
        if manifests_created:
            self.fsync_directory(self.repo_root)

    def load_current(self) -> RepositoryManifest | None:
        if not self.head_path.exists():
            return None
        try:
            generation = int(self.head_path.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as exc:
            raise ManifestError("HEAD is invalid") from exc
        return self.load_generation(generation)

    def load_generation(self, generation: int) -> RepositoryManifest:
        path = self.path_for(generation)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError(f"cannot load manifest generation {generation}") from exc
        manifest = RepositoryManifest.from_dict(raw)
        if manifest.generation != generation:
            raise ManifestError(f"manifest generation mismatch: filename={generation} body={manifest.generation}")
        return manifest

    def path_for(self, generation: int) -> Path:
        return self.manifests_root / f"manifest-{generation:08d}.json"

    def next_generation(self) -> int:
        current = self.load_current()
        return 1 if current is None else current.generation + 1

    @staticmethod
    def encode(manifest: RepositoryManifest) -> bytes:
        return (json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n").encode("utf-8")

    @staticmethod
    def fsync_directory(path: Path) -> None:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
