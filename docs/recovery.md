# Recovery

EndKeep can export an older world while the server is online or restore from
a repository when the server will not start.

**Always keep an independent copy of your backup repository before recovery.**
Do not restore directly over a running world.

## Export from a running server

1. Run `/backup list` to find a recovery point.
2. Run `/backup export <id>` to export that snapshot.
3. Check `/backup status` until the export is complete.

The world is written to `<storage.path>/exports/<id>/`. EndKeep will not
overwrite an existing export or your active world. You can inspect or copy the
exported world after it is complete.

## Restore without Endstone

Download `endkeep-offline.py` and `requirements-offline.txt` from the same
[EndKeep release](https://github.com/ReallocAll/endkeep/releases/latest).
The standalone restore tool does not require a working BDS or Endstone installation.

Stop the server before restoring. On Linux with Python 3.12–3.14, run:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-offline.txt

REPO=/path/to/backups/repo

.venv/bin/python endkeep-offline.py --repo "$REPO" list
.venv/bin/python endkeep-offline.py --repo "$REPO" verify
.venv/bin/python endkeep-offline.py --repo "$REPO" restore /path/to/restored-world --snapshot SNAPSHOT_ID
```

On Windows, create a virtual environment with `py -3.14 -m venv .venv`
and use `.venv\\Scripts\\python` instead of `.venv/bin/python` in the
commands above. Python 3.12/3.13 uses the matching Amulet-LevelDB
compatibility Wheel from GitHub Releases on either platform.

Replace `REPO` and `SNAPSHOT_ID` with your paths and recovery point ID.
Omit `--snapshot SNAPSHOT_ID` to restore the latest recovery point.
The destination directory **must not already exist**.

After restoration, copy the restored world into your server's worlds directory
as a separate world, select it in `server.properties` and check that it loads
correctly. Keep the original world until the restore has been verified.

## Offline management

The standalone tool also supports `delete` and `rollover`, with confirmation.
Run these only while EndKeep and its repository worker are not using the
repository. Use `--help` to see the available options.

If you installed the EndKeep wheel directly into a Python environment, it also
provides an `endkeep` command. Merely copying the wheel into Endstone's
`plugins/` directory does not create that command.

Keep the offline restore script and requirements from the release that created
your backups, especially when upgrading between major repository formats.
