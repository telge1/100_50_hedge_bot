"""Runner FSM labels derived from entry + position state."""

from __future__ import annotations

from typing import Literal

from long_entry_chronology_v1 import LongEntryStateV1
from long_exit_lifecycle_v1 import LongExitStateV1

RunnerState = Literal[
    "WAIT_SETUP",
    "WATCH_SUPPORT",
    "WAIT_RECOVERY",
    "PENDING_BUY_LIMIT",
    "OPEN_LONG",
    "CLOSED",
]


def derive_runner_state(
    *,
    entry: LongEntryStateV1,
    exit_st: LongExitStateV1 | None,
    pool_history_ok: bool,
    ema_authorized: bool,
) -> RunnerState:
    if exit_st is not None and exit_st.closed:
        return "CLOSED"
    if entry.position is not None:
        return "OPEN_LONG"
    if entry.pending is not None:
        return "PENDING_BUY_LIMIT"
    if entry.touch_tracker.phase == "TOUCHED":
        return "WAIT_RECOVERY"
    if not pool_history_ok or not ema_authorized:
        return "WAIT_SETUP"
    return "WATCH_SUPPORT"
