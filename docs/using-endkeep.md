# Using EndKeep

## Configuration

EndKeep creates `plugins/endkeep/config.toml` when first started.
The [default configuration](../src/endstone_endkeep/config.toml) is the source of truth for available settings.

By default, captures run at **12:00, 16:30, 20:30 and 23:45** (server local time).
Maintenance runs at **06:00** (full, including retention and verification)
and **18:30** (process pending snapshots).
Missed captures are not replayed.

Snapshots are retained if they are **within 7 days or among the latest 28**.
`[retention]` and `[capture]` settings can be changed in `config.toml`.
`[worker].priority` controls how strongly backup work yields to the server.

## Configuration reference

Edit `plugins/endkeep/config.toml` in the BDS working directory:

| Setting | Default | Purpose |
| --- | --- | --- |
| `capture.times` | 12:00, 16:30, 20:30, 23:45 | Local-time capture schedule |
| `maintenance.times` | 06:00, 18:30 | First slot FULL; later slots LOGIC_ONLY |
| `raw.max_pending` | 18 | Hard upper limit for queued raw snapshots |
| `raw.max_age_days` | 3 | Maximum raw backlog age before pressure handling |
| `logical.compression_level` | 6 | Zstd object compression strength |
| `logical.compression_threads` | 4 | Threads used by compression **inside the worker process** |
| `retention.keep_days` | 7 | Keep recent snapshots |
| `retention.keep_last` | 28 | Also keep newest N snapshots (logical **OR** with keep_days) |
| `storage.path` | backups | Backup root, relative to BDS unless absolute |
| `storage.min_free_space_gib` | 5 | Free-space reserve for capture/export |
| `worker.priority` | background | Separate worker's OS priority policy |
| `verify.mode` | normal | Normal or deep verification **at the end of FULL maintenance** |

The raw backlog limits are **hard**: EndKeep tries logicalizing the oldest raw
snapshot under pressure but may evict it if logicalization fails. The raw queue
is not a substitute for committed recovery points. Deep verification validates
object bytes and replays retained logical states. `/backup verify` is always
normal; `/backup verify deep` explicitly requests deep verification.

## Commands

Commands require `endkeep.admin` (operator/console by default).

| Command | Action |
| --- | --- |
| `/backup status` | Show capture, repository and worker status |
| `/backup list` | List all committed recovery points |
| `/backup create` | Capture a new raw recovery point |
| `/backup maintenance` | Process pending snapshots |
| `/backup maintenance full` | Run full maintenance |
| `/backup verify [deep]` | Check repository integrity |
| `/backup cancel` | Request cancellation at a safe checkpoint |
| `/backup reload` | Reload configuration |
| `/backup delete <id>` | Preview deletion of a recovery point |
| `/backup rollover <id>` | Preview advancing BASE to a recovery point |
| `/backup export <id>` | Restore a recovery point into a new `backups/exports/<id>/` directory |
| `/backup confirm` | Execute your most recently previewed delete or rollover operation |

To delete or roll over, run `/backup delete <id>` or `/backup rollover <id>`
to inspect the impact, followed by **`/backup confirm`** to commit. Previews are
held only in plugin memory, separately for each command sender, with no timeout.
A new preview replaces the previous one. If the repository generation changes,
confirmation rejects the stale preview and requires a fresh preview. Restarting
or reloading the plugin clears all pending previews; `/backup confirm` then
reports that no preview is pending. No confirmation state is written to disk.
The worker also checks the generation under the repository lock.

After a mutation or export is accepted, it **cannot be cancelled**. Mutations never run concurrently with maintenance and do not immediately reclaim
orphan objects; scheduled full maintenance handles garbage collection.

Exports always go to `<storage.path>/exports/<snapshot-id>/`. Before restoring,
EndKeep reserves a conservative estimate of the required free space and monitors
the configured storage reserve while writing. They do not overwrite
an existing directory or the active world. These jobs run in the repository
worker and can take time; monitor them with `/backup status` and server logs.

Captures use `save hold/query/resume`. Raw snapshots are queued in `backups/raw/`,
then committed to `backups/repo/` during maintenance. The repository is the long-term
backup; do not use the raw queue as your only recovery copy.

## Wheel CLI

The wheel exposes a standard `endkeep` console entry point if installed
normally into a Python environment. Endstone's private plugin-wheel installation
does **not** create an additional launcher in `plugins/endkeep/`. To run the
wheel CLI separately, install the wheel into the CLI's Python environment and
run `endkeep list` (or provide `--repo`). By default the CLI discovers the
repository from `plugins/endkeep/config.toml` in the BDS working directory.

The CLI's verification and restore progress bars use `tqdm`, as does the
standalone `endkeep-offline.py` tool. Install the packages listed in
`requirements-offline.txt` for the standalone tool; the wheel's CLI needs
`tqdm` available when running progress-displaying operations. The **online**
`/backup` commands and repository worker do not require `tqdm`. For live
mutations or exports, prefer `/backup`, which coordinates jobs through the
worker. Do not run direct CLI mutations while the server or worker may be
writing to the repository.

## Offline restore

**Stop BDS before restoring. Never restore over an active world.**
Download `endkeep-offline.py` and `requirements-offline.txt` from
[Releases](https://github.com/ReallocAll/endkeep/releases/latest).
The standalone tool does not need Endstone or the plugin wheel.

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-offline.txt

REPO=/path/to/backups/repo

.venv/bin/python endkeep-offline.py --repo "$REPO" list
.venv/bin/python endkeep-offline.py --repo "$REPO" verify
.venv/bin/python endkeep-offline.py --repo "$REPO" restore /path/to/new-world --snapshot SNAPSHOT_ID
```

The restore destination must not exist. Restores rebuild the logical LevelDB state
and verify the expected state digest; the resulting SST files need not be byte-identical.

The standalone tool also provides `delete` and `rollover` for retention management
when the plugin is unavailable. Run them only while the repository is quiescent.
See `endkeep-offline.py --help` before using them.

Keep an **independent off-host copy** of `backups/repo/` for machine-level recovery.

## When repository verification fails

A failed normal or deep verification records a persistent
`backups/repository-health.json` marker (under the configured storage root).
`/backup status` then reports `health=FAILED` and the reason. EndKeep blocks
logicalization, maintenance, destructive snapshot operations, retention, rollover,
and garbage collection until **`/backup verify deep` passes**.

Raw capture can continue while the queue has spare capacity and the filesystem
has enough free space. In this failed state EndKeep does **not** evict or
logicalize old raw snapshots, even under pressure; it blocks new captures once
raw capacity or free-space limits are reached. Startup recovery also preserves
already committed raw snapshots while the marker exists.

Investigate the error and preserve the repository before attempting repairs.
Do not delete the health marker to bypass the safety interlock: repair or
replace the damaged repository, then run `/backup verify deep` to re-enable
maintenance. Keep independent off-host backups.

## Repository format compatibility

The current repository manifest uses schema 1. EndKeep is pre-1.0 and does
not yet promise that every future version will transparently read every earlier
repository. Keep the standalone restore script and offline requirements shipped
with the release that wrote your snapshots. Make an independently verified copy
before any future format migration. Incompatible migrations must be explicit,
documented, and tested; they must never silently rewrite repository history.
