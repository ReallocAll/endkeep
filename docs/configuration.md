# Configuration

EndKeep creates `plugins/endkeep/config.toml` when the plugin starts for the
first time. Edit the file to change schedules and storage options, then run
`/backup reload` to apply the changes.

## Options

| Setting | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Enable automatic backups |
| `capture.times` | 12:00, 16:30, 20:30, 23:45 | Daily snapshot times (server local time) |
| `maintenance.times` | 06:00, 18:30 | Daily maintenance times; first runs full maintenance |
| `retention.keep_days` | 7 | Keep backups this many days old or newer |
| `retention.keep_last` | 28 | Also keep at least this many recent recovery points |
| `storage.path` | `backups` | Backup directory, relative to the server unless absolute |
| `storage.min_free_space_gib` | 5 | Minimum free disk space to keep available |
| `worker.priority` | `background` | Background work priority |
| `logical.compression_level` | 6 | Compression level for stored backups |
| `logical.compression_threads` | 4 | Compression threads |
| `raw.max_pending` | 18 | Maximum pending snapshots |
| `raw.max_age_days` | 3 | Pending-snapshot age limit used when handling backlog pressure |
| `verify.mode` | `normal` | Verification level during full maintenance (`normal` or `deep`) |

Retention uses **either** limit: a recovery point is kept if it is recent
enough **or** among the newest `keep_last` snapshots.

When the pending snapshot queue or disk limit is reached, EndKeep may refuse
new backups rather than remove unprocessed recovery points. Monitor `/backup status`
and ensure maintenance completes successfully.

For the complete default file, see
[config.toml](../src/endstone_endkeep/config.toml).

## Anonymous statistics (bStats)

EndKeep uses Endstone's built-in [bStats reporting](https://bstats.org/plugin/bukkit/endkeep/34593)
to help track plugin usage. It does not send world contents, backup files,
filesystem paths or snapshot names.

To disable reporting, edit Endstone's shared `plugins/bstats/config.toml`:

```toml
enabled = false
```

Restart the server after changing this setting. It applies to Endstone's
bStats integration, not just EndKeep.
