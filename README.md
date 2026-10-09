# EndKeep

[![Build](https://github.com/ReallocAll/endkeep/actions/workflows/build.yml/badge.svg)](https://github.com/ReallocAll/endkeep/actions/workflows/build.yml)

EndKeep is an incremental world backup plugin for [Endstone](https://github.com/EndstoneMC/endstone).
It backs up Minecraft Bedrock worlds while the server is running, keeps recovery points
without storing a complete world copy each time, and lets you restore an earlier version.

## Installation

**Current public releases** require Linux x86-64, Python 3.14 and Endstone 0.11;
tested on Ubuntu 26.04 LTS. Development CI for the next version also covers
Linux/Windows x86-64 with Python 3.12–3.14. For Python 3.12/3.13, the plugin automatically downloads matching, pinned,
unofficial Amulet-LevelDB compatibility Wheels from our
[compatibility Release](https://github.com/ReallocAll/endkeep/releases/tag/amulet-leveldb-3.0.7a0-compat-cp312-cp313).
These builds are for x86-64 Linux and Windows only. Python 3.14 still uses
upstream PyPI. Production rollout still requires live BDS validation.

1. Download the plugin `.whl` from [Releases](https://github.com/ReallocAll/endkeep/releases/latest).
2. Place it in your server's `plugins/` directory.
3. Restart the server. Endstone installs the required Python packages automatically.

The server needs internet access and compatible dependency packages for the initial installation.
See [Using EndKeep](docs/using-endkeep.md) for details.

## Quick start

Run these commands in the server console or as an operator:

```text
/backup status
/backup create
/backup list
```

`/backup create` takes a snapshot. EndKeep processes pending snapshots during
maintenance, which can also be started manually with `/backup maintenance`.
Automatic backups are enabled by default.

## Features

- **Online backups** — capture snapshots without shutting down your server.
- **Incremental storage** — save changes between recovery points to reduce disk usage.
- **Scheduled backups** — set backup times, retention and maintenance in a configuration file.
- **Verification** — check that stored recovery points can be read correctly.
- **Restore and export** — recover a world even when the server cannot start.

## Storage efficiency

In one 54-hour server test, **14 recovery points used 0.54 GiB** with EndKeep,
compared with **7.40 GiB** for 14 individually compressed full backups
(92.67% less storage). Savings depend on world activity and backup history.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/benchmarks/2026-10-storage/cumulative-storage-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/benchmarks/2026-10-storage/cumulative-storage.svg">
  <img src="docs/benchmarks/2026-10-storage/cumulative-storage.svg" alt="Storage used by 14 EndKeep recovery points compared with independent compressed full backups.">
</picture>

[Benchmark results and methodology](docs/benchmarks/2026-10-storage/benchmark.md)

## Documentation

For server owners:

- [Using EndKeep](docs/using-endkeep.md) — commands and everyday backup management
- [Configuration](docs/configuration.md) — schedules, storage and retention
- [Recovery](docs/recovery.md) — export and offline restore

For contributors:

- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

## License

EndKeep's source code is [MIT licensed](LICENSE). Its dependencies have separate
terms. In particular, [Amulet-LevelDB](https://github.com/Amulet-Team/Amulet-LevelDB/blob/3.0.7a0/LICENSE)
has restrictions affecting some commercial uses. Check the dependency license
before using EndKeep on a commercial server.
