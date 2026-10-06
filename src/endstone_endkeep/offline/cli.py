from __future__ import annotations

import argparse
import json
import os
import shutil
from collections.abc import Sequence
from pathlib import Path

from ..logical.amulet_reader import iter_visible_state, write_fresh_leveldb
from ..logical.merge import hash_state
from ..logical.sidecar import extract_sidecar
from ..repository.lock import RepositoryLock
from ..repository.manifest import ManifestStore, RepositoryManifest, SnapshotNode
from ..repository.objects import ObjectStore
from ..repository.reader import RepositoryReader
from ..repository.verify import RepositoryVerifier


def _runtime(repo: Path) -> tuple[ManifestStore, ObjectStore, RepositoryReader]:
    objects = ObjectStore(repo, compression_level=6, compression_threads=1)
    manifests = ManifestStore(repo)
    return manifests, objects, RepositoryReader(objects)


def _load_manifest(manifests: ManifestStore) -> RepositoryManifest:
    manifest = manifests.load_current()
    if manifest is None:
        raise RuntimeError("repository is empty")
    return manifest


def _resolve_node(manifest: RepositoryManifest, snapshot: str | None) -> SnapshotNode:
    if snapshot is None or snapshot == "latest":
        return manifest.chain[-1]
    for node in manifest.chain:
        if node.snapshot == snapshot:
            return node
    raise KeyError(f"snapshot not found: {snapshot}")


def command_list(repo: Path, *, as_json: bool) -> int:
    manifests, _objects, _reader = _runtime(repo)
    with RepositoryLock(repo):
        manifest = _load_manifest(manifests)
        rows = [
            {
                "snapshot": node.snapshot,
                "world_name": node.world_name,
                "type": node.type,
                "captured_at": node.captured_at,
                "records": node.records,
                "value_bytes": node.value_bytes,
                "state_sha256": node.state_sha256,
            }
            for node in manifest.chain
        ]

    if as_json:
        print(json.dumps({"generation": manifest.generation, "snapshots": rows}, indent=2))
    else:
        print(f"generation={manifest.generation} snapshots={len(rows)}")
        for row in rows:
            print(
                f"{row['snapshot']} {row['type'].upper()} {row['captured_at']} "
                f"records={row['records']} state={row['state_sha256'][:16]}"
            )
    return 0


def command_verify(repo: Path) -> int:
    manifests, objects, _reader = _runtime(repo)
    with RepositoryLock(repo):
        report = RepositoryVerifier(manifests, objects).verify(deep=True)
    print(
        f"PASS generation={report.generation} snapshots={report.snapshots} "
        f"objects={report.referenced_objects} orphans={report.orphan_objects}"
    )
    return 0


def command_restore(repo: Path, destination: Path, snapshot: str | None) -> int:
    if destination.exists():
        raise FileExistsError(f"restore destination already exists: {destination}")

    manifests, objects, reader = _runtime(repo)
    with RepositoryLock(repo):
        manifest = _load_manifest(manifests)
        node = _resolve_node(manifest, snapshot)

        # Verify repository logical integrity before constructing a fresh world.
        RepositoryVerifier(manifests, objects).verify(deep=True)

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir()
        try:
            with objects.open_logical(node.sidecar) as stream:
                extract_sidecar(stream, destination, strip_prefix=node.world_name)

            state = reader.iter_state(manifest, snapshot=node.snapshot)
            records, value_bytes = write_fresh_leveldb(destination / "db", state)
            if records != node.records or value_bytes != node.value_bytes:
                raise RuntimeError(
                    f"restored DB stats mismatch: records={records}/{node.records} "
                    f"value_bytes={value_bytes}/{node.value_bytes}"
                )

            with iter_visible_state(destination / "db") as reopened:
                reopened_stats = hash_state(reopened)
            if (
                reopened_stats.sha256 != node.state_sha256
                or reopened_stats.records != node.records
                or reopened_stats.value_bytes != node.value_bytes
            ):
                raise RuntimeError(
                    f"restored world state digest mismatch: {reopened_stats.sha256} != {node.state_sha256}"
                )

            _fsync_tree(destination)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    print(
        f"RESTORED snapshot={node.snapshot} world={node.world_name} "
        f"records={node.records} state_sha256={node.state_sha256} destination={destination}"
    )
    return 0


def _fsync_tree(root: Path) -> None:
    directories: list[Path] = []
    for current, _dirnames, filenames in os.walk(root):
        current_path = Path(current)
        directories.append(current_path)
        for filename in filenames:
            fd = os.open(current_path / filename, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in reversed(directories):
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            continue
        try:
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="endkeep-offline.py",
        description="Offline EndKeep repository verification and world restore tool.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path("backups/repo"),
        help="Path to EndKeep repo/ directory (default: backups/repo)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List committed recovery points")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    subparsers.add_parser("verify", help="Deep-verify objects and logical state digests")

    restore_parser = subparsers.add_parser("restore", help="Restore a snapshot into a fresh world directory")
    restore_parser.add_argument(
        "destination",
        type=Path,
        help="Fresh destination world directory; it must not already exist",
    )
    restore_parser.add_argument(
        "--snapshot",
        default="latest",
        help="Snapshot ID to restore (default: latest)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = args.repo.resolve()

    if args.command == "list":
        return command_list(repo, as_json=args.json)
    if args.command == "verify":
        return command_verify(repo)
    if args.command == "restore":
        return command_restore(repo, args.destination.resolve(), args.snapshot)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
