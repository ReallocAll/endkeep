# Release checks

These checks complement automated tests. Perform them with a release candidate
on a test world before publishing a version.

**Validated environment:** Ubuntu 26.04 LTS x86-64, Python 3.14, Endstone 0.11.
The automated CI runner is not a substitute for testing a real Bedrock server.

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
