# Contributing

Bug reports, fixes and documentation improvements are welcome.

EndKeep targets Linux, Python 3.14 and Endstone 0.11. To work on the project:

```sh
git clone https://github.com/ReallocAll/endkeep.git
cd endkeep
uv sync --python 3.14 --extra dev
uv run ruff check src tests tools
uv run ruff format --check src tests tools
uv run pytest
```

Open a pull request with a short explanation of the change and tests for
new behavior. Changes affecting backups or recovery should also be exercised
on a disposable repository; see [Release checks](docs/manual-validation.md).

For bugs, use [GitHub Issues](https://github.com/ReallocAll/endkeep/issues).
For security reports, see [SECURITY.md](SECURITY.md).
