"""Bootstrap sys.path for read-only reuse of spread_recovery L1 watcher (no FSM edits)."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

_L1_PROBE = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research/manual_channel_pool_long_v1"
)
_LINE_PROBE = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research/manual_line_touch_probe_v1"
)
_V3AN = _L1_PROBE / "touch_watcher_research_v3an"
_V3AO = _L1_PROBE / "watcher_integration_v3ao"
_V3AK = _L1_PROBE / "substantial_entry_gate_v3ak"
_DASH = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard"
)


def bootstrap_l1_watcher_paths() -> None:
    for p in (_LINE_PROBE, _V3AN, _V3AO, _V3AK, _L1_PROBE, _DASH):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


@lru_cache(maxsize=1)
def default_watcher_config():
    bootstrap_l1_watcher_paths()
    from watcher_integration_v3ao.v3ao_context import DEFAULT_WATCHER_CONFIG

    return DEFAULT_WATCHER_CONFIG


@lru_cache(maxsize=1)
def watcher_replay_entrypoint():
    bootstrap_l1_watcher_paths()
    from watcher_fsm import replay_watcher_from_config
    from line_geometry import fmt_utc

    return replay_watcher_from_config, fmt_utc
