"""EndKeep crash-safe logical incremental backup plugin."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .plugin import EndKeepPlugin

__all__ = ["EndKeepPlugin"]


def __getattr__(name: str) -> Any:
    if name == "EndKeepPlugin":
        from .plugin import EndKeepPlugin

        return EndKeepPlugin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
