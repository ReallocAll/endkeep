# Manual validation

These production checks require a running Bedrock Dedicated Server and are not part of regular CI.

## Online capture

Run this on a disposable copy or a server where a failed test capture can be safely discarded.

1. Install the wheel from a green GitHub Release and start BDS normally.
2. Run `/backup status` and confirm EndKeep is enabled, idle, and using the expected storage path.
3. Run `/backup create`.
4. Confirm the capture log reports the snapshot ID, file count, raw bytes, full BDS hold duration, exact staging
   duration, and the published raw path.
5. Confirm BDS resumed saving immediately after exact staging; no Amulet scan, zstd compression, retention, or deep
   verification should occur while the save is held.
6. Inspect `backups/raw/<snapshot-id>/snapshot.json` and confirm the files and byte limits match the authoritative
   `save query` result.
7. Run `/backup maintenance` and confirm the pending raw snapshot is committed to the logical repository and then
   removed from `raw/`.
8. Run `/backup list` and `/backup verify`; both must succeed.

### Capture regression baseline

The validated reference world was about 444 MiB / 394 files:

- save-query ready latency: about 562 ms
- exact-byte copy: about 1.387 s
- externally measured copy phase: about 1.444 s
- total BDS hold window: about 2.012 s

Storage hardware and world shape vary, so these are regression references rather than hard limits. A comparable world
suddenly taking 10–20 seconds of hold time should be treated as a regression and investigated before release.

## Online repository management

The v0.1.9 online export, delete, rollover, and confirmation flows have also
been manually exercised by the maintainer. For future changes, repeat the
following on a **disposable repository copy**, not the sole backup:

1. Export a recovery point; verify its world contents and state digest.
2. Preview and confirm tail/middle DELTA deletion; deep-verify survivors.
3. Preview and confirm BASE deletion or forced rollover; deep-verify survivors.
4. Verify stale confirmations are refused after repository changes or reload.
5. Verify accepted mutations/exports cannot be cancelled.
6. Verify insufficient disk space and existing export paths leave no partial export.

## Offline disaster recovery

**Stop BDS before restore. Never restore in place over the active world.**

1. Download `endkeep-offline.py` and `requirements-offline.txt` directly from the Actions artifact or GitHub
   Release. Do not install the EndKeep plugin wheel in the restore environment.
2. Install only the declared offline dependencies.
3. Run `endkeep-offline.py --repo <backups/repo> list`.
4. Run `endkeep-offline.py --repo <backups/repo> verify`.
5. Restore into a new directory with
   `endkeep-offline.py --repo <backups/repo> restore <new-world-directory> --snapshot <id>`.
6. Confirm restore reports the expected world-state SHA256 and completes successfully.
7. Start BDS with a copy of the restored world and verify representative player data, chunks, inventories, and world
   metadata.

Correctness is defined by identical visible LevelDB KV state / state SHA256 and byte-identical sidecars. Physical
LevelDB file layout is not expected to be byte-identical after a fresh rebuild.
