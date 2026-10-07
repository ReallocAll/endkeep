# Changelog

All notable changes to EndKeep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
