from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PendingMutation:
    operation: str
    snapshot: str
    generation: int


class MutationConfirmations:
    """Per-sender previews. Persist only restart markers, never executable requests."""

    def __init__(self, data_folder: Path) -> None:
        self._marker = data_folder / "pending-confirmations.json"
        self._pending: dict[str, PendingMutation] = {}
        try:
            saved = json.loads(self._marker.read_text(encoding="utf-8"))
            valid = isinstance(saved, list) and all(isinstance(x, str) for x in saved)
            self._restarted = set(saved) if valid else set()
        except OSError, ValueError:
            self._restarted = set()

    def get(self, sender_key: str) -> PendingMutation | None:
        return self._pending.get(sender_key)

    def remember(self, sender_key: str, operation: str, snapshot: str, generation: int) -> None:
        self._pending[sender_key] = PendingMutation(operation, snapshot, generation)
        self._restarted.discard(sender_key)
        self._persist()

    def forget(self, sender_key: str) -> None:
        self._pending.pop(sender_key, None)
        self._restarted.discard(sender_key)
        self._persist()

    def was_restarted(self, sender_key: str) -> bool:
        if sender_key not in self._restarted:
            return False
        self._restarted.remove(sender_key)
        self._persist()
        return True

    def _persist(self) -> None:
        # Save only an existence marker so previews can never be replayed after
        # a restart, but /backup confirm can explain why its preview is gone.
        keys = sorted(self._pending.keys() | self._restarted)
        if not keys:
            self._marker.unlink(missing_ok=True)
            return
        self._marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(keys), encoding="utf-8")
        os.replace(temporary, self._marker)
