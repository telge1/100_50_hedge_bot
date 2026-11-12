"""When chart drags should trigger MLB entry sync (not SL/TP-only drags)."""

from __future__ import annotations

from typing import Any

_ENTRY_DRAG_MODES = frozenset({"move", "resize-entry"})
_NO_EXCHANGE_SYNC_MODES = frozenset(
    {
        "resize-sl",
        "resize-tp",
        "resize-left",
        "resize-right",
    }
)


def drawing_drag_triggers_entry_sync(body: dict[str, Any] | None) -> bool:
    """
    Position drags always include entry_price in the payload even for SL/TP handles.
    Only entry-moving drags may call sync_pass → exchange entry AMEND.
    """
    if not body or body.get("type") != "drag":
        return False
    mode = str(body.get("mode") or "move").strip()
    if mode in _NO_EXCHANGE_SYNC_MODES:
        return False
    if mode in _ENTRY_DRAG_MODES:
        return True
    if mode.startswith("resize-") and mode not in _ENTRY_DRAG_MODES:
        return False
    return mode == "move"
