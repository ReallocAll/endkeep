# Release checks

These checks complement automated tests. Perform them with a release candidate
on a test world before publishing a version.

**Existing live-server validation:** Ubuntu 26.04 LTS x86-64, Python 3.14,
Endstone 0.11. The cross-platform test matrix covers Python 3.12/3.13/3.14 on
Linux x86-64 and Windows x64, but CI is not a substitute for running a real BDS
instance. Until the steps below succeed on disposable Windows worlds, Windows
support is a development target rather than a production guarantee.

## Installation and backup

1. Install the plugin wheel on a clean Endstone host and confirm its dependencies load.
2. Run `/backup status` and `/backup create`. Confirm the server remains usable.
3. Run `/backup maintenance`, then `/backup list` and `/backup verify deep`.
4. Confirm the configured schedule and retention settings behave as expected.

## Recovery

1. Export a snapshot with `/backup export <id>` and check the restored world.
2. Stop BDS and restore a snapshot with the standalone recovery script.
3. Start BDS using a copy of the restored world and check that the world loads.
4. On a disposable backup repository, preview and confirm delete and rollover;
   verify the remaining recovery points afterwards.

## Failure handling

- Test low free space, interrupted work and plugin reload/restart without
  losing existing recovery points.
- Confirm a failed repository verification prevents destructive maintenance.
- Check that disabling bStats does not affect backup operation.

Keep these tests on disposable worlds or independent copies of backup data.

## Cross-platform release acceptance (Python 3.12–3.14)

Complete the following for **each** Linux and Windows platform family. Do not
publish a compatibility claim merely because the six GitHub Actions jobs passed.

1. Test the exact published plugin Wheel and a clean installation on Endstone
   0.11 for Python 3.12/3.13/3.14. The 3.12/3.13 compatibility Wheels of
   Amulet-LevelDB must be publicly retrievable and their checksums verified;
   CI artifacts expire and are not a production dependency source.
2. With an isolated BDS world, exercise `save hold`, `save query`, `save resume`,
   and two snapshots containing LevelDB keys with binary data. Verify no tick
   stall or held-save state persists after a failed capture.
3. Capture BASE and at least two DELTAs. Compare each exported state's records,
   world files and SHA-256 with the original world at that recovery point.
4. Test delete-middle, rollover, GC, `verify deep`, and offline restore to a new
   directory. Load the restored disposable world in BDS.
5. Crash a disposable backup worker while writing an incoming object, after an
   object rename, after manifest publication and while updating HEAD. Restart,
   verify the correct recovery point survives, and confirm that suspicious
   repository states block destructive operations.
6. On Windows, separately test NTFS file locking/sharing, open LevelDB handles,
   reparse points/junctions and interrupted file/directory rename sequences.
   Windows' CRT does not provide POSIX directory-fsync behavior; passing unit
   tests is not evidence of identical power-failure durability guarantees.

Only then update README installation claims and enable normal end-user
distribution on the newly supported platform/Python versions.
