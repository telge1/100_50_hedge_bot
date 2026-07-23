"""Deterministic profitable TEM control selection (outcome metadata only).

Selection is frozen before any scanner output is inspected.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.regime_scanner.tem_structure_break.eval_common import (
    CONTINUOUS_SRC,
    TradeSpec,
    csv_dicts,
)

SELECTION_RULE_ID = "tem_structure_break_controls_v1_20260723"

SELECTION_RULE_DOC = """
Eligible universe (pre-scanner, outcome-only):
  - source: staging_profiles_continuous_1000_500_20260722/continuous_trades.csv
  - profile == two_early_medium
  - trade_flat == 1
  - is_blocker == 0
  - total_pnl > 0

Deterministic inclusion (no randomness, no scanner features):
  1) Include ALL eligible trades with max_cycle >= 3
     (stress then profitable recovery: cycles 3/4/5).
  2) Include ALL eligible trades with max_cycle == 2.
  3) For each coin present in the 27-blocker set, include the single
     earliest (by start_bar, then trade_id) eligible trade with max_cycle == 1
     if that coin is not already represented by steps 1–2.
     If already represented, still add that coin's earliest max_cycle==1
     trade to keep a low-cycle profitable baseline per coin.

Sort final list by (coin, start_bar, trade_id).
No cherry-picking by chart, scanner signal, or expected timestamps.
"""


def select_control_specs(blocker_coins: set[str]) -> tuple[list[TradeSpec], list[dict[str, Any]]]:
    rows = [
        r
        for r in csv_dicts(CONTINUOUS_SRC / "continuous_trades.csv")
        if r.get("profile") == "two_early_medium"
        and str(r.get("trade_flat")) == "1"
        and str(r.get("is_blocker")) == "0"
        and float(r.get("total_pnl") or 0) > 0
    ]
    selected: dict[str, dict[str, Any]] = {}
    audit: list[dict[str, Any]] = []

    def add(row: dict[str, str], reason: str) -> None:
        tid = row["trade_id"]
        if tid in selected:
            return
        selected[tid] = {**row, "selection_reason": reason}
        audit.append(
            {
                "coin": row["coin"],
                "trade_id": tid,
                "entry_ts": row.get("first_timestamp"),
                "entry_price": None,  # filled at runtime from bar close
                "final_pnl": float(row["total_pnl"]),
                "highest_cycle": int(float(row["max_cycle"])),
                "duration": int(float(row["duration_candles"])),
                "flat_ts": row.get("last_timestamp"),
                "selection_reason": reason,
                "start_bar": int(float(row["start_bar"])),
                "flat_bar": int(float(row["flat_bar"])) if row.get("flat_bar") else None,
            }
        )

    high = [r for r in rows if int(float(r["max_cycle"])) >= 3]
    mid = [r for r in rows if int(float(r["max_cycle"])) == 2]
    low = [r for r in rows if int(float(r["max_cycle"])) == 1]

    for r in sorted(high, key=lambda x: (x["coin"], int(float(x["start_bar"])), x["trade_id"])):
        add(r, "all_profitable_flat_max_cycle_ge_3")
    for r in sorted(mid, key=lambda x: (x["coin"], int(float(x["start_bar"])), x["trade_id"])):
        add(r, "all_profitable_flat_max_cycle_eq_2")

    # earliest max_cycle==1 per blocker coin
    by_coin: dict[str, list[dict[str, str]]] = {}
    for r in low:
        if r["coin"] not in blocker_coins:
            continue
        by_coin.setdefault(r["coin"], []).append(r)
    for coin in sorted(by_coin):
        cand = sorted(by_coin[coin], key=lambda x: (int(float(x["start_bar"])), x["trade_id"]))[0]
        add(cand, "earliest_profitable_flat_max_cycle_eq_1_per_blocker_coin")

    specs: list[TradeSpec] = []
    for a in sorted(audit, key=lambda x: (x["coin"], x["start_bar"], x["trade_id"])):
        specs.append(
            TradeSpec(
                coin=a["coin"],
                trade_id=a["trade_id"],
                entry_ts=str(a["entry_ts"]),
                entry_price=0.0,  # filled from candle close at start_bar
                start_bar=int(a["start_bar"]),
                end_bar=int(a["flat_bar"]) if a.get("flat_bar") is not None else None,
                cohort="control",
                holdout_bucket="control",
                final_pnl=float(a["final_pnl"]),
                highest_cycle=int(a["highest_cycle"]),
                duration_bars=int(a["duration"]),
                flat_ts=str(a["flat_ts"]) if a.get("flat_ts") else None,
                selection_reason=a["selection_reason"],
            )
        )
    return specs, audit


def selection_manifest() -> dict[str, Any]:
    return {
        "selection_rule_id": SELECTION_RULE_ID,
        "source": str(CONTINUOUS_SRC / "continuous_trades.csv"),
        "documentation": SELECTION_RULE_DOC.strip(),
        "scanner_blind": True,
    }
