"""Registry models and field lists."""

from __future__ import annotations

from typing import Any, Literal

Side = Literal["long", "short"]

LONG_STRATEGY = "long_geometry_ladder24_be100_v1"
LONG_VERSION = "v1"
SHORT_STRATEGY = "e1r_cluster3_ema200_v1"
SHORT_VERSION = "v1"

BLOCK_REASON_NONE = "NONE"

LONG_SNAPSHOT_FIELDS = [
    "signal_id",
    "detected_at",
    "decision_time",
    "symbol",
    "side",
    "strategy_name",
    "strategy_version",
    "signal_status",
    "allowed",
    "blocked",
    "block_reason",
    "raw_block_reason",
    "hypothetical",
    "entry_price",
    "initial_sl",
    "active_sl",
    "tp",
    "pool_id",
    "m15_lower_2_age_h",
    "ladder24_pass",
    "be_trigger_pct",
    "be_triggered",
    "be_trigger_time",
    "tracking_status",
    "outcome",
    "exit_time",
    "exit_price",
    "pnl_pct",
    "mae_pct",
    "mfe_pct",
    "duration_min",
    "horizon_time",
    "backfilled",
    "backfill_source",
    "created_at",
    "updated_at",
    "last_processed_1m",
    "causality_status",
]

SHORT_SNAPSHOT_FIELDS = [
    "signal_id",
    "detected_at",
    "decision_time",
    "symbol",
    "side",
    "strategy_name",
    "strategy_version",
    "signal_status",
    "allowed",
    "blocked",
    "block_reason",
    "raw_block_reason",
    "hypothetical",
    "entry_price",
    "initial_sl",
    "active_sl",
    "tp",
    "pool_id",
    "e1r_state",
    "e1r_block_reason",
    "floor_guard_state",
    "tracking_status",
    "outcome",
    "exit_time",
    "exit_price",
    "pnl_pct",
    "mae_pct",
    "mfe_pct",
    "duration_min",
    "horizon_time",
    "backfilled",
    "backfill_source",
    "created_at",
    "updated_at",
    "last_processed_1m",
    "causality_status",
]


def make_signal_id(
    side: Side,
    strategy_name: str,
    strategy_version: str,
    symbol: str,
    decision_time: str,
    pool_id: str,
) -> str:
    return f"{side}|{strategy_name}|{strategy_version}|{symbol}|{decision_time}|{pool_id}"


def empty_snapshot_row(side: Side, signal_id: str, now_iso: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "signal_id": signal_id,
        "detected_at": now_iso,
        "decision_time": None,
        "symbol": None,
        "side": side.upper(),
        "strategy_name": LONG_STRATEGY if side == "long" else SHORT_STRATEGY,
        "strategy_version": LONG_VERSION if side == "long" else SHORT_VERSION,
        "signal_status": "DETECTED",
        "allowed": False,
        "blocked": False,
        "block_reason": BLOCK_REASON_NONE,
        "raw_block_reason": None,
        "hypothetical": False,
        "entry_price": None,
        "initial_sl": None,
        "active_sl": None,
        "tp": None,
        "pool_id": None,
        "tracking_status": "OPEN",
        "outcome": None,
        "exit_time": None,
        "exit_price": None,
        "pnl_pct": None,
        "mae_pct": 0.0,
        "mfe_pct": 0.0,
        "duration_min": None,
        "horizon_time": None,
        "backfilled": False,
        "backfill_source": None,
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_processed_1m": None,
        "causality_status": "PASS",
    }
    if side == "long":
        base.update(
            {
                "m15_lower_2_age_h": None,
                "ladder24_pass": None,
                "be_trigger_pct": 1.0,
                "be_triggered": False,
                "be_trigger_time": None,
            }
        )
    else:
        base.update(
            {
                "e1r_state": None,
                "e1r_block_reason": None,
                "floor_guard_state": None,
            }
        )
    return base
