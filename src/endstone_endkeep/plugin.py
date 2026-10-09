from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, override

from endstone.command import Command, CommandSender
from endstone.plugin import Plugin

from .config import ConfigError, EndKeepConfig, reconcile_config_file
from .coordinator import CaptureCoordinator
from .scheduler import EndKeepScheduler, ScheduleEvent, SchedulerState
from .staging.raw import RawSnapshotStore
from .worker.client import RepositoryWorkerClient, WorkerError, WorkerTimeout


class EndKeepPlugin(Plugin):
    prefix = "EndKeep"
    api_version = "0.11"

    AUTO_RPC_RETRY_TICKS = 20

    commands = {
        "backup": {
            "description": "Manage EndKeep backups",
            "usages": [
                "/backup (status|create|list|cancel|reload)<action: EndKeepBackupAction>",
                "/backup (verify)<action: EndKeepVerifyAction> (deep)[mode: EndKeepVerifyMode]",
                "/backup (maintenance)<action: EndKeepMaintenanceAction> (full)[mode: EndKeepMaintenanceMode]",
                "/backup (delete|rollover)<action: EndKeepMutationAction> <snapshot: str> "
                "(confirm)[approval: EndKeepMutationApproval] [generation: int]",
                "/backup (export)<action: EndKeepExportAction> <snapshot: str>",
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
        self._repository: RepositoryWorkerClient | None = None
        self._clock: EndKeepScheduler | None = None
        self._schedule_tick_counter = 0
        self._worker_poll_tick_counter = 0
        self._storage_root: Path | None = None
        self._pending_capture = False
        self._pending_capture_scheduled_for: str | None = None
        self._pending_capture_required_bytes = 0
        self._pending_result_ack: int | None = None
        self._worker_start_retry_ticks = 0

    @override
    def on_enable(self) -> None:
        self.save_default_config()
        try:
            self._reconcile_and_reload_config()
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
            deep = len(args) == 2 and args[1] == "deep"
            if len(args) > 2 or (len(args) == 2 and not deep):
                return False
            self._command_verify(sender, deep=deep)
            return True
        if action in ("delete", "rollover"):
            if len(args) not in (2, 4):
                return False
            if len(args) == 4 and (args[2] != "confirm" or not args[3].isdigit()):
                return False
            self._command_mutation(
                sender, action, args[1],
                expected_generation=int(args[3]) if len(args) == 4 else None,
            )
            return True
        if action == "export":
            if len(args) != 2:
                return False
            self._command_export(sender, args[1])
            return True
        if action == "cancel":
            self._command_cancel(sender)
            return True
        if action == "reload":
            self._command_reload(sender)
            return True
        return False

    def _reconcile_and_reload_config(self) -> None:
        path = Path(self.data_folder) / "config.toml"
        changed = reconcile_config_file(path)
        self.reload_config()
        if changed:
            self.logger.info("Updated config.toml with missing default settings.")

    def _configure_runtime(self, runtime_config: EndKeepConfig | None = None) -> None:
        config = runtime_config or EndKeepConfig.from_mapping(self.config)
        storage_root = config.storage.path
        if not storage_root.is_absolute():
            storage_root = Path.cwd() / storage_root
        storage_root = storage_root.resolve()
        storage_root.mkdir(parents=True, exist_ok=True)

        repository = RepositoryWorkerClient.connect_or_start(
            storage_root,
            priority=config.worker.priority,
        )
        settings = {
            "compression_level": config.logical.compression_level,
            "compression_threads": config.logical.compression_threads,
            "max_pending": config.raw.max_pending,
            "max_age_days": config.raw.max_age_days,
            "min_free_space_gib": config.storage.min_free_space_gib,
            "keep_days": config.retention.keep_days,
            "keep_last": config.retention.keep_last,
            "verify_mode": config.verify.mode,
        }
        recovery = repository.configure(settings)

        min_free_bytes = config.storage.min_free_space_gib * 1024**3

        def free_space_allows(additional_bytes: int = 0) -> bool:
            free = shutil.disk_usage(storage_root).free
            return free - additional_bytes >= min_free_bytes

        source_root = (Path.cwd() / "worlds").resolve()
        raw_store = RawSnapshotStore(storage_root, source_root)
        capture = CaptureCoordinator(
            self,
            raw_store,
            space_guard=free_space_allows,
            space_recovery=self._request_capture_space_recovery,
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
        self._pending_capture_required_bytes = 0
        self._pending_result_ack = None
        self._worker_start_retry_ticks = 0
        self._worker_poll_tick_counter = 0
        self._schedule_tick_counter = 0

        pending_raw = recovery.get("pending_raw", ())
        self.logger.info(
            f"Startup recovery: pending_raw={len(pending_raw)} "
            f"committed_raw_removed={recovery.get('removed_committed_raw', 0)} "
            f"recovered_head={recovery.get('recovered_head')}"
        )
        if repository.priority_deferred:
            self.logger.warning(
                f"Worker priority change to {config.worker.priority} is deferred while the current "
                "worker remains active; reload EndKeep again after the job completes to apply it."
            )
        if repository.upgrade_deferred:
            self.logger.warning(
                f"Repository worker {repository.runtime.version} is still active while the plugin is newer; "
                "reload EndKeep again after the current job/result is complete to start the new worker build."
            )

    def _close_runtime(self) -> None:
        capture = self._capture
        repository = self._repository
        self._capture = None
        self._repository = None
        self._clock = None
        self._pending_capture = False
        self._pending_capture_scheduled_for = None
        self._pending_capture_required_bytes = 0
        self._pending_result_ack = None
        self._worker_start_retry_ticks = 0
        if capture is not None:
            capture.close()
        if repository is not None:
            repository.detach()

    def _tick(self) -> None:
        capture = self._capture
        repository = self._repository

        if capture is not None:
            capture.pump()

        if self._worker_start_retry_ticks > 0:
            self._worker_start_retry_ticks -= 1

        self._worker_poll_tick_counter += 1
        if repository is not None and self._worker_poll_tick_counter >= 20:
            self._worker_poll_tick_counter = 0
            if self._pending_result_ack is not None:
                try:
                    acknowledged = repository.ack_result(self._pending_result_ack)
                except WorkerTimeout:
                    acknowledged = False
                except Exception as exc:
                    self.logger.error(f"REPOSITORY FAILURE: worker result acknowledgement failed: {exc}")
                    acknowledged = False
                if acknowledged:
                    self._pending_result_ack = None

            if self._pending_result_ack is None:
                try:
                    polled = repository.poll()
                except WorkerTimeout:
                    polled = None
                except Exception as exc:
                    self.logger.error(f"REPOSITORY FAILURE: worker communication failed: {exc}")
                    self._clear_pending_capture()
                    polled = None
                if polled is not None:
                    result_id, result = polled
                    try:
                        self._handle_repository_result(result)
                    except Exception as exc:
                        self.logger.error(f"REPOSITORY FAILURE: failed to handle worker result: {exc}")
                    else:
                        self._pending_result_ack = result_id
                        try:
                            if repository.ack_result(result_id):
                                self._pending_result_ack = None
                        except WorkerTimeout:
                            pass
                        except Exception as exc:
                            self.logger.error(f"REPOSITORY FAILURE: worker result acknowledgement failed: {exc}")

        if (
            self._pending_capture
            and repository is not None
            and not repository.busy
            and self._worker_start_retry_ticks == 0
        ):
            try:
                accepted = repository.start_pre_capture(
                    required_bytes=self._pending_capture_required_bytes,
                    timeout=RepositoryWorkerClient.AUTO_CONTROL_TIMEOUT_SECONDS,
                )
            except WorkerError as exc:
                self._arm_worker_start_retry()
                self.logger.warning(f"REPOSITORY WARNING: pre-capture control RPC failed; retrying in 1s: {exc}")
            else:
                if accepted:
                    self._worker_start_retry_ticks = 0
                else:
                    self._arm_worker_start_retry()

        self._dispatch_pending_maintenance()

        self._schedule_tick_counter += 1
        if self._schedule_tick_counter < 200:
            return
        self._schedule_tick_counter = 0

        config = self._runtime_config
        clock = self._clock
        if config is None or clock is None or not config.enabled:
            return
        try:
            clock.tick()
        except Exception as exc:
            self.logger.error(f"Scheduler failure: {exc}")

    def _dispatch_schedule_event(self, event: ScheduleEvent) -> bool:
        if event.kind == "capture":
            accepted = self._request_capture(scheduled_for=event.configured_time)
            if not accepted:
                self.logger.error(f"CAPTURE FAILURE: scheduled capture {event.configured_time} could not be accepted")
            return accepted

        repository = self._repository
        capture = self._capture
        if repository is None or event.maintenance_mode is None:
            return False
        if (capture is not None and capture.busy) or self._pending_capture:
            return False

        if event.request_id is not None and repository.current_request_id == event.request_id:
            self.logger.info(
                f"Scheduled {event.maintenance_mode} maintenance at {event.configured_time} "
                "is already covered by the accepted worker request."
            )
            return True

        status = repository.status
        current_job = status.get("job", {})
        if repository.busy and current_job.get("kind") == "maintenance":
            current_mode = current_job.get("mode")
            if current_mode == "FULL" or current_mode == event.maintenance_mode:
                self.logger.info(
                    f"Scheduled {event.maintenance_mode} maintenance at {event.configured_time} "
                    f"is covered by the running {current_mode} maintenance."
                )
                return True
            return False
        if repository.busy or self._worker_start_retry_ticks > 0:
            return False

        try:
            accepted = repository.start_maintenance(
                event.maintenance_mode,
                request_id=event.request_id,
                timeout=RepositoryWorkerClient.AUTO_CONTROL_TIMEOUT_SECONDS,
            )
        except WorkerError as exc:
            self._arm_worker_start_retry()
            self.logger.warning(f"REPOSITORY WARNING: scheduled maintenance control RPC failed; retrying in 1s: {exc}")
            return False
        if not accepted:
            self._arm_worker_start_retry()
        if accepted:
            self.logger.info(f"Scheduled {event.maintenance_mode} maintenance started ({event.configured_time}).")
        return accepted

    def _dispatch_pending_maintenance(self) -> None:
        clock = self._clock
        repository = self._repository
        capture = self._capture
        if clock is None or repository is None:
            return
        if (
            repository.busy
            or self._pending_capture
            or (capture is not None and capture.busy)
            or self._worker_start_retry_ticks > 0
        ):
            return
        pending = clock.pending_maintenance
        if pending is None:
            return
        try:
            started = clock.dispatch_pending()
        except Exception as exc:
            self.logger.error(f"Scheduler failure while dispatching queued maintenance: {exc}")
            return
        if started is not None:
            self.logger.info(
                f"Queued {started.maintenance_mode} maintenance started (original window {started.configured_time})."
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
        self._pending_capture_required_bytes = 0
        if not repository.busy and self._worker_start_retry_ticks == 0:
            try:
                accepted = repository.start_pre_capture(
                    timeout=RepositoryWorkerClient.AUTO_CONTROL_TIMEOUT_SECONDS,
                )
            except WorkerError as exc:
                self._arm_worker_start_retry()
                self.logger.warning(f"REPOSITORY WARNING: pre-capture control RPC failed; retrying in 1s: {exc}")
            else:
                if accepted:
                    self._worker_start_retry_ticks = 0
                else:
                    self._arm_worker_start_retry()
        return True

    def _request_capture_space_recovery(self, required_bytes: int, scheduled_for: str | None) -> bool:
        config = self._runtime_config
        capture = self._capture
        repository = self._repository
        if config is None or not config.enabled or capture is None or repository is None:
            return False
        if capture.busy or self._pending_capture or repository.busy:
            return False

        self._pending_capture = True
        self._pending_capture_scheduled_for = scheduled_for
        self._pending_capture_required_bytes = required_bytes
        if self._worker_start_retry_ticks > 0:
            return True

        try:
            accepted = repository.start_pre_capture(
                required_bytes=required_bytes,
                timeout=RepositoryWorkerClient.AUTO_CONTROL_TIMEOUT_SECONDS,
            )
        except WorkerError as exc:
            self._arm_worker_start_retry()
            self.logger.warning(f"REPOSITORY WARNING: free-space recovery control RPC failed; retrying in 1s: {exc}")
            return True
        if not accepted:
            self._arm_worker_start_retry()
        else:
            self._worker_start_retry_ticks = 0
        return True

    def _handle_repository_result(self, result: dict[str, Any]) -> None:
        request_id = result.get("request_id")
        if isinstance(request_id, str) and self._clock is not None:
            self._clock.complete_pending(request_id)

        kind = result.get("kind")
        if kind == "error":
            self.logger.error(f"REPOSITORY FAILURE: worker job failed: {result.get('error', 'unknown error')}")
            self._clear_pending_capture()
            return

        if kind == "cancelled":
            cancelled_kind = result.get("cancelled_kind", "repository")
            self.logger.info(f"{cancelled_kind} job cancelled safely.")
            if cancelled_kind == "pre_capture":
                self._clear_pending_capture()
            return

        if kind == "pre_capture":
            limits = result.get("raw_limits")
            if not isinstance(limits, dict):
                self.logger.error("REPOSITORY FAILURE: pre-capture job returned no result")
                self._clear_pending_capture()
                return

            logicalized = tuple(limits.get("logicalized") or ())
            dropped = tuple(limits.get("dropped") or ())
            if logicalized:
                self.logger.info(
                    f"Pre-capture logicalized {len(logicalized)} raw snapshot(s): {', '.join(logicalized)}"
                )
            if dropped:
                self.logger.critical(
                    f"CRITICAL RAW LIMIT: dropped oldest pending raw snapshot(s): {', '.join(dropped)}"
                )
            if limits.get("blocked_for_space"):
                self.logger.error("CAPTURE FAILURE: free space is below configured reserve after raw-limit enforcement")
                self._clear_pending_capture()
                return

            capture = self._capture
            scheduled_for = self._pending_capture_scheduled_for
            self._clear_pending_capture()
            if capture is None or not capture.start_capture(scheduled_for=scheduled_for):
                self.logger.error("CAPTURE FAILURE: capture became unavailable after pre-capture checks")
            return

        if kind == "maintenance":
            maintenance = result.get("maintenance")
            if isinstance(maintenance, dict):
                self._log_maintenance(maintenance)
            return

        if kind == "mutation":
            summary = result.get("mutation")
            if isinstance(summary, dict):
                self.logger.info(
                    f"Repository {summary.get('operation')} committed: snapshot={summary.get('snapshot')} "
                    f"generation={summary.get('generation')} remaining={summary.get('remaining')} "
                    f"base={summary.get('base')}"
                )
            return

        if kind == "export":
            summary = result.get("export")
            if isinstance(summary, dict):
                self.logger.info(
                    f"Repository export complete: snapshot={summary.get('snapshot')} "
                    f"destination={summary.get('destination')}"
                )
            return

        if kind == "verify":
            report = result.get("verify")
            if isinstance(report, dict):
                mode = "deep" if report.get("deep") else "normal"
                self.logger.info(
                    f"Repository {mode} verify PASS: generation={report.get('generation')} "
                    f"snapshots={report.get('snapshots')} objects={report.get('referenced_objects')} "
                    f"orphans={report.get('orphan_objects')}"
                )

    def _log_maintenance(self, result: dict[str, Any]) -> None:
        committed = list(result.get("committed") or ())
        failures = list(result.get("failures") or ())
        mode = result.get("mode", "UNKNOWN")
        self.logger.info(f"{mode} maintenance complete: committed={len(committed)} failures={len(failures)}")
        for item in committed:
            self.logger.info(
                f"Logicalized {item['snapshot_id']}: type={item['node_type']} records={item['records']} "
                f"changed={item['changed']} inserted={item['inserted']} deleted={item['deleted']} "
                f"logical_bytes={item['logical_bytes']} compressed_bytes={item['compressed_bytes']} "
                f"elapsed={item['elapsed_seconds']:.3f}s"
            )
        for failure in failures:
            self.logger.error(f"LOGICALIZATION FAILURE: snapshot={failure.get('snapshot_id')}: {failure.get('error')}")
        rollover = result.get("rollover")
        if isinstance(rollover, dict):
            self.logger.info(
                f"Retention rollover: new_base={rollover.get('new_base_snapshot')} "
                f"absorbed={len(rollover.get('absorbed_snapshots') or ())}"
            )
        gc = result.get("gc")
        if isinstance(gc, dict):
            self.logger.info(f"GC: removed_objects={gc.get('removed_objects')} removed_bytes={gc.get('removed_bytes')}")
        verify = result.get("verify")
        if isinstance(verify, dict):
            self.logger.info(
                f"{'Deep' if verify.get('deep') else 'Structural'} verify: "
                f"generation={verify.get('generation')} "
                f"snapshots={verify.get('snapshots')} orphans={verify.get('orphan_objects')}"
            )

    def _clear_pending_capture(self) -> None:
        self._pending_capture = False
        self._pending_capture_scheduled_for = None
        self._pending_capture_required_bytes = 0

    def _arm_worker_start_retry(self) -> None:
        self._worker_start_retry_ticks = self.AUTO_RPC_RETRY_TICKS

    def _send_status(self, sender: CommandSender) -> None:
        config = self._runtime_config
        capture = self._capture
        repository = self._repository
        if config is None or capture is None or repository is None or self._storage_root is None:
            sender.send_error_message("EndKeep is not configured.")
            return

        try:
            worker_status = repository.refresh()
        except Exception as exc:
            sender.send_error_message(f"EndKeep repository worker is unavailable: {exc}")
            return

        capture_status = capture.status()
        repo = worker_status.get("repository", {})
        worker = worker_status.get("worker", {})
        job = worker_status.get("job", {})
        pending = self._clock.pending_maintenance if self._clock is not None else None

        sender.send_message(
            f"EndKeep: enabled={config.enabled} capture={capture_status.state} held={capture_status.held} "
            f"repository={self._repository_label(job)} raw_pending={repo.get('raw_pending')} "
            f"generation={repo.get('generation')} snapshots={repo.get('snapshots')} storage={self._storage_root}"
        )

        stages = tuple(job.get("stages") or ())
        state = job.get("state")
        if state in ("running", "cancel_requested") and stages:
            stage_index = int(job.get("stage_index", 0))
            rendered = [f"[{stage}]" if index == stage_index else str(stage) for index, stage in enumerate(stages)]
            sender.send_message("  ".join(rendered))
            sender.send_message(self._format_progress(job))

        if pending is not None:
            if pending.request_id is not None and pending.request_id == repository.current_request_id:
                sender.send_message(f"Schedule: {pending.maintenance_mode} maintenance (accepted)")
            else:
                sender.send_message(f"Next: {pending.maintenance_mode} maintenance (queued)")

        priority = worker.get("priority")
        priority_text = str(priority)
        if repository.priority_deferred:
            priority_text += f" -> {config.worker.priority} (deferred)"
        version_text = repository.runtime.version
        if repository.upgrade_deferred:
            version_text += " (upgrade deferred)"
        settings_text = "deferred" if worker_status.get("settings_deferred") else "active"
        sender.send_message(
            f"Worker: pid={worker.get('pid')} version={version_text} "
            f"priority={priority_text} full_verify={config.verify.mode} settings={settings_text}"
        )

    @staticmethod
    def _repository_label(job: dict[str, Any]) -> str:
        state = job.get("state")
        if state == "idle":
            return "idle"
        kind = str(job.get("kind") or "busy")
        mode = job.get("mode")
        if mode:
            kind = str(mode)
        if state == "cancel_requested":
            return f"{kind}(cancel-requested)"
        return kind

    @staticmethod
    def _format_progress(job: dict[str, Any]) -> str:
        current = job.get("current")
        total = job.get("total")
        unit = job.get("unit")
        detail = job.get("detail")
        snapshot = job.get("snapshot")
        elapsed = float(job.get("elapsed_seconds") or 0.0)
        stage_elapsed = float(job.get("stage_elapsed_seconds") or 0.0)
        approximate = bool(job.get("approximate", False))
        logicalizing = detail in ("scan+compress", "diff+compress")

        pieces: list[str] = []
        if logicalizing and current is not None:
            if total not in (None, 0):
                percent = min(100.0, float(current) * 100.0 / float(total))
                prefix = "~" if approximate else ""
                pieces.append(f"{prefix}{percent:.1f}%")
            pieces.append(f"{EndKeepPlugin._format_count(int(current))} {unit or ''}".strip())
            if stage_elapsed > 0.0:
                rate_k = float(current) / stage_elapsed / 1000.0
                pieces.append(f"{rate_k:.1f}k/s")
        elif current is not None and total not in (None, 0):
            percent = float(current) * 100.0 / float(total)
            pieces.append(
                f"{EndKeepPlugin._format_count(int(current))} / "
                f"{EndKeepPlugin._format_count(int(total))} {unit or ''} ({percent:.1f}%)".strip()
            )
        elif current is not None:
            pieces.append(f"{EndKeepPlugin._format_count(int(current))} {unit or ''}".strip())
        elif detail:
            pieces.append(str(detail))

        if snapshot:
            pieces.append(f"snapshot={snapshot}")
        display_elapsed = stage_elapsed if logicalizing else elapsed
        pieces.append(f"elapsed={EndKeepPlugin._format_elapsed(display_elapsed)}")
        return "Progress: " + " · ".join(pieces)

    @staticmethod
    def _format_count(value: int) -> str:
        if abs(value) >= 1_000_000:
            return f"{value / 1_000_000:.2f}M"
        return str(value)

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def _command_list(self, sender: CommandSender) -> None:
        repository = self._repository
        if repository is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        try:
            info = repository.list_snapshots()
        except Exception as exc:
            sender.send_error_message(f"REPOSITORY FAILURE: {exc}")
            return

        snapshots = list(info.get("snapshots") or ())
        if not snapshots:
            sender.send_message("EndKeep repository is empty.")
            return
        sender.send_message(f"EndKeep repository generation {info.get('generation')}: {len(snapshots)} snapshot(s)")
        for node in snapshots[-20:]:
            sender.send_message(
                f"{node['snapshot']} {str(node['type']).upper()} {node['captured_at']} "
                f"records={node['records']} state={str(node['state_sha256'])[:12]}"
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
        try:
            accepted = repository.start_maintenance(mode)
        except Exception as exc:
            sender.send_error_message(f"Failed to start maintenance: {exc}")
            return
        if not accepted:
            sender.send_error_message("EndKeep repository is busy.")
            return
        sender.send_message(f"{mode} maintenance started.")

    def _command_verify(self, sender: CommandSender, *, deep: bool) -> None:
        repository = self._repository
        capture = self._capture
        if repository is None or capture is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        if capture.busy or self._pending_capture:
            sender.send_error_message("Cannot verify while capture is active or pending.")
            return

        try:
            accepted = repository.start_verify(deep=deep)
        except Exception as exc:
            sender.send_error_message(f"Failed to start repository verification: {exc}")
            return
        if not accepted:
            sender.send_error_message("EndKeep repository is busy.")
            return
        mode = "Deep" if deep else "Normal"
        sender.send_message(f"{mode} repository verification started.")

    def _command_mutation(
        self, sender: CommandSender, operation: str, snapshot: str, *, expected_generation: int | None
    ) -> None:
        repository = self._repository
        capture = self._capture
        if repository is None or capture is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        if capture.busy or self._pending_capture:
            sender.send_error_message("Cannot change repository while capture is active or pending.")
            return
        if expected_generation is None:
            try:
                plan = repository.plan_mutation(operation, snapshot)
            except Exception as exc:
                sender.send_error_message(f"Cannot plan {operation}: {exc}")
                return
            removed = list(plan["removed"])
            preview = ", ".join(removed[:4])
            if len(removed) > 4:
                preview += f" (+{len(removed) - 4} more)"
            sender.send_message(
                f"Preview: {operation} {snapshot}; generation={plan['generation']}; "
                f"impact={plan['detail']}; removes={preview}; remains={plan['remaining']}."
            )
            sender.send_message(
                f"To commit: /backup {operation} {snapshot} confirm {plan['generation']}"
            )
            return
        try:
            accepted = repository.start_mutation(
                operation, snapshot, expected_generation=expected_generation
            )
        except Exception as exc:
            sender.send_error_message(f"Failed to start {operation}: {exc}")
            return
        if not accepted:
            sender.send_error_message("EndKeep repository is busy.")
            return
        sender.send_message(
            f"{operation} accepted for {snapshot} (expected generation {expected_generation}). "
            "Check /backup status and server logs for completion."
        )

    def _command_export(self, sender: CommandSender, snapshot: str) -> None:
        repository = self._repository
        capture = self._capture
        if repository is None or capture is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        if capture.busy or self._pending_capture:
            sender.send_error_message("Cannot export while capture is active or pending.")
            return
        try:
            accepted = repository.start_export(snapshot)
        except Exception as exc:
            sender.send_error_message(f"Failed to start export: {exc}")
            return
        if not accepted:
            sender.send_error_message("EndKeep repository is busy.")
            return
        sender.send_message(
            f"Export accepted: {snapshot}. Output: {self._storage_root / 'exports' / snapshot}. "
            "Use /backup status to monitor; check server logs for completion."
        )

    def _command_cancel(self, sender: CommandSender) -> None:
        repository = self._repository
        if repository is None:
            sender.send_error_message("EndKeep repository service is unavailable.")
            return
        try:
            accepted = repository.cancel()
        except Exception as exc:
            sender.send_error_message(f"Failed to request cancellation: {exc}")
            return
        if not accepted:
            sender.send_error_message("No cancellable repository task is running.")
            return
        sender.send_message("Cancellation requested; the current repository task will stop at the next safe point.")

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
            self._reconcile_and_reload_config()
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
