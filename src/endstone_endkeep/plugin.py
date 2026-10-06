from __future__ import annotations

from pathlib import Path

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin
from typing_extensions import override

from .config import ConfigError, EndKeepConfig
from .coordinator import CaptureCoordinator
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
                "/backup maintenance <full>",
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
        self._clock: EndKeepScheduler | None = None
        self._tick_counter = 0
        self._storage_root: Path | None = None

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
        if self._capture is not None:
            self._capture.close()
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
            capture = self._require_capture(sender)
            if capture is None:
                return True
            if capture.start_capture(scheduled_for=None):
                sender.send_message("EndKeep snapshot capture started.")
            else:
                sender.send_error_message("EndKeep is already processing a capture.")
            return True
        if action == "list":
            self._command_list(sender)
            return True
        if action == "maintenance":
            full = len(args) > 1 and args[1] == "full"
            self._command_maintenance(sender, full=full)
            return True
        if action == "verify":
            self._command_verify(sender)
            return True
        if action == "reload":
            self._command_reload(sender)
            return True
        return False

    def _configure_runtime(self) -> None:
        runtime_config = EndKeepConfig.from_mapping(self.config)
        storage_root = runtime_config.storage.path
        if not storage_root.is_absolute():
            storage_root = Path.cwd() / storage_root
        storage_root = storage_root.resolve()
        storage_root.mkdir(parents=True, exist_ok=True)

        source_root = (Path.cwd() / "worlds").resolve()
        raw_store = RawSnapshotStore(storage_root, source_root)

        self._runtime_config = runtime_config
        self._storage_root = storage_root
        self._capture = CaptureCoordinator(self, raw_store)
        self._clock = EndKeepScheduler(
            runtime_config,
            SchedulerState(storage_root / "scheduler-state.json"),
            self._dispatch_schedule_event,
        )

    def _tick(self) -> None:
        capture = self._capture
        if capture is not None:
            capture.pump()

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
            capture = self._capture
            if capture is None:
                return
            if not capture.start_capture(scheduled_for=event.configured_time):
                self.logger.error(
                    f"CAPTURE FAILURE: scheduled capture {event.configured_time} skipped because capture is busy"
                )
            return

        self._run_scheduled_maintenance(event)

    def _run_scheduled_maintenance(self, event: ScheduleEvent) -> None:
        # Wired to the repository maintenance service in M3. Keeping the scheduler
        # dispatch point explicit prevents heavy work from leaking into this class.
        self.logger.info(
            f"Maintenance {event.configured_time} ({event.maintenance_mode}) is due; repository service not ready yet."
        )

    def _send_status(self, sender: CommandSender) -> None:
        config = self._runtime_config
        capture = self._capture
        if config is None or capture is None or self._storage_root is None:
            sender.send_error_message("EndKeep is not configured.")
            return
        status = capture.status()
        raw_root = self._storage_root / "raw"
        pending = 0
        if raw_root.exists():
            pending = sum(
                1
                for path in raw_root.iterdir()
                if path.is_dir() and path.name != ".incoming"
            )
        sender.send_message(
            f"EndKeep: enabled={config.enabled} capture={status.state} held={status.held} "
            f"raw_pending={pending} storage={self._storage_root}"
        )

    def _command_list(self, sender: CommandSender) -> None:
        sender.send_message("Repository listing will be available after logical repository initialization.")

    def _command_maintenance(self, sender: CommandSender, *, full: bool) -> None:
        mode = "FULL" if full else "LOGIC_ONLY"
        sender.send_message(f"{mode} maintenance will be available after logical repository initialization.")

    def _command_verify(self, sender: CommandSender) -> None:
        sender.send_message("Repository verification will be available after logical repository initialization.")

    def _command_reload(self, sender: CommandSender) -> None:
        if self._capture is not None and self._capture.busy:
            sender.send_error_message("Cannot reload EndKeep configuration while capture is active.")
            return

        old_capture = self._capture
        try:
            self.reload_config()
            new_config = EndKeepConfig.from_mapping(self.config)
        except ConfigError as exc:
            sender.send_error_message(f"Invalid EndKeep config: {exc}")
            return
        except Exception as exc:
            sender.send_error_message(f"Failed to reload EndKeep config: {exc}")
            return

        if old_capture is not None:
            old_capture.close()
        try:
            # reload_config already refreshed self.config; build all runtime services.
            self._runtime_config = new_config
            self._configure_runtime()
        except Exception as exc:
            sender.send_error_message(f"Failed to apply EndKeep config: {exc}")
            return
        sender.send_message("EndKeep configuration reloaded.")

    def _require_capture(self, sender: CommandSender) -> CaptureCoordinator | None:
        if self._runtime_config is None or not self._runtime_config.enabled:
            sender.send_error_message("EndKeep is disabled.")
            return None
        if self._capture is None:
            sender.send_error_message("EndKeep capture service is unavailable.")
            return None
        return self._capture
