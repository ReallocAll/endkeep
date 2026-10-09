# Changelog

All notable changes to EndKeep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [0.2.0] - 2026-10-09

### Added
- Add automatic bStats usage reporting through Endstone's built-in metrics service.
- Protect existing backups when repository verification or background processing fails.
- Preserve pending snapshots when backup limits are reached.

### Fixed
- Show all available recovery points in `/backup list`.
- Prevent unsuccessful snapshot processing from discarding recovery data.
- Improve release safeguards and platform documentation.

### Documentation
- Simplify installation, backup management, configuration and recovery guides.

## [0.1.9] - 2026-10-09

### Added
- Manage recovery points online with `/backup delete <snapshot>`, `/backup rollover <snapshot>` and a separate `/backup confirm` after a read-only impact preview.
- Export a verified recovery point into a separate world directory with `/backup export <snapshot>`.
- Add a standard `endkeep` console entry point for environments that explicitly install the wheel.

### Changed
- Keep pending online confirmations in plugin memory only, with no expiration and no disk persistence. Repository changes invalidate stale previews; reloading or restarting EndKeep clears them.
- Dispatch online management through the existing repository worker, protecting active worlds and configured free-space reserves.
- Keep the original `tqdm` progress bars and dependency for the standalone offline restore tool; online worker operations do not require `tqdm`.

### Fixed
- Support suffixed snapshot IDs in exports, reject unsafe export paths and dangling symlinks, and clean up interrupted exports.
- Make worker management requests idempotent across RPC timeouts, with explicit reporting when request acceptance cannot be confirmed.


## [0.1.8] - 2026-10-08

### Added
- Add offline-only confirmed `delete SNAPSHOT` and `rollover SNAPSHOT` repository mutations.
- Rebuild and verify bridge DELTAs when deleting an intermediate recovery point; preserve retained restore chains without implicit GC.

### Changed
- Stream middle-snapshot bridge DELTAs from a single predecessor replay, reducing redundant chain reads and open file descriptors.

### Fixed
- Treat HEAD as the authoritative transaction commit point during startup recovery instead of promoting unpublished manifest generations.

## [0.1.7] - 2026-10-07

### Changed
- Make `verify.mode` control the verification performed by FULL maintenance.
- Make `/backup verify` always run normal verification and add `/backup verify deep` for explicit deep verification.
- Label the configured status field as `full_verify` to make its scope explicit.

## [0.1.6] - 2026-10-07

### Added
- Run heavy repository maintenance in an automatically managed long-lived subprocess with an independent Python interpreter/GIL.
- Add normalized `worker.priority` policies for conservative, background, balanced, and throughput scheduling.
- Add one-shot stage/progress details to `/backup status` and cooperative `/backup cancel`.
- Add configurable manual verification strength with `verify.mode = normal|deep`, defaulting to normal.
- Persist and coalesce overlapping scheduled maintenance into a single pending slot.

### Changed
- Show BASE/DELTA logicalization throughput in `k/s`, compact million-scale counts with `M`, and an estimated DELTA completion percentage without an extra LevelDB scan.
- Preserve active repository work across Endstone plugin reloads by reconnecting to the existing worker.
- Recursively add newly introduced default configuration keys at startup while preserving existing and unknown settings.
- Keep FULL maintenance's built-in repository verification structural even when manual verification is configured as deep.

### Fixed
- Isolate Python-heavy LevelDB scanning and semantic diffing from the BDS process so repository maintenance cannot starve the server thread on the shared GIL.
- Treat cooperative cancellation separately from logicalization failure so cancellation can never trigger raw recovery-point eviction.

## [0.1.5] - 2026-10-07

### Added
- Show stage-level tqdm progress for long-running standalone offline verify and restore operations.
- Add `--verbose` diagnostics for object roles, BASE/DELTA details, byte counts, state digests, and timings.

### Changed
- Report object verification progress as an object count instead of summed compressed-plus-logical bytes, avoiding misleading apparent repository sizes.

## [0.1.4] - 2026-10-06

### Changed
- Keep save-query retry policy internal and retry transient misses every 10 server ticks instead of exposing a retry-count setting.

## [0.1.3] - 2026-10-06

### Fixed
- Retry transient `save query` failures instead of treating Endstone's command-success boolean as a dispatch guarantee.

### Added
- Add backward-compatible `capture.query_retries` configuration for save-query retry limits.

## [0.1.2] - 2026-10-06

### Fixed
- Avoid Amulet-LevelDB high-level iterator EOF handling that could abort Endstone during background logicalization.

## [0.1.1] - 2026-10-06

### Fixed
- Avoid duplicate EndKeep maintenance enum registration while preserving `/backup maintenance [full]` completion.

## [0.1.0] - 2026-10-06

### Added
- Crash-safe online BDS snapshot capture using save hold/query/resume.
- Authoritative exact-byte staging from the save-query manifest.
- Short-lived durable raw snapshot queue with hard backlog, age, and free-space limits.
- Canonical logical BASE, semantic DELTA, and SIDECAR formats.
- Amulet-LevelDB visible-state scanning and fresh-database restore.
- Immutable zstd-compressed object store with logical and compressed SHA256 identities.
- Generation manifests, atomic HEAD commits, repository locking, and startup recovery.
- LOGIC_ONLY and FULL maintenance, retention, logical rollover, orphan GC, and verification.
- Administrator-only /backup status/create/list/maintenance/verify/reload commands.
- Standalone endkeep-offline.py list/verify/restore tool independent of the plugin wheel.
- Unit, integration, repository-only restore, scheduler, and crash/fault-injection tests.
- GitHub Actions artifacts and GitHub Release packaging for the wheel and offline recovery assets.
