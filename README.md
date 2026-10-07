# EndKeep

[![Build](https://github.com/ReallocAll/endkeep/actions/workflows/build.yml/badge.svg)](https://github.com/ReallocAll/endkeep/actions/workflows/build.yml)

EndKeep is a crash-safe logical incremental world backup plugin for
[Endstone](https://github.com/EndstoneMC/endstone) and Minecraft Bedrock Dedicated Server.

It creates short online recovery-point captures with BDS `save hold/query/resume`, then converts
those raw snapshots during maintenance windows into a compact logical repository based on the
visible Bedrock LevelDB key/value state.

## Requirements

- Python 3.14
- Endstone >= 0.11 and < 0.12
- Bedrock Dedicated Server managed by Endstone
- Amulet-LevelDB 3.0.7a0
- zstandard

## How it works

The latency-sensitive online capture path is deliberately small:

```text
save hold
-> save query
-> copy exactly the files and byte limits reported by BDS
-> save resume
```

Hashing, zstd compression, Amulet LevelDB scans, semantic diffing, retention, rollover, and
repository verification happen after BDS has resumed. Heavy repository work runs in a dedicated
long-lived Python worker process, so it has its own interpreter/GIL and cannot starve Endstone's
embedded Python runtime. The plugin automatically starts or reconnects to this worker; no separate
daemon setup is required.

Raw snapshots are a short-lived write-back queue, not the long-term backup format. Successful
maintenance converts them into:

- **BASE** — complete canonical visible LevelDB state.
- **DELTA** — sorted semantic PUT/DELETE changes relative to the previous logical state.
- **SIDECAR** — every query-manifest file under the world that is not under `world/db/**`.

The repository uses immutable content-addressed objects, generation manifests, and an atomic
`HEAD`. A raw snapshot is deleted only after the new logical objects, manifest generation, and
`HEAD` are durable.

## Default schedule

EndKeep uses the operating system's local time.

Online recovery-point captures:

```text
12:00
16:30
20:30
23:45
```

Maintenance:

```text
06:00  FULL
18:30  LOGIC_ONLY
```

`LOGIC_ONLY` drains all pending raw snapshots into the repository in timestamp order.

`FULL` first performs the same drain, then applies retention, any required logical rollover,
orphan-object GC, stale work cleanup, and repository verification at the configured `verify.mode`.

Missed schedule times are not replayed later. If a maintenance window arrives while repository work
is already active, EndKeep coalesces it into at most one durable pending maintenance slot instead of
dropping it. `FULL` subsumes `LOGIC_ONLY`, and an accepted pending job survives plugin reloads or
server restarts until it can run.

## Configuration

The plugin writes `plugins/endkeep/config.toml` on first start.

```toml
enabled = true

[capture]
times = [
    "12:00",
    "16:30",
    "20:30",
    "23:45",
]

[maintenance]
times = [
    "06:00",
    "18:30",
]

[raw]
max_pending = 18
max_age_days = 3

[logical]
compression_level = 6
compression_threads = 4

[retention]
keep_days = 7
keep_last = 28

[storage]
path = "backups"
min_free_space_gib = 5

[worker]
priority = "background"

[verify]
mode = "normal"
```

Transient `save query` misses are retried internally every 10 server ticks. The capture still has a fixed
15-second query deadline, after which EndKeep enters the fail-safe `save resume` recovery path.

Retention keeps a snapshot when it is **within `keep_days` OR among the last `keep_last`**.
The raw hard limits are safety limits: if the queue cannot be logicalized and a hard limit must be
enforced, EndKeep drops the oldest raw recovery point first so newer player work remains protected.
It still preserves the configured free-space reserve rather than filling the BDS disk.

`worker.priority` selects a normalized operating-system scheduling policy:

- `conservative` — strongest preference for BDS responsiveness; intended for constrained 1-2 core hosts.
- `background` — default; aggressively uses otherwise-idle resources but yields CPU/I/O weight under contention.
- `balanced` — smaller priority penalty when backup completion time matters more.
- `throughput` — normal OS priority for maximum repository throughput; EndKeep never raises itself above BDS.

EndKeep does not depend on Spark/PAPI or MSPT-based throttling. CPU placement and contention are left
to the operating-system scheduler. On startup, missing configuration keys are recursively added from
the packaged defaults; existing values and unknown keys are preserved. Existing invalid values fail
configuration validation instead of being silently overwritten.

`verify.mode` controls the verification performed at the end of FULL maintenance. `normal` (default)
performs structural checks, while `deep` additionally verifies immutable object content and replays
every retained logical state. Manual verification is explicit: `/backup verify` always performs normal
verification and `/backup verify deep` requests deep verification.

## Administrator commands

All `/backup` commands require `endkeep.admin` and are OP/console-only by default.

```text
/backup status
/backup create
/backup list
/backup maintenance
/backup maintenance full
/backup verify
/backup verify deep
/backup cancel
/backup reload
```

`/backup create` creates a raw snapshot only. It does not immediately force normal
logicalization.

`/backup status` prints a one-shot worker/job snapshot. Active jobs include a stage pipeline such as
`Clone  [Logicalize]  Sidecar  Commit  Retention  Rollover  GC  Verify  Finalize` followed by current progress; EndKeep does
not continuously print progress to the console.

`/backup cancel` requests cooperative cancellation of the current repository job. Cancellation is
honored only at transaction-safe checkpoints; an atomic manifest/HEAD commit is always allowed to
finish. Any queued maintenance job remains queued.

A normal Endstone `/reload` detaches the plugin controller but leaves an active repository worker
running. The new plugin instance reconnects to the same worker and continues observing the existing
job instead of restarting it.

There is intentionally no online `/backup restore`.

## Storage layout

By default:

```text
backups/
├── raw/
│   ├── .incoming/
│   └── <snapshot-id>/
├── repo/
│   ├── LOCK
│   ├── HEAD
│   ├── objects/
│   ├── manifests/
│   └── .incoming/
├── work/
├── scheduler-state.json
├── worker-runtime.json
└── worker.log
```

`raw/` may normally contain only a few snapshots. `repo/` is the authoritative long-term
backup repository.

## Installation

EndKeep currently supports Linux only; Windows is not a supported or tested runtime.

Download the EndKeep `.whl` from either a GitHub Actions build artifact or a GitHub Release and
place it in the server's `plugins/` directory. Endstone installs Python plugin wheels and their
runtime dependencies into its managed plugin environment automatically; restart the server or use
Endstone's plugin reload flow as appropriate.

Current Amulet-LevelDB 3.0.7a0 metadata contains a compiler-version identifier that current uv
resolvers reject, so EndKeep intentionally uses pip for runtime/offline dependency installation.

Actions and Releases also provide the standalone recovery assets:

- `endkeep-offline.py`
- `requirements-offline.txt`
- `SHA256SUMS`

## Offline verification and restore

**STOP BDS BEFORE RESTORE.**

Restore is intentionally unavailable inside the online plugin.

The standalone tool does not require the EndKeep plugin wheel or Endstone. Create a clean
Python 3.14 environment and install only the supplied offline requirements:

```bash
python3.14 -m venv endkeep-offline-env
endkeep-offline-env/bin/python -m pip install -r requirements-offline.txt

endkeep-offline-env/bin/python endkeep-offline.py --repo /path/to/backups/repo list
endkeep-offline-env/bin/python endkeep-offline.py --repo /path/to/backups/repo verify
endkeep-offline-env/bin/python endkeep-offline.py --repo /path/to/backups/repo restore /path/to/new-world
```

Long-running `verify` and `restore` operations show stage-level tqdm progress by default. Add
`--verbose` to either command to include object roles, BASE/DELTA details, logical/compressed
byte counts, state digests, and per-step timings.

To restore a specific recovery point:

```bash
endkeep-offline-env/bin/python endkeep-offline.py \
  --repo /path/to/backups/repo \
  restore /path/to/new-world \
  --snapshot 20261006-163000
```

The destination world directory must not already exist. EndKeep restores sidecars, streams the
BASE plus required DELTAs into a **fresh** Amulet LevelDB, closes and reopens it, and verifies the
expected canonical visible-state SHA256 before reporting success.

A restored LevelDB is not expected to be physically byte-identical to the original database.
Correctness is defined by identical visible key/value state, matching state SHA256, and
byte-identical sidecar content.

## Crash-safety model

Key invariants:

- The BDS `save query` manifest is the sole authoritative definition of snapshot files and byte
  lengths.
- Every failure after a successful `save hold` attempts `save resume`.
- Source paths reject absolute paths, `..`, symlink traversal, and non-regular files.
- Active LevelDB logs are copied only to the byte length reported by BDS.
- Repository objects are immutable and verified before publication.
- `HEAD` moves only after all objects and the new generation manifest are durable.
- Retention/GC happens only after the replacement authoritative state is committed.
- Repository mutation is serialized by `repo/LOCK`.
- Startup recovery repairs interrupted generation publication without doing a slow deep scan.
- Any committed recovery point is designed to be restorable from the repository alone.

## Verification

Daily FULL maintenance performs structural verification: HEAD/manifest consistency, chain shape,
referenced-object presence and size sanity, orphan detection, and stale-work cleanup.

Manual `/backup verify` uses `verify.mode` from `config.toml`: `normal` performs structural
verification and `deep` additionally validates immutable object hashes and every retained logical
state digest. The standalone `endkeep-offline.py verify` remains a deep offline verification tool.

## Limitations and non-goals

EndKeep v1 intentionally does not implement cloud upload, S3/WebDAV/FTP, online restore, GUI,
player-facing backup commands, TPS/MSPT/player-count guards, a timezone framework, missed-event
catch-up, physical SST CDC, proactive rebase heuristics, parallel logicalization, or automatic
repair of arbitrary repository corruption.

The repository should still be copied off-host using an independent operational process if
machine-level disaster recovery is required.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m ruff check src tests tools
.venv/bin/python -m ruff format --check src tests tools
.venv/bin/python -m pytest
.venv/bin/python -m build
.venv/bin/python tools/build_offline.py --output dist/endkeep-offline.py
```

The Build workflow additionally creates a clean offline virtual environment that installs only
`requirements-offline.txt`, then performs repository-only list/verify/restore with the generated
standalone script.

## License

[MIT License](LICENSE)
