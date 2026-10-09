# Using EndKeep

EndKeep takes scheduled snapshots of your Bedrock world while the server is online.
It stores snapshots in the directory configured by `storage.path` (default: `backups/`).

## Getting started

Install the plugin wheel from [Releases](https://github.com/ReallocAll/endkeep/releases/latest)
into `plugins/` and restart Endstone. Requires Linux or Windows x86-64,
Python 3.12–3.14 and Endstone 0.11. Python 3.12/3.13 automatically install
the pinned Amulet-LevelDB compatibility Wheel from the
[compatibility Release](https://github.com/ReallocAll/endkeep/releases/tag/amulet-leveldb-3.0.7a0-compat-cp312-cp313);
Python 3.14 uses the official upstream package. The host must reach
GitHub Releases and PyPI during the initial installation.

Use `/backup status` to check that EndKeep loaded successfully.
Use `/backup create` for an immediate snapshot, then `/backup maintenance`
to process pending snapshots. `/backup list` shows recovery points available for restore.

## Commands

Commands are available to the console and operators by default (`endkeep.admin`).

| Command | Description |
| --- | --- |
| `/backup status` | Show backup status and current work |
| `/backup create` | Take a snapshot now |
| `/backup list` | List available recovery points |
| `/backup maintenance` | Process snapshots waiting for maintenance |
| `/backup maintenance full` | Also apply retention and run configured verification |
| `/backup verify` | Check repository structure |
| `/backup verify deep` | Check stored data and restore history more thoroughly |
| `/backup export <id>` | Export a recovery point to a separate world directory |
| `/backup delete <id>` | Preview deleting a recovery point |
| `/backup rollover <id>` | Preview discarding recovery points older than the selected one |
| `/backup confirm` | Confirm the most recent delete or rollover preview |
| `/backup cancel` | Request cancellation of supported background work |
| `/backup reload` | Reload EndKeep configuration |

Deleting or rolling over a snapshot is a two-step operation: run the command
to view what will change, then run `/backup confirm`. Preview information is
cleared on reload or restart. Export, delete and rollover jobs cannot be
cancelled once accepted. Make an independent copy before changing recovery history.

## Automatic backups

Default backup times are **12:00, 16:30, 20:30 and 23:45** in the server's
local timezone. Maintenance runs at **06:00 and 18:30**.
The first maintenance run also handles retention and verification.

Snapshots are kept for **7 days or at least the latest 28 recovery points**,
whichever retains more. Missed backup times are not replayed.

Edit `plugins/endkeep/config.toml` and run `/backup reload` to change these
settings. See [Configuration](configuration.md).

## Disk space and backup health

EndKeep normally reserves at least **5 GiB of free disk space**, plus enough
room for a new snapshot. On smaller disks, adjust `storage.min_free_space_gib`
carefully. If storage is full or backup processing fails, new captures may be
paused to preserve earlier recovery points. Check `/backup status` and the
server log before retrying.

If `/backup status` shows `health=FAILED`, maintenance and backup deletion are
blocked until a successful `/backup verify deep`. Preserve the repository and
investigate the error rather than deleting the health marker manually.

**Keep a separate off-server copy of your backups.** Backups stored on the same
disk cannot protect against disk or host failure.

## Restoring a world

Use `/backup export <id>` to create a separate restored world while Endstone
is running. To recover when the server cannot start, use the standalone restore
tool. Neither method overwrites your active world.

Follow the [Recovery guide](recovery.md) before replacing a live world.
