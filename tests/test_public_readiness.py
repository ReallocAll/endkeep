from __future__ import annotations

from types import SimpleNamespace

from endstone_endkeep.plugin import EndKeepPlugin


def test_backup_list_reports_all_retained_snapshots() -> None:
    class Sender:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def send_message(self, message: str) -> None:
            self.messages.append(message)

        def send_error_message(self, message: str) -> None:
            raise AssertionError(message)

    snapshots = [
        {
            "snapshot": f"20261007-{index:06d}",
            "type": "delta",
            "captured_at": "2026-10-07T12:00:00+08:00",
            "records": 100,
            "state_sha256": "a" * 64,
        }
        for index in range(28)
    ]
    sender = Sender()
    repository = SimpleNamespace(list_snapshots=lambda: {"generation": 28, "snapshots": snapshots})
    EndKeepPlugin._command_list(SimpleNamespace(_repository=repository), sender)
    assert len(sender.messages) == len(snapshots) + 1
    assert all(row["snapshot"] in "\n".join(sender.messages) for row in snapshots)
