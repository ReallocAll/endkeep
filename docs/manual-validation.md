# Release checks

These checks complement automated tests. Perform them with a release candidate
on a test world before publishing a version.

**Automated validation:** Python 3.12/3.13/3.14 on Linux x86-64 and Windows
x64; [six-combination CI](https://github.com/ReallocAll/endkeep/actions/runs/37976573011).
Real BDS 1.26.52.3 / Endstone 0.11.13 with Python 3.14 passed on both platforms
in [bds-test-lab](https://github.com/ReallocAll/bds-test-lab/actions/runs/37977425842):
two online snapshots, maintenance, deep verify, export, graceful BDS stop,
explicit authenticated worker shutdown and offline restore. The tests do
not establish equivalent NTFS/POSIX sudden-power-loss guarantees.

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

The Python 3.14 smoke scenarios were completed on disposable Linux and Windows
BDS worlds. Repeat the following when validating additional server versions,
large repositories or production configurations.

1. Test the exact published plugin Wheel and a clean installation on Endstone
   0.11 for Python 3.12/3.13/3.14. The 3.12/3.13 compatibility Wheels of
   Amulet-LevelDB must be publicly retrievable and their checksums verified;
   EndKeep uses the fixed compatibility Release rather than expiring CI artifacts.
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

The repository worker intentionally stays alive across plugin reloads.
The headless lab explicitly shuts down the authenticated worker after
stopping BDS so the process tree is clean; this is separate from checking
whether BDS itself stops normally.

Sudden power failure cannot be simulated by clean shutdown. The Windows
NTFS directory-metadata durability guarantees remain different from POSIX
directory fsync; test these separately before making stronger claims.
