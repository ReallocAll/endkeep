from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Protocol

from .model import SnapshotEntry, SnapshotManifest


class QueryManifestError(ValueError):
    """Raised when BDS save query output cannot be interpreted safely."""


class _TranslatableLike(Protocol):
    @property
    def text(self) -> str: ...

    @property
    def params(self) -> list[str]: ...


class QueryManifestParser:
    """Parse the file:size list emitted by BDS save query without localizing messages."""

    @classmethod
    def parse_messages(cls, messages: Iterable[str | _TranslatableLike]) -> SnapshotManifest:
        candidates: list[str] = []
        for message in messages:
            if isinstance(message, str):
                candidates.append(message)
                continue
            text = getattr(message, "text", None)
            params = getattr(message, "params", None)
            if isinstance(text, str):
                candidates.append(text)
            if isinstance(params, list):
                candidates.extend(param for param in params if isinstance(param, str))

        errors: list[str] = []
        for candidate in candidates:
            try:
                return cls.parse(candidate)
            except QueryManifestError as exc:
                errors.append(str(exc))
        detail = errors[-1] if errors else "no textual command output was captured"
        raise QueryManifestError(f"no valid save query manifest found: {detail}")

    @classmethod
    def parse(cls, payload: str) -> SnapshotManifest:
        payload = payload.strip()
        if not payload:
            raise QueryManifestError("empty save query output")

        # Translation parameters may include prose around the actual manifest. Find the
        # first comma-delimited run whose members all end in :<decimal byte count>.
        for fragment in cls._candidate_fragments(payload):
            try:
                return cls._parse_manifest_fragment(fragment)
            except QueryManifestError:
                continue
        raise QueryManifestError("output does not contain a valid relative/path:size manifest")

    @staticmethod
    def _candidate_fragments(payload: str) -> list[str]:
        fragments = [payload]
        for separator in ("\n", "\r"):
            expanded: list[str] = []
            for fragment in fragments:
                expanded.extend(fragment.split(separator))
            fragments = expanded
        return [fragment.strip(" \t[]()") for fragment in fragments if fragment.strip()]

    @classmethod
    def _parse_manifest_fragment(cls, fragment: str) -> SnapshotManifest:
        tokens = [token.strip() for token in fragment.split(",") if token.strip()]
        if not tokens:
            raise QueryManifestError("manifest contains no entries")

        entries: list[SnapshotEntry] = []
        world_name: str | None = None
        seen: set[PurePosixPath] = set()

        for token in tokens:
            if ":" not in token:
                raise QueryManifestError(f"manifest entry has no byte size: {token!r}")
            raw_path, raw_size = token.rsplit(":", 1)
            raw_path = raw_path.strip()
            raw_size = raw_size.strip()
            if not raw_size.isdecimal():
                raise QueryManifestError(f"manifest entry has invalid byte size: {token!r}")

            path = PurePosixPath(raw_path)
            cls._validate_relative_path(path)
            components = path.parts
            if len(components) < 2:
                raise QueryManifestError(f"manifest path has no world-relative component: {raw_path!r}")

            if world_name is None:
                world_name = components[0]
            elif components[0] != world_name:
                raise QueryManifestError("save query manifest spans multiple world prefixes")

            if path in seen:
                raise QueryManifestError(f"duplicate manifest path: {raw_path!r}")
            seen.add(path)
            entries.append(SnapshotEntry(path, int(raw_size)))

        if world_name is None:
            raise QueryManifestError("manifest contains no entries")
        return SnapshotManifest(world_name=world_name, entries=tuple(entries))

    @staticmethod
    def _validate_relative_path(path: PurePosixPath) -> None:
        raw = str(path)
        if path.is_absolute() or raw.startswith(("/", "\\")):
            raise QueryManifestError(f"absolute path is forbidden: {raw!r}")
        if not path.parts or any(part in ("", ".", "..") for part in path.parts):
            raise QueryManifestError(f"unsafe relative path: {raw!r}")
        if "\\" in raw:
            raise QueryManifestError(f"backslash path is forbidden in query manifest: {raw!r}")
