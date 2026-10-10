# Release checks

Validate new releases on a disposable BDS world and a copy of the backup
repository. These checks complement automated tests.

## Installation and backup

1. Install the release wheel on supported platforms and Python versions.
   Confirm the plugin and its dependencies load without manual setup.
2. Capture multiple snapshots while BDS is running, process maintenance,
   and run `/backup verify deep`.
3. Check backup scheduling, retention, low-disk-space protection and reload.

## Recovery

1. Export a recovery point and verify its world files and LevelDB data.
2. Stop BDS, restore offline to a new directory, and confirm the restored
   world loads in BDS.
3. On a repository copy, test delete, rollover and cleanup; then verify
   the remaining recovery points.

## Failure handling

- Interrupt captures and worker operations at different stages; verify
  restart recovery without losing committed snapshots.
- Ensure failed integrity checks block destructive operations.
- On Windows, verify file locking, junction handling and interrupted
  renames. Clean shutdown tests do not establish sudden-power-loss durability.
