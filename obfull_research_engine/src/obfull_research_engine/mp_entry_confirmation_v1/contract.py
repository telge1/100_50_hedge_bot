"""Frozen entry confirmation contract + canonical hash (pre-outcome)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .params import (
    CONFIRMATION_TIMEOUT_MINUTES,
    CONFIRMATION_TF_MINUTES,
    DIAGNOSTIC_PAIRS,
    MAX_HOLDING_MINUTES,
    REQUIRED_RECLAIM_CLOSES,
    RETEST_TOLERANCE_PCT,
    STOP_PCT,
    STRUCTURE_LOOKBACK_BARS,
    TARGET_PCT,
)


def build_contract() -> dict[str, Any]:
    return {
        "contract_name": "frozen_entry_confirmation_contract_v1",
        "version": 1,
        "units": "percent",
        "frozen": True,
        "note": "Parameters frozen before outcome computation; do not retune from results.",
        "candle_confirmation_tf_minutes": CONFIRMATION_TF_MINUTES,
        "required_consecutive_closes": REQUIRED_RECLAIM_CLOSES,
        "structure_lookback_bars": STRUCTURE_LOOKBACK_BARS,
        "confirmation_timeout_minutes": CONFIRMATION_TIMEOUT_MINUTES,
        "retest_tolerance_pct": RETEST_TOLERANCE_PCT,
        "target_pct": TARGET_PCT,
        "stop_pct": STOP_PCT,
        "max_holding_minutes": MAX_HOLDING_MINUTES,
        "entry_rule": "open_of_first_1m_candle_with_open_time_ns_ge_confirmation_ts",
        "alert_candle_rule": "5m_bucket_containing_alert_ts_not_usable_as_complete_post_alert_bar",
        "first_allowed_5m": "first_5m_bucket_whose_open_is_strictly_after_alert_ts",
        "no_centered_pivots": True,
        "no_future_swing_lookback": True,
        "diagnostic_pairs_only": [list(p) for p in DIAGNOSTIC_PAIRS],
        "labels": {
            "FAILED_BREAK": "two_reclaim_closes_then_structure_break",
            "ABSORB": "alert_only_then_two_zone_holds_then_structure_break_with_reset",
            "TRUE_BREAK": "acceptance_retest_hold_continuation",
        },
    }


def canonical_hash(obj: dict[str, Any]) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def write_frozen_contract(path: Path) -> dict[str, Any]:
    contract = build_contract()
    h = canonical_hash(contract)
    out = dict(contract)
    out["contract_hash_sha256"] = h
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return out
