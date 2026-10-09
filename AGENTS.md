# Working on EndKeep

EndKeep is a Python plugin for Endstone that creates incremental backups of
Minecraft Bedrock worlds. This repository targets Linux, Python 3.14 and
Endstone 0.11. For Endstone API usage, see [Endstone's documentation](https://endstone.dev/).

## Project layout

- `src/endstone_endkeep/plugin.py` — plugin commands and lifecycle.
- `src/endstone_endkeep/config.py` — configuration.
- `src/endstone_endkeep/coordinator.py` and `staging/` — online snapshot capture.
- `src/endstone_endkeep/repository/` and `worker/` — backup storage and background processing.
- `src/endstone_endkeep/offline/` — standalone verification and recovery.
- `tests/` — automated tests.

## Development

```sh
uv sync --python 3.14 --extra dev
uv run ruff check src tests tools
uv run ruff format --check src tests tools
uv run pytest
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

See [Contributing](CONTRIBUTING.md) and the
[release checks](docs/manual-validation.md) for the development workflow.
