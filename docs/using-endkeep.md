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

## Commands

Commands require `endkeep.admin` (operator/console by default).

| Command | Action |
| --- | --- |
| `/backup status` | Show capture, repository and worker status |
| `/backup list` | List committed recovery points |
| `/backup create` | Capture a new raw recovery point |
| `/backup maintenance` | Process pending snapshots |
| `/backup maintenance full` | Run full maintenance |
| `/backup verify [deep]` | Check repository integrity |
| `/backup cancel` | Request cancellation at a safe checkpoint |
| `/backup reload` | Reload configuration |
| `/backup delete <id>` | Preview deletion of a recovery point |
| `/backup rollover <id>` | Preview advancing BASE to a recovery point |
| `/backup export <id>` | Restore a recovery point into a new `backups/exports/<id>/` directory |

To delete or roll over, run the preview command first. EndKeep displays a confirmation
command containing the current repository generation, for example
`/backup delete 20261006-163000 confirm 2`. Confirmations reject a changed generation.
Mutations never run concurrently with maintenance and do not immediately reclaim
orphan objects; scheduled full maintenance handles garbage collection.

Exports always go to `<storage.path>/exports/<snapshot-id>/`. They do not overwrite
an existing directory or the active world. These jobs run in the repository
worker and can take time; monitor them with `/backup status` and server logs.

Captures use `save hold/query/resume`. Raw snapshots are queued in `backups/raw/`,
then committed to `backups/repo/` during maintenance. The repository is the long-term
backup; do not use the raw queue as your only recovery copy.

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
