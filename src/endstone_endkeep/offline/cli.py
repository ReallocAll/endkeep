from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from ..logical.amulet_reader import iter_visible_state, write_fresh_leveldb
from ..logical.merge import hash_state
from ..logical.sidecar import extract_sidecar
from ..repository.gc import referenced_object_hashes
from ..repository.lock import RepositoryLock
from ..repository.manifest import ManifestStore, RepositoryManifest, SnapshotNode
from ..repository.mutation import SnapshotMutator
from ..repository.objects import ObjectMetadata, ObjectStore
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


class _ProgressBar:
    """Low-noise standard-library progress output for wheel and standalone CLIs."""

    def __init__(self, *, total: int, desc: str, unit: str, unit_scale: bool = False) -> None:
        self.total = total
        self.desc = desc
        self.unit = unit
        self.unit_scale = unit_scale
        self.n = 0
        self._last_report = 0.0
        self._reported = False

    def update(self, amount: int) -> None:
        self.n += amount
        now = time.monotonic()
        if now - self._last_report >= 1.0 or (self.total and self.n >= self.total):
            self._report(now)

    def _report(self, now: float) -> None:
        count = f"{self.n:,}"
        total = f"{self.total:,}"
        if self.unit_scale and self.n >= 1000:
            count = f"{self.n / 1000:.1f}k"
            total = f"{self.total / 1000:.1f}k"
        percent = f" ({self.n / self.total * 100:.1f}%)" if self.total else ""
        print(f"{self.desc}: {count}/{total} {self.unit}{percent}", file=sys.stderr, flush=True)
        self._last_report = now
        self._reported = True

    def close(self) -> None:
        if not self._reported or self.n < self.total:
            self._report(time.monotonic())


def _new_progress_bar(*, total: int, desc: str, unit: str, unit_scale: bool = False) -> _ProgressBar:
    return _ProgressBar(total=total, desc=desc, unit=unit, unit_scale=unit_scale)


def _progress_write(message: str) -> None:
    print(message, file=sys.stderr)


def _stage(index: int, total: int, message: str) -> None:
    print(f"[{index}/{total}] {message}", file=sys.stderr)


def _stage_done() -> None:
    print("done", file=sys.stderr)


def _verbose(message: str, *, enabled: bool) -> None:
    if enabled:
        _progress_write(f"  {message}")


def _unique_referenced_objects(manifest: RepositoryManifest) -> list[ObjectMetadata]:
    result: list[ObjectMetadata] = []
    checked: set[str] = set()
    for node in manifest.chain:
        for metadata in (node.object, node.sidecar):
            if metadata.logical_sha256 in checked:
                continue
            checked.add(metadata.logical_sha256)
            result.append(metadata)
    return result


def _restore_dependencies(
    manifest: RepositoryManifest,
    target: SnapshotNode,
) -> list[tuple[SnapshotNode, str, ObjectMetadata]]:
    target_index = next(index for index, node in enumerate(manifest.chain) if node.snapshot == target.snapshot)
    result: list[tuple[SnapshotNode, str, ObjectMetadata]] = []
    checked: set[str] = set()

    for node in manifest.chain[: target_index + 1]:
        metadata = node.object
        if metadata.logical_sha256 in checked:
            continue
        checked.add(metadata.logical_sha256)
        result.append((node, node.type.upper(), metadata))

    if target.sidecar.logical_sha256 not in checked:
        result.append((target, "SIDECAR", target.sidecar))

    return result


def _verify_restore_dependencies(
    manifest: RepositoryManifest,
    objects: ObjectStore,
    target: SnapshotNode,
    *,
    verbose: bool,
    show_progress: bool,
) -> None:
    """Deep-verify only the immutable objects required to restore one snapshot."""

    manifest.validate_chain()
    required = _restore_dependencies(manifest, target)
    bar = None
    if show_progress:
        _stage(1, 5, "Verifying required objects")
        bar = _new_progress_bar(
            total=len(required),
            desc="Verifying objects",
            unit="obj",
        )

    try:
        for node, role, metadata in required:
            started = time.perf_counter()
            _verbose(
                f"{role} snapshot={node.snapshot} object={metadata.logical_sha256} "
                f"logical_bytes={metadata.logical_bytes} compressed_bytes={metadata.compressed_bytes}",
                enabled=verbose,
            )
            objects.verify(metadata)
            if bar is not None:
                bar.update(1)
            _verbose(
                f"verified object={metadata.logical_sha256[:16]} elapsed={time.perf_counter() - started:.3f}s",
                enabled=verbose,
            )
    finally:
        if bar is not None:
            bar.close()


def command_mutation(
    repo: Path,
    operation: str,
    snapshot: str,
    *,
    yes: bool = False,
) -> int:
    manifests, objects, reader = _runtime(repo)
    with RepositoryLock(repo):
        manifest = _load_manifest(manifests)
        index = reader._index_of(manifest, snapshot)
        if operation == "delete":
            if len(manifest.chain) == 1:
                raise ValueError("cannot delete the only recovery point")
            removed = (snapshot,)
            if index == 0:
                detail = f"replace BASE with {manifest.chain[1].snapshot}"
            elif index == len(manifest.chain) - 1:
                detail = "remove tail DELTA"
            else:
                detail = f"bridge DELTA {manifest.chain[index - 1].snapshot} -> {manifest.chain[index + 1].snapshot}"
        elif operation == "rollover":
            if index == 0:
                raise ValueError("selected snapshot is already the BASE")
            removed = tuple(node.snapshot for node in manifest.chain[:index])
            detail = f"promote {snapshot} to BASE; discard {len(removed)} earlier recovery point(s)"
        else:
            raise ValueError(f"unknown repository mutation: {operation}")

        print(f"EndKeep offline {operation}")
        print(f"repository={repo} generation={manifest.generation} snapshots={len(manifest.chain)}")
        print(f"target={snapshot} type={manifest.chain[index].type.upper()}")
        print(f"plan={detail}")
        print(f"unavailable_after_commit={', '.join(removed)}")
        print("GC=not requested (unreferenced objects remain on disk)")
        if not yes:
            if not sys.stdin.isatty():
                raise RuntimeError("mutation requires interactive confirmation or explicit --yes")
            try:
                answer = input("Type 'yes' to commit this repository mutation: ")
            except EOFError:
                answer = ""
            if answer != "yes":
                print("ABORTED (no changes committed)")
                return 1

        # Cleanup is part of the confirmed transaction, never the preview.
        # HEAD remains authoritative if a prior commit was interrupted.
        manifests.discard_unpublished()
        mutator = SnapshotMutator(manifests, objects)
        if operation == "delete":
            updated = mutator.delete(manifest, snapshot)
        else:
            updated = mutator.rollover(manifest, snapshot)

        referenced = referenced_object_hashes(updated)
        all_objects = {path.stem for path in objects.objects_root.glob("*/*.zst")}
        orphans = len(all_objects - referenced)

    print("COMMITTED")
    print(f"operation={operation} generation={manifest.generation}->{updated.generation}")
    print(
        f"snapshots={len(manifest.chain)}->{len(updated.chain)} "
        f"base={updated.chain[0].snapshot} tail={updated.chain[-1].snapshot}"
    )
    print(f"unavailable={', '.join(removed)}")
    print(f"orphan_objects={orphans} GC=not_run")
    return 0


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


def command_verify(
    repo: Path,
    *,
    verbose: bool = False,
    show_progress: bool = False,
) -> int:
    manifests, objects, _reader = _runtime(repo)
    with RepositoryLock(repo):
        manifest = _load_manifest(manifests)

        if show_progress:
            print("EndKeep repository verification", file=sys.stderr)
            print(f"Repository: {repo}", file=sys.stderr)
            print(f"Generation: {manifest.generation}", file=sys.stderr)
            print(f"Snapshots: {len(manifest.chain)}", file=sys.stderr)
            print(file=sys.stderr)
            _stage(1, 2, "Verifying objects")

        unique_objects = _unique_referenced_objects(manifest)
        object_bar = (
            _new_progress_bar(
                total=len(unique_objects),
                desc="Verifying objects",
                unit="obj",
            )
            if show_progress
            else None
        )
        object_started: dict[str, float] = {}
        object_completed: set[str] = set()
        state_bar = None
        state_snapshot: str | None = None
        state_started = 0.0
        state_stage_printed = False

        def on_object(
            node: SnapshotNode,
            role: str,
            metadata: ObjectMetadata,
            current: int,
            total: int,
        ) -> None:
            key = metadata.logical_sha256
            if current == 0:
                object_started[key] = time.perf_counter()
                _verbose(
                    f"{role} snapshot={node.snapshot} object={key} "
                    f"logical_bytes={metadata.logical_bytes} compressed_bytes={metadata.compressed_bytes}",
                    enabled=verbose,
                )
            if current >= total and key not in object_completed:
                object_completed.add(key)
                if object_bar is not None:
                    object_bar.update(1)
                _verbose(
                    f"verified object={key[:16]} elapsed={time.perf_counter() - object_started.pop(key):.3f}s",
                    enabled=verbose,
                )

        def on_state(node: SnapshotNode, current: int, total: int) -> None:
            nonlocal state_bar, state_snapshot, state_started, state_stage_printed
            if state_snapshot != node.snapshot:
                if state_bar is not None:
                    state_bar.close()
                if show_progress and not state_stage_printed:
                    if object_bar is not None:
                        object_bar.close()
                    print(file=sys.stderr)
                    _stage(2, 2, "Verifying logical states")
                    state_stage_printed = True
                state_snapshot = node.snapshot
                state_started = time.perf_counter()
                _verbose(
                    f"{node.type.upper()} snapshot={node.snapshot} records={node.records} "
                    f"value_bytes={node.value_bytes} state_sha256={node.state_sha256}",
                    enabled=verbose,
                )
                state_bar = (
                    _new_progress_bar(
                        total=total,
                        desc=node.snapshot,
                        unit="rec",
                        unit_scale=True,
                    )
                    if show_progress
                    else None
                )

            if state_bar is not None and current > state_bar.n:
                state_bar.update(current - int(state_bar.n))
            if current >= total:
                if state_bar is not None:
                    state_bar.close()
                    state_bar = None
                _verbose(
                    f"verified state={node.snapshot} elapsed={time.perf_counter() - state_started:.3f}s",
                    enabled=verbose,
                )

        try:
            report = RepositoryVerifier(manifests, objects).verify(
                deep=True,
                object_progress=on_object if show_progress or verbose else None,
                state_progress=on_state if show_progress or verbose else None,
            )
        finally:
            if object_bar is not None:
                object_bar.close()
            if state_bar is not None:
                state_bar.close()

    print("PASS")
    print(
        f"generation={report.generation} snapshots={report.snapshots} "
        f"objects={report.referenced_objects} orphans={report.orphan_objects}"
    )
    return 0


def command_restore(
    repo: Path,
    destination: Path,
    snapshot: str | None,
    *,
    verbose: bool = False,
    show_progress: bool = False,
    preflight: Callable[[SnapshotNode], None] | None = None,
    space_guard: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> int:
    if destination.exists():
        raise FileExistsError(f"restore destination already exists: {destination}")

    manifests, objects, reader = _runtime(repo)
    with RepositoryLock(repo):
        manifest = _load_manifest(manifests)
        node = _resolve_node(manifest, snapshot)
        if preflight is not None:
            preflight(node)

        if show_progress:
            print("EndKeep restore", file=sys.stderr)
            print(f"Snapshot: {node.snapshot}", file=sys.stderr)
            print(f"Captured: {node.captured_at}", file=sys.stderr)
            print(f"World: {node.world_name}", file=sys.stderr)
            print(f"Destination: {destination}", file=sys.stderr)
            print(f"Records: {node.records}", file=sys.stderr)
            print(file=sys.stderr)

        _verify_restore_dependencies(
            manifest,
            objects,
            node,
            verbose=verbose,
            show_progress=show_progress,
        )

        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir()
        try:
            started = time.perf_counter()
            if show_progress:
                print(file=sys.stderr)
                _stage(2, 5, "Restoring SIDECAR")
            with objects.open_logical(node.sidecar) as stream:
                sidecar = extract_sidecar(stream, destination, strip_prefix=node.world_name)
            _verbose(
                f"SIDECAR files={sidecar.files} bytes={sidecar.bytes} elapsed={time.perf_counter() - started:.3f}s",
                enabled=verbose,
            )
            if show_progress:
                _stage_done()

            if space_guard is not None:
                space_guard()
            state = reader.iter_state(manifest, snapshot=node.snapshot)
            if show_progress:
                print(file=sys.stderr)
                _stage(3, 5, "Rebuilding LevelDB")
            started = time.perf_counter()
            rebuild_bar = (
                _new_progress_bar(
                    total=node.records,
                    desc="Rebuilding LevelDB",
                    unit="rec",
                    unit_scale=True,
                )
                if show_progress
                else None
            )
            completed_records = 0

            def on_written(amount: int) -> None:
                nonlocal completed_records
                completed_records += amount
                if rebuild_bar is not None:
                    rebuild_bar.update(amount)
                if progress is not None:
                    progress(completed_records, node.records)
                if space_guard is not None:
                    space_guard()

            callback_enabled = rebuild_bar is not None or progress is not None or space_guard is not None
            try:
                records, value_bytes = write_fresh_leveldb(
                    destination / "db",
                    state,
                    progress=on_written if callback_enabled else None,
                )
            finally:
                if rebuild_bar is not None:
                    rebuild_bar.close()
            _verbose(
                f"rebuilt records={records} value_bytes={value_bytes} elapsed={time.perf_counter() - started:.3f}s",
                enabled=verbose,
            )
            if records != node.records or value_bytes != node.value_bytes:
                raise RuntimeError(
                    f"restored DB stats mismatch: records={records}/{node.records} "
                    f"value_bytes={value_bytes}/{node.value_bytes}"
                )

            if show_progress:
                print(file=sys.stderr)
                _stage(4, 5, "Verifying restored LevelDB")
            started = time.perf_counter()
            verify_bar = (
                _new_progress_bar(
                    total=node.records,
                    desc="Verifying LevelDB",
                    unit="rec",
                    unit_scale=True,
                )
                if show_progress
                else None
            )
            try:
                with iter_visible_state(destination / "db") as reopened:
                    reopened_stats = hash_state(
                        reopened,
                        progress=verify_bar.update if verify_bar is not None else None,
                    )
            finally:
                if verify_bar is not None:
                    verify_bar.close()
            _verbose(
                f"verified records={reopened_stats.records} value_bytes={reopened_stats.value_bytes} "
                f"state_sha256={reopened_stats.sha256} elapsed={time.perf_counter() - started:.3f}s",
                enabled=verbose,
            )
            if (
                reopened_stats.sha256 != node.state_sha256
                or reopened_stats.records != node.records
                or reopened_stats.value_bytes != node.value_bytes
            ):
                raise RuntimeError(
                    f"restored world state digest mismatch: {reopened_stats.sha256} != {node.state_sha256}"
                )

            if show_progress:
                print(file=sys.stderr)
                _stage(5, 5, "Flushing restored world")
            started = time.perf_counter()
            _fsync_tree(destination)
            _verbose(
                f"fsync destination={destination} elapsed={time.perf_counter() - started:.3f}s",
                enabled=verbose,
            )
            if show_progress:
                _stage_done()
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    print("RESTORED")
    print(
        f"snapshot={node.snapshot} world={node.world_name} records={node.records} "
        f"state_sha256={node.state_sha256} destination={destination}"
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
        _fsync_directory(directory)
    _fsync_directory(root.parent)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        if os.name == "posix":
            raise
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            if os.name == "posix":
                raise
    finally:
        os.close(fd)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="endkeep-offline.py",
        description="Offline EndKeep repository verification, restore, and snapshot management tool.",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="Repository path (default: discover plugins/endkeep/config.toml, otherwise backups/repo)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List committed recovery points")
    list_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    verify_parser = subparsers.add_parser("verify", help="Deep-verify objects and logical state digests")
    verify_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show object, BASE/DELTA, byte-count, and timing details",
    )

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
    restore_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show object, BASE/DELTA, byte-count, and timing details",
    )
    for name in ("delete", "rollover"):
        mutation = subparsers.add_parser(
            name,
            help="Delete one snapshot" if name == "delete" else "Advance BASE to a retained snapshot",
        )
        mutation.add_argument("snapshot", help="Exact committed snapshot ID")
        mutation.add_argument(
            "--yes",
            action="store_true",
            help="Confirm without an interactive prompt (required for automation)",
        )
    return parser


def _default_repository() -> Path:
    """Discover the active repository from the server's EndKeep configuration."""
    server_root = Path.cwd()
    config_path = server_root / "plugins" / "endkeep" / "config.toml"
    if config_path.is_file():
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
        storage = config.get("storage", {})
        configured = storage.get("path") if isinstance(storage, dict) else None
        if not isinstance(configured, str) or not configured.strip():
            raise ValueError(f"invalid storage.path in {config_path}")
        storage_root = Path(configured)
        if not storage_root.is_absolute():
            storage_root = server_root / storage_root
        return storage_root / "repo"
    return server_root / "backups" / "repo"


def install_server_launcher(data_folder: Path) -> Path:
    """Expose the wheel CLI from Endstone's private plugin installation prefix."""
    data_folder.mkdir(parents=True, exist_ok=True)
    script = data_folder / "endkeep"
    temporary = data_folder / ".endkeep.tmp"
    site_root = Path(__file__).resolve().parents[2]
    python = Path(sys.executable).resolve()
    contents = (
        f"#!{python}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(site_root)!r})\n"
        "from endstone_endkeep.offline.cli import main\n"
        "raise SystemExit(main())\n"
    )
    temporary.write_text(contents, encoding="utf-8")
    temporary.chmod(0o755)
    os.replace(temporary, script)
    return script


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = (args.repo if args.repo is not None else _default_repository()).resolve()

    if args.command in ("delete", "rollover"):
        return command_mutation(repo, args.command, args.snapshot, yes=args.yes)
    if args.command == "list":
        return command_list(repo, as_json=args.json)
    if args.command == "verify":
        return command_verify(
            repo,
            verbose=args.verbose,
            show_progress=True,
        )
    if args.command == "restore":
        return command_restore(
            repo,
            args.destination.resolve(),
            args.snapshot,
            verbose=args.verbose,
            show_progress=True,
        )
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
