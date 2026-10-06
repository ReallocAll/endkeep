# Changelog

All notable changes to EndKeep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
