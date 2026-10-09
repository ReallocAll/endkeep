# EndKeep

[![Build](https://github.com/ReallocAll/endkeep/actions/workflows/build.yml/badge.svg)](https://github.com/ReallocAll/endkeep/actions/workflows/build.yml)

EndKeep is a crash-safe, incremental world backup plugin for
[Endstone](https://github.com/EndstoneMC/endstone) and Minecraft Bedrock Dedicated Server.
It captures online recovery points and stores changes to the *logical* LevelDB state,
rather than keeping a complete copy of the world each time.

## Install

**Linux only.** Requires Python 3.14 and Endstone 0.11 (`>=0.11,<0.12`). Validated on a live Bedrock server running **Ubuntu 26.04 LTS x86-64**. Automated CI runs on Ubuntu 24.04; that CI runner is not a second live-server validation environment.

Download the latest [EndKeep release](https://github.com/ReallocAll/endkeep/releases/latest),
place the `.whl` file in your server's `plugins/` directory, and start the server.
Endstone installs the plugin dependencies automatically.

The pinned `amulet-leveldb` dependency is a native extension. Installation requires a host that
can obtain a compatible Python 3.14 wheel; restricted/offline panel hosts may need dependency
provisioning by their provider. The default configuration also reserves **5 GiB** of free disk
space (plus room to stage a new snapshot); adjust the reserve for your disk capacity.

## Quick start

EndKeep creates recovery points automatically. Use the server console or an operator account to check them:

```text
/backup status
/backup list
/backup create
```

`/backup create` captures a recovery point; repository processing runs later during maintenance.
See [Using EndKeep](docs/using-endkeep.md) for schedules, configuration and offline restore.

## Features

- **Online snapshots** — short `save hold/query/resume` captures; heavier work runs after saving resumes.
- **Logical incremental storage** — one BASE followed by semantic DELTAs of visible LevelDB key/value data.
- **Crash-safe repository** — immutable compressed objects and atomic manifest publication.
- **Background processing** — a separate worker process keeps repository work outside Endstone's Python interpreter.
- **Offline recovery** — list, verify and restore individual snapshots with the standalone recovery tool.
- **Fail-closed verification** — integrity failures block repository mutations and preserve pending raw snapshots until a deep verification passes.

## Storage efficiency

In a 54-hour live-server test, EndKeep stored **14 recovery points** using **92.67% less space**
than 14 separately compressed full-world backups.

| 14 recovery points | Storage |
| --- | ---: |
| Independent full backups (TAR + Zstd-6) | 7.40 GiB |
| **EndKeep** (1 BASE + 13 DELTAs) | **0.54 GiB** |

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/benchmarks/2026-10-storage/cumulative-storage-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/benchmarks/2026-10-storage/cumulative-storage.svg">
  <img src="docs/benchmarks/2026-10-storage/cumulative-storage.svg" alt="Fourteen Bedrock recovery points: independently compressed full backups reach 7.40 GiB while EndKeep uses about 0.54 GiB, with an inset showing EndKeep's per-snapshot growth.">
</picture>

Both series use GiB; the inset shows EndKeep's per-snapshot growth on a zoomed scale.
[Benchmark methodology and data](docs/benchmarks/2026-10-storage/benchmark.md).

## Documentation

- [Using EndKeep](docs/using-endkeep.md) — schedule, commands, configuration and recovery
- [Storage benchmark](docs/benchmarks/2026-10-storage/benchmark.md) — measurements and source data
- [Manual validation](docs/manual-validation.md) — production smoke tests

## License

EndKeep's own source code is licensed under [MIT](LICENSE). Its dependencies have
separate licenses. In particular, the pinned [Amulet-LevelDB 3.0.7a0](https://github.com/Amulet-Team/Amulet-LevelDB/tree/3.0.7a0)
uses the [Amulet Team License 1.0.0](https://github.com/Amulet-Team/Amulet-LevelDB/blob/3.0.7a0/LICENSE),
which includes restrictions on commercial use. The MIT license on EndKeep does **not**
override the dependency's terms. Review those terms before deployment, particularly
for commercial servers.
