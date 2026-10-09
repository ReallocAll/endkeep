from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from endstone_endkeep.plugin import EndKeepPlugin
from endstone_endkeep.worker.client import WorkerRequestIndeterminate

SNAPSHOT = "20261006-163000"


class Sender:
    def __init__(self, name: str) -> None:
        self.name = name
        self.messages: list[str] = []
        self.errors: list[str] = []

    def send_message(self, text: str) -> None:
        self.messages.append(text)

    def send_error_message(self, text: str) -> None:
        self.errors.append(text)


class Repository:
    generation = 7

    def __init__(self) -> None:
        self.mutations: list[tuple[str, str, int]] = []
        self.accepted = True
        self.uncertain = False

    def plan_mutation(self, operation: str, snapshot: str) -> dict:
        return {
            "operation": operation,
            "snapshot": snapshot,
            "generation": self.generation,
            "detail": "bridge DELTA",
            "removed": [snapshot],
            "remaining": 2,
        }

    def list_snapshots(self) -> dict:
        return {"generation": self.generation, "snapshots": []}

    def detach(self) -> None:
        pass

    def start_mutation(self, operation: str, snapshot: str, *, expected_generation: int) -> bool:
        if self.uncertain:
            raise WorkerRequestIndeterminate("unknown outcome")
        if self.accepted:
            self.mutations.append((operation, snapshot, expected_generation))
        return self.accepted


class CommandHarness:
    _confirmation_sender_key = staticmethod(EndKeepPlugin._confirmation_sender_key)
    _command_mutation = EndKeepPlugin._command_mutation
    _command_confirm = EndKeepPlugin._command_confirm
    on_command = EndKeepPlugin.on_command

    _close_runtime = EndKeepPlugin._close_runtime

    def __init__(self) -> None:
        self._repository = Repository()
        self._pending_mutations = {}
        self._capture = SimpleNamespace(busy=False, close=lambda: None)
        self._clock = None
        self._pending_capture = False
        self._pending_capture_scheduled_for = None
        self._pending_capture_required_bytes = 0
        self._pending_result_ack = None
        self._worker_start_retry_ticks = 0


def test_preview_confirm_has_no_expiry_or_manual_generation() -> None:
    harness = CommandHarness()
    operator = Sender("admin")
    cmd = SimpleNamespace(name="backup")

    assert harness.on_command(operator, cmd, ["delete", SNAPSHOT])
    assert operator.messages[-1].startswith("To commit: /backup confirm")
    assert harness._pending_mutations.get("Sender:admin") is not None
    assert harness.on_command(operator, cmd, ["confirm"])
    assert harness._repository.mutations == [("delete", SNAPSHOT, 7)]
    assert harness._pending_mutations.get("Sender:admin") is None
    assert not harness.on_command(operator, cmd, ["delete", SNAPSHOT, "confirm", "7"])
    assert harness.on_command(operator, cmd, ["confirm"])
    assert "No pending preview" in operator.errors[-1]


def test_repository_change_invalidates_only_that_preview() -> None:
    harness = CommandHarness()
    first = Sender("first")
    second = Sender("second")
    cmd = SimpleNamespace(name="backup")
    harness.on_command(first, cmd, ["delete", SNAPSHOT])
    harness.on_command(second, cmd, ["rollover", SNAPSHOT])
    assert harness._pending_mutations.get("Sender:first").operation == "delete"
    assert harness._pending_mutations.get("Sender:second").operation == "rollover"

    harness._repository.generation += 1
    harness.on_command(first, cmd, ["confirm"])
    assert "repository changed since preview" in first.errors[-1]
    assert "generation 7 -> 8" in first.errors[-1]
    assert "preview" in first.errors[-1].lower()
    assert harness._repository.mutations == []

    harness.on_command(second, cmd, ["confirm"])
    assert "generation 7 -> 8" in second.errors[-1]
    harness.on_command(first, cmd, ["delete", SNAPSHOT])
    harness.on_command(first, cmd, ["confirm"])
    assert harness._repository.mutations == [("delete", SNAPSHOT, 8)]


def test_restart_and_reload_discard_previews_without_disk_state(tmp_path: Path) -> None:
    harness = CommandHarness()
    sender = Sender("operator")
    cmd = SimpleNamespace(name="backup")
    harness.on_command(sender, cmd, ["delete", SNAPSHOT])
    assert harness._pending_mutations

    # Closing the plugin runtime invalidates pending confirmation.
    harness._close_runtime()
    assert harness._pending_mutations == {}
    harness.on_command(sender, cmd, ["confirm"])
    assert "No pending preview" in sender.errors[-1]
    assert harness._repository is None

    # New plugin instance after a process restart has no preview to recover.
    restarted = CommandHarness()
    restarted.on_command(sender, cmd, ["confirm"])
    assert "No pending preview" in sender.errors[-1]
    assert restarted._repository.mutations == []
    assert list(tmp_path.iterdir()) == []


def test_a_new_preview_replaces_the_old_one() -> None:
    harness = CommandHarness()
    sender = Sender("admin")
    cmd = SimpleNamespace(name="backup")
    harness.on_command(sender, cmd, ["delete", SNAPSHOT])
    harness.on_command(sender, cmd, ["rollover", SNAPSHOT])
    harness.on_command(sender, cmd, ["confirm"])
    assert harness._repository.mutations == [("rollover", SNAPSHOT, 7)]


def test_confirm_busy_preserves_preview_and_retry_is_safe() -> None:
    harness = CommandHarness()
    sender = Sender("admin")
    cmd = SimpleNamespace(name="backup")
    harness.on_command(sender, cmd, ["rollover", SNAPSHOT])

    harness._repository.accepted = False
    harness.on_command(sender, cmd, ["confirm"])
    assert "busy" in sender.errors[-1]
    assert harness._pending_mutations.get("Sender:admin") is not None
    harness._repository.accepted = True
    harness.on_command(sender, cmd, ["confirm"])
    assert harness._repository.mutations == [("rollover", SNAPSHOT, 7)]


def test_indeterminate_outcome_requires_new_preview() -> None:
    harness = CommandHarness()
    sender = Sender("admin")
    cmd = SimpleNamespace(name="backup")
    harness.on_command(sender, cmd, ["delete", SNAPSHOT])
    harness._repository.uncertain = True
    harness.on_command(sender, cmd, ["confirm"])
    assert "unknown outcome" in sender.errors[-1]
    assert harness._pending_mutations.get("Sender:admin") is None
