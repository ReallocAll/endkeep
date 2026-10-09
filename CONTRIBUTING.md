# Contributing to EndKeep

EndKeep protects Minecraft Bedrock server world data. Changes to repository formats,
capture, retention or restore require regression tests and restore validation.

## Development

Linux, Python 3.14 and Endstone 0.11 are currently supported.

```sh
git clone https://github.com/ReallocAll/endkeep.git
cd endkeep
uv sync --python 3.14 --extra dev
uv run ruff check src tests tools
uv run ruff format --check src tests tools
uv run pytest
```

Create a branch, document any data-safety implications, add tests (including
interrupted writes when relevant), update `CHANGELOG.md` and open a PR.

Report bugs through [EndKeep Issues](https://github.com/ReallocAll/endkeep/issues).
Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
