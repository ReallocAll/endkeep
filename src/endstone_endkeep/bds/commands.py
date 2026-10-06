from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from endstone import Server


@dataclass(frozen=True)
class CommandCapture:
    dispatched: bool
    messages: tuple[Any, ...]
    errors: tuple[Any, ...]


class BdsSaveError(RuntimeError):
    """Raised when a BDS save command cannot be dispatched successfully."""


class BdsSaveAdapter:
    """Thin isolation layer around Endstone's command dispatch/output-capture API."""

    def __init__(self, server: Server) -> None:
        self._server = server
        self._held = False

    @property
    def is_held(self) -> bool:
        return self._held

    def hold(self) -> CommandCapture:
        result = self._dispatch("save hold")
        if not result.dispatched or result.errors:
            raise BdsSaveError(f"save hold failed: {self._describe(result)}")
        self._held = True
        return result

    def query(self) -> CommandCapture:
        return self._dispatch("save query")

    def resume(self) -> CommandCapture:
        result = self._dispatch("save resume")
        if result.dispatched and not result.errors:
            self._held = False
        return result

    def best_effort_resume(self) -> bool:
        if not self._held:
            return True
        try:
            result = self.resume()
        except Exception:
            return False
        return result.dispatched and not result.errors

    def _dispatch(self, command_line: str) -> CommandCapture:
        # Imported lazily so pure unit-test modules do not need Endstone imported.
        from endstone.command import CommandSenderWrapper

        messages: list[Any] = []
        errors: list[Any] = []
        sender = CommandSenderWrapper(
            self._server.command_sender,
            on_message=messages.append,
            on_error=errors.append,
        )
        dispatched = self._server.dispatch_command(sender, command_line)
        return CommandCapture(bool(dispatched), tuple(messages), tuple(errors))

    @staticmethod
    def _describe(result: CommandCapture) -> str:
        if result.errors:
            return "; ".join(BdsSaveAdapter._message_text(message) for message in result.errors)
        if result.messages:
            return "; ".join(BdsSaveAdapter._message_text(message) for message in result.messages)
        return "command was not dispatched" if not result.dispatched else "unknown command failure"

    @staticmethod
    def _message_text(message: Any) -> str:
        text = getattr(message, "text", None)
        params = getattr(message, "params", None)
        if isinstance(text, str):
            if isinstance(params, list) and params:
                return f"{text}({', '.join(str(param) for param in params)})"
            return text
        return str(message)
