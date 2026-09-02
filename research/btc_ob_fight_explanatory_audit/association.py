"""Heuristic liquidation ↔ public trade association sensitivity."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from research.btc_ob_fight.config import utc

ASSOCIATION_LABEL = "HEURISTIC_TEMPORAL_PRICE_ASSOCIATION"
NOT_DIRECT = "NOT_DIRECTLY_IDENTIFIED"


def build_association_sensitivity(
    liq_events: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    windows_ms: tuple[int, ...] = (100, 250, 500, 1000),
) -> list[dict[str, Any]]:
    buy_trades = [t for t in trades if t["side"] == "Buy"]
    total_buy = sum(t["notional"] for t in buy_trades)
    rows: list[dict[str, Any]] = []

    short_liqs = [e for e in liq_events if e["liquidated_side"] == "LIQUIDATED_SHORT"]

    for win_ms in windows_ms:
        delta = timedelta(milliseconds=win_ms)
        matched_liq = 0
        matched_buy_vol = 0.0
        multi_match_trades = 0
        unassigned_liq = 0

        for ev in short_liqs:
            et = datetime.fromisoformat(ev["event_time"].replace("Z", "+00:00"))
            bp = ev["bankruptcy_price"]
            hits = []
            for t in buy_trades:
                if abs((t["ts"] - et).total_seconds() * 1000) <= win_ms:
                    dist_bps = abs(t["price"] - bp) / bp * 10000.0 if bp else None
                    hits.append((t, dist_bps))
            if hits:
                matched_liq += 1
                matched_buy_vol += sum(t["notional"] for t, _ in hits)
                if len(hits) > 1:
                    multi_match_trades += len(hits)
            else:
                unassigned_liq += 1

        rows.append(
            {
                "sensitivity_window_ms": win_ms,
                "association_type": ASSOCIATION_LABEL,
                "identification_status": NOT_DIRECT,
                "short_liquidation_events": len(short_liqs),
                "events_with_temporal_buy_match": matched_liq,
                "overlapping_buy_notional_sum": matched_buy_vol,
                "fraction_of_total_taker_buy": matched_buy_vol / total_buy if total_buy else None,
                "multi_match_trade_count": multi_match_trades,
                "unassigned_liquidation_events": unassigned_liq,
                "double_counting_risk": "OVERLAPPING_BUY_NOTIONAL_MAY_COUNT_SAME_TRADE_MULTIPLE_TIMES",
            }
        )
    return rows
