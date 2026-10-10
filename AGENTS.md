# Working on EndKeep

EndKeep is a Python plugin for Endstone that creates incremental backups of
Minecraft Bedrock worlds. EndKeep targets Python 3.12–3.15
on Linux and Windows x86-64 with Endstone 0.11. For Endstone API usage, see
[Endstone's documentation](https://endstone.dev/).

## Project layout

- `src/endstone_endkeep/plugin.py` — plugin commands and lifecycle.
- `src/endstone_endkeep/config.py` — configuration.
- `src/endstone_endkeep/coordinator.py` and `staging/` — online snapshot capture.
- `src/endstone_endkeep/repository/` and `worker/` — backup storage and background processing.
- `src/endstone_endkeep/offline/` — standalone verification and recovery.
- `tests/` — automated tests.

## Development

```sh
uv venv --python 3.14
uv pip install --python .venv -e ".[dev]"
uv run --no-sync ruff check src tests tools
uv run --no-sync ruff format --check src tests tools
uv run --no-sync pytest
```

## Important constraints

- Backup reliability comes first. Do not discard recovery points when an
  operation fails or when the repository's integrity is uncertain.
- Never overwrite an active world during recovery. Make destructive repository
  changes explicit and test their failure cases.
- Keep expensive backup processing away from the server tick and preserve
  cancellation and reload behavior.
- Prefer small, focused changes and existing APIs over new layers or dependencies.
- Update user documentation for visible behavior changes. Explain what server
  owners need to do; keep internal implementation notes out of usage guides.
- Do not create releases or rewrite version tags unless explicitly requested.
- Python 3.12/3.13/3.15 resolves pinned, SHA256-verified Amulet-LevelDB compatibility
  Wheels from the dedicated GitHub Release. Test a clean EndKeep install without
  pre-installing the dependency. The upstream Amulet version metadata currently prevents regenerating a
  universal uv.lock; prefer `uv pip install` instead of `uv sync`.
- The eight-combination compatibility workflow is not a substitute for
  testing real BDS worlds, especially NTFS crash and directory metadata durability.

See [Contributing](CONTRIBUTING.md) and the
[release checks](docs/manual-validation.md) for the development workflow.
