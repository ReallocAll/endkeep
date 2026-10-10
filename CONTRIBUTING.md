# Contributing

Bug reports, fixes and documentation improvements are welcome.

EndKeep requires Endstone 0.11 and Python 3.12–3.14.
To work on the project:

```sh
git clone https://github.com/ReallocAll/endkeep.git
cd endkeep
uv venv --python 3.14
uv pip install --python .venv -e ".[dev]"
uv run --no-sync ruff check src tests tools
uv run --no-sync ruff format --check src tests tools
uv run --no-sync pytest
```

Open a pull request with a short explanation of the change and tests for
new behavior. Changes affecting backups or recovery should also be exercised
on a disposable repository; see [Release checks](docs/manual-validation.md).

For bugs, use [GitHub Issues](https://github.com/ReallocAll/endkeep/issues).
For security reports, see [SECURITY.md](SECURITY.md).

The upstream Amulet package currently contains version metadata rejected by
some `uv lock` versions. Use `uv pip install` as above for development.
