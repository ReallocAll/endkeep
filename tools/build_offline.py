from __future__ import annotations

import argparse
import pprint
from pathlib import Path


MODULES = (
    "bds/model.py",
    "logical/format.py",
    "logical/merge.py",
    "logical/sidecar.py",
    "logical/amulet_reader.py",
    "repository/objects.py",
    "repository/manifest.py",
    "repository/reader.py",
    "repository/gc.py",
    "repository/verify.py",
    "repository/lock.py",
    "offline/cli.py",
)

PACKAGES = ("", "bds", "logical", "repository", "offline")
SOURCE_NAMESPACE = "endstone_endkeep"
STANDALONE_NAMESPACE = "_endkeep_standalone"


def build(output: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    package_root = root / "src" / SOURCE_NAMESPACE

    sources: dict[str, str] = {}
    for package in PACKAGES:
        relative = f"{package}/__init__.py" if package else "__init__.py"
        sources[relative] = ""

    for relative in MODULES:
        source = (package_root / relative).read_text(encoding="utf-8")
        sources[relative] = source.replace(SOURCE_NAMESPACE, STANDALONE_NAMESPACE)

    embedded = pprint.pformat(sources, width=120, sort_dicts=True)
    script = f'''#!/usr/bin/env python3
# /// script
# requires-python = ">=3.14"
# dependencies = [
#   "amulet-leveldb==3.0.7a0",
#   "zstandard>=0.23,<1",
# ]
# ///
"""Standalone EndKeep offline repository management tool.

Generated from the EndKeep source tree. It does not require the EndKeep plugin
wheel or Endstone to be installed.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_SOURCES = {embedded}


def _run() -> int:
    with tempfile.TemporaryDirectory(prefix="endkeep-offline-") as temporary:
        root = Path(temporary) / "{STANDALONE_NAMESPACE}"
        for relative, source in _SOURCES.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")

        sys.path.insert(0, temporary)
        try:
            from {STANDALONE_NAMESPACE}.offline.cli import main

            return main()
        finally:
            sys.path.remove(temporary)


if __name__ == "__main__":
    raise SystemExit(_run())
'''
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(script, encoding="utf-8")
    output.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("dist/endkeep-offline.py"))
    args = parser.parse_args()
    build(args.output)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
