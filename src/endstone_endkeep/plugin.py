from __future__ import annotations

from pathlib import Path

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin
from typing_extensions import override

from .config import ConfigError, EndKeepConfig
from .coordinator import CaptureCoordinator
from .repository.lock import RepositoryLock
from .repository.maintenance import MaintenanceResult, RepositoryJobResult, RepositoryService
from .repository.recovery import StartupRecovery
from .scheduler import EndKeepScheduler, ScheduleEvent, SchedulerState
from .staging.raw import RawSnapshotStore


class EndKeepPlugin(Plugin):
    prefix = "EndKeep"
    api_version = "0.11"

    commands = {
        "backup": {
            "description": "Manage EndKeep backups",
            "usages": [
                "/backup <status|create|list|verify|reload>",
                "/backup maintenance",
                "/backup maintenance full",
            ],
            "permissions": ["endkeep.admin"],
        }
    }

    permissions = {
        "endkeep.admin": {
            "description": "Allow administrative EndKeep backup operations.",
            "default": "op",
        }
    }

    def __init__(self) -> None:
        super().__init__()
        self._runtime_config: EndKeepConfig | None = None
        self._capture: CaptureCoordinator | None = None
        self._repository: RepositoryService | None = None
        self._clock: EndKeepScheduler | None = None
        self._tick_counter = 0
        self._storage_root: Path | None = None
        self._pending_capture = False
        self._pending_capture_scheduled_for: str | None = None

    @override
    def on_enable(self) -> None:
        self.save_default_config()
        try:
            self._configure_runtime()
        except Exception as exc:
            self.logger.critical(f"Failed to enable EndKeep: {exc}")
            return

        self.server.scheduler.run_task(self, self._tick, delay=1, period=1)
        self.logger.info("EndKeep enabled.")

    @override
    def on_disable(self) -> None:
        self._close_runtime()
        self.logger.info("EndKeep disabled.")

    @override
    def on_command(self, sender: CommandSender, command: Command, args: list[str]) -> bool:
        if command.name != "backup" or not args:
            return False

        action = args[0]
        if action == "status":
            self._send_status(sender)
            return True
        if action == "create":
            if self._request_capture(scheduled_for=None):
                sender.send_message("EndKeep snapshot capture accepted.")
            else:
                sender.send_error_message("EndKeep is busy or disabled.")
            return True
        if action == "list":
            self._command_list(sender)
            return True
        if action == "maintenance":
            full = len(args) == 2 and args[1] == "full"
            if len(args) > 2 or (len(args) == 2 and not full):
                return False
            self._command_maintenance(sender, full=full)
            return True
        if action == "verify":
            self._command_verify(sender)
            return True
        if action == "reload":
            self._command_reload(sender)
            return True
        return False

    def _configure_runtime(self, runtime_config: EndKeepConfig | None = None) -> None:
        config = runtime_config or EndKeepConfig.from_mapping(self.config)
        storage_root = config.storage.path
        if not storage_root.is_absolute():
            storage_root = Path.cwd() / storage_root
        storage_root = storage_root.resolve()
        storage_root.mkdir(parents=True, exist_ok=True)

        repository = RepositoryService(
            storage_root,
            compression_level=config.logical.compression_level,
            compression_threads=config.logical.compression_threads,
            max_pending=config.raw.max_pending,
            max_age_days=config.raw.max_age_days,
            min_free_space_gib=config.storage.min_free_space_gib,
            keep_days=config.retention.keep_days,
            keep_last=config.retention.keep_last,
        )

        try:
            with RepositoryLock(repository.repo_root):
                recovery = StartupRecovery(
                    storage_root,
                    repository.manifests,
                    repository.objects,
                ).run()
        except Exception:
            repository.close()
            raise

        source_root = (Path.cwd() / "worlds").resolve()
        raw_store = RawSnapshotStore(storage_root, source_root)
        capture = CaptureCoordinator(
            self,
            raw_store,
            space_guard=repository.queue.free_space_allows,
        )
        clock = EndKeepScheduler(
            config,
            SchedulerState(storage_root / "scheduler-state.json"),
            self._dispatch_schedule_event,
        )

        self._runtime_config = config
        self._storage_root = storage_root
        self._repository = repository
        self._capture = capture
        self._clock = clock
        self._pending_capture = False
        self._pending_capture_scheduled_for = None

        self.logger.info(
            f"Startup recovery: pending_raw={len(recovery.pending_raw)} "
            f"committed_raw_removed={recovery.removed_committed_raw} "
            f"recovered_head={recovery.recovered_head}"
        )

    def _close_runtime(self) -> None:
        capture = self._capture
        repository = self._repository
        self._capture = None
        self._repository = None
        self._clock = None
        self._pending_capture = False
        self._pending_capture_scheduled_for = None
        if capture is not None:
            capture.close()
        if repository is not None:
            repository.close()

    def _tick(self) -> None:
        capture = self._capture
        repository = self._repository

        if capture is not None:
            capture.pump()

        if repository is not None:
            try:
                result = repository.poll()
            except Exception as exc:
                self.logger.error(f"REPOSITORY FAILURE: background job failed: {exc}")
                self._pending_capture = False
                self._pending_capture_scheduled_for = None
            else:
                if result is not None:
                    self._handle_repository_result(result)

        if self._pending_capture and repository is not None and not repository.busy:
            if not repository.start_pre_capture():
                self.logger.error("REPOSITORY FAILURE: unable to start pre-capture raw-limit enforcement")

        self._tick_counter += 1
        if self._tick_counter < 200:
            return
        self._tick_counter = 0

        config = self._runtime_config
        clock = self._clock
        if config is None or clock is None or not config.enabled:
            return
        try:
            clock.tick()
        except Exception as exc:
            self.logger.error(f"Scheduler failure: {exc}")

    def _dispatch_schedule_event(self, event: ScheduleEvent) -> None:
        if event.kind == "capture":
            if not self._request_capture(scheduled_for=event.configured_time):
                self.logger.error(
                    f"CAPTURE FAILURE: scheduled capture {event.configured_time} could not be accepted"
                )
            return

        repository = self._repository
        if repository is None or event.maintenance_mode is None:
            return
        if not repository.start_maintenance(event.maintenance_mode):
            self.logger.error(
                f"REPOSITORY FAILURE: scheduled {event.maintenance_mode} maintenance "
                f"at {event.configured_time} skipped because repository is busy"
            )
        else:
            self.logger.info(
                f"Scheduled {event.maintenance_mode} maintenance started ({event.configured_time})."
            )

    def _request_capture(self, *, scheduled_for: str | None) -> bool:
        config = self._runtime_config
        capture = self._capture
        repository = self._repository
        if config is None or not config.enabled or capture is None or repository is None:
            return False
        if capture.busy or self._pending_capture:
            return False

        self._pending_capture = True
        self._pending_capture_scheduled_for = scheduled_for
        if not repository.busy:
            repository.start_pre_capture()
        return True

    def _handle_repository_result(self, result: RepositoryJobResult) -> None:
        if result.kind == "pre_capture":
            limits = result.raw_limits
            if limits is None:
                self.logger.error("REPOSITORY FAILURE: pre-capture job returned no result")
                self._clear_pending_capture()
                return

            if limits.logicalized:
                self.logger.info(
                    f"Pre-capture logicalized {len(limits.logicalized)} raw snapshot(s): "
                    f"{', '.join(limits.logicalized)}"
                )
            if limits.dropped:
                self.logger.critical(
                    f"CRITICAL RAW LIMIT: dropped oldest pending raw snapshot(s): "
                    f"{', '.join(limits.dropped)}"
                )
            if limits.blocked_for_space:
                self.logger.error(
                    "CAPTURE FAILURE: free space is below configured reserve after raw-limit enforcement"
                )
                self._clear_pending_capture()
                return

            capture = self._capture
            scheduled_for = self._pending_capture_scheduled_for
            self._clear_pending_capture()
            if capture is None or not capture.start_capture(scheduled_for=scheduled_for):
                self.logger.error("CAPTURE FAILURE: capture became unavailable after pre-capture checks")
            return

        if result.kind == "maintenance":
            if result.maintenance is not None:
                self._log_maintenance(result.maintenance)
            return

        if result.kind == "verify" and result.verify is not None:
            report = result.verify
            self.logger.info(
                f"Repository deep verify PASS: generation={report.generation} "
                f"snapshots={report.snapshots} objects={report.referenced_objects} "
                f"orphans={report.orphan_objects}"
            )

    def _log_maintenance(self, result: MaintenanceResult) -> None:
        committed = len(result.committed)
        failures = len(result.failures)
        self.logger.info(
            f"{result.mode} maintenance complete: committed={committed} failures={failures}"
        )
        for item in result.committed:
            self.logger.info(
                f"Logicalized {item.snapshot_id}: type={item.node_type} records={item.records} "
                f"changed={item.changed} inserted={item.inserted} deleted={item.deleted} "
                f"logical_bytes={item.logical_bytes} compressed_bytes={item.compressed_bytes} "
                f"elapsed={item.elapsed_seconds:.3f}s"
            )
        for failure in result.failures:
            self.logger.error(
                f"LOGICALIZATION FAILURE: snapshot={failure.snapshot_id}: {failure.error}"
            )
        if result.rollover is not None:
            self.logger.info(
                f"Retention rollover: new_base={result.rollover.new_base_snapshot} "
                f"absorbed={len(result.rollover.absorbed_snapshots)}"
            )
        if result.gc is not None:
            self.logger.info(
                f"GC: removed_objects={result.gc.removed_objects} "
                f"removed_bytes={result.gc.removed_bytes}"
            )
        if result.verify is not None:
            self.logger.info(
                f"Structural verify: generation={result.verify.generation} "
                f"snapshots={result.verify.snapshots} orphans={result.verify.orphan_objects}"
            )

    def _clear_pending_capture(self) -> None:
        self._pending_capture = False
        self._pending_capture_scheduled_for = None

    def _send_status(self, sender: CommandSender) -> None:
        config = self._runtime_config
        capture = self._capture
        repository = self._repository
        if config is None or capture is None or repository is None or self._storage_root is None:
            sender.send_error_message("EndKeep is not configured.")
            return
        status = capture.status()
        pending = repository.queue.pending()
        current = repository.manifests.load_current()
        generation = None if current is None else current.generation
        snapshots = 0 if current is None else len(current.chain)
        sender.send_message(
            f"EndKeep: enabled={config.enabled} capture={status.state} held={status.held} "
            f"repository_busy={repository.busy} raw_pending={len(pending)} "
            f"generation={generation} snapshots={snapshots} storage={self._storage_root}"
        )

    def _command_list(self, sender: CommandSender) -> None:
        repository = self._repository
        if repository is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        try:
            manifest = repository.manifests.load_current()
        except Exception as exc:
            sender.send_error_message(f"REPOSITORY FAILURE: {exc}")
            return
        if manifest is None:
            sender.send_message("EndKeep repository is empty.")
            return
        sender.send_message(
            f"EndKeep repository generation {manifest.generation}: {len(manifest.chain)} snapshot(s)"
        )
        for node in manifest.chain[-20:]:
            sender.send_message(
                f"{node.snapshot} {node.type.upper()} {node.captured_at} "
                f"records={node.records} state={node.state_sha256[:12]}"
            )

    def _command_maintenance(self, sender: CommandSender, *, full: bool) -> None:
        repository = self._repository
        capture = self._capture
        if repository is None or capture is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        if capture.busy or self._pending_capture:
            sender.send_error_message("Cannot start maintenance while capture is active or pending.")
            return
        mode = "FULL" if full else "LOGIC_ONLY"
        if not repository.start_maintenance(mode):
            sender.send_error_message("EndKeep repository is busy.")
            return
        sender.send_message(f"{mode} maintenance started.")

    def _command_verify(self, sender: CommandSender) -> None:
        repository = self._repository
        capture = self._capture
        if repository is None or capture is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        if capture.busy or self._pending_capture:
            sender.send_error_message("Cannot verify while capture is active or pending.")
            return
        if not repository.start_verify():
            sender.send_error_message("EndKeep repository is busy.")
            return
        sender.send_message("Deep repository verification started.")

    def _command_reload(self, sender: CommandSender) -> None:
        capture = self._capture
        repository = self._repository
        if (
            (capture is not None and capture.busy)
            or (repository is not None and repository.busy)
            or self._pending_capture
        ):
            sender.send_error_message("Cannot reload EndKeep configuration while work is active.")
            return

        try:
            self.reload_config()
            new_config = EndKeepConfig.from_mapping(self.config)
        except ConfigError as exc:
            sender.send_error_message(f"Invalid EndKeep config: {exc}")
            return
        except Exception as exc:
            sender.send_error_message(f"Failed to reload EndKeep config: {exc}")
            return

        self._close_runtime()
        try:
            self._configure_runtime(new_config)
        except Exception as exc:
            sender.send_error_message(f"Failed to apply EndKeep config: {exc}")
            self.logger.critical(f"EndKeep runtime is unavailable after reload failure: {exc}")
            return
        sender.send_message("EndKeep configuration reloaded.")
