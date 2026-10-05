"""Frozen Long V1 geometry + ladder (imports only, no local rule changes)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from frozen_strategies.long_geometry_ladder24_be100_v1 import config as FC
from frozen_strategies.long_geometry_ladder24_be100_v1.geometry import entry_setup_long, row_for_signal
from frozen_strategies.long_geometry_ladder24_be100_v1.ladder import m15_lower_2_age_h, passes_ladder_filter


def evaluate_geometry_bar(
    symbol: str,
    pools: list[dict],
    bar: dict,
    moment: datetime,
    by_open: dict,
) -> dict[str, Any] | None:
    """Return signal payload or None if no geometry setup on this bar."""
    setup = entry_setup_long(pools, bar, moment)
    if setup is None:
        return None
    pool, bridge_lower, upper, gap = setup
    source_t = pool.get("source")
    birth = by_open.get(source_t, bar) if source_t is not None else bar
    sig = row_for_signal(symbol, pool, birth, bar, bridge_lower, upper, gap)
    age_h = m15_lower_2_age_h(pools, moment, sig["entry_price"])
    ladder_ok = passes_ladder_filter(age_h)
    stop_dist = (float(sig["entry_price"]) - float(sig["stop"])) / float(sig["entry_price"]) * 100.0
    tp_dist = (float(sig["tp"]) - float(sig["entry_price"])) / float(sig["entry_price"]) * 100.0
    return {
        **sig,
        "decision_time": moment.isoformat(),
        "m15_lower_2_age_h": age_h,
        "ladder_pass": ladder_ok,
        "be_trigger_pct": FC.BE_TRIGGER_MFE_PCT,
        "stop_distance_pct": round(stop_dist, 4),
        "tp_distance_pct": round(tp_dist, 4),
    }


def signal_id_for(sig: dict) -> str:
    from bot.shadow_signal_registry.models import LONG_STRATEGY, LONG_VERSION, make_signal_id

    return make_signal_id("long", LONG_STRATEGY, LONG_VERSION, sig["symbol"], sig["decision_time"], sig["pool_id"])
