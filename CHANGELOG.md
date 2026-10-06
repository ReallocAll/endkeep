# Changelog

All notable changes to EndKeep are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

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
