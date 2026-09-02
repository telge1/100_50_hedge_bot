"""Event-derived phase segmentation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from research.btc_ob_fight.config import iso_z
from research.btc_ob_fight.facts import window_trade_facts

from .config import (
    ANCHOR,
    CORE_END,
    CORE_START,
    TPO_VAH,
    UPPER_INNER,
    UPPER_OUTER,
    VOLUME_VVAH,
)


def _classify_price(p: float) -> str:
    if p >= VOLUME_VVAH:
        return "OUTSIDE_BREAK"
    if p >= TPO_VAH:
        return "EDGE_ZONE_ATTACK"
    return "PRE_ATTACK"


def derive_phases(
    trades: list[dict[str, Any]],
    liq_events: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    reclaim_cross_ts: str | None,
    *,
    peak_ts: datetime,
    peak_price: float,
    retest_ts: datetime | None = None,
    retest_high: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return phase boundaries and per-phase summary rows."""
    boundaries: list[dict[str, Any]] = []

    # Find first edge zone entry and first outside break after anchor
    post = [t for t in trades if t["ts"] >= ANCHOR]
    first_edge = next((t for t in post if t["price"] >= TPO_VAH), None)
    first_outside = next((t for t in post if t["price"] >= VOLUME_VVAH), None)
    reclaim_dt = (
        datetime.fromisoformat(reclaim_cross_ts.replace("Z", "+00:00"))
        if reclaim_cross_ts
        else None
    )

    phase_specs: list[tuple[str, datetime | None, datetime | None]] = [
        ("PRE_ATTACK", ANCHOR, first_edge["ts"] if first_edge else ANCHOR),
        ("EDGE_ZONE_ATTACK", first_edge["ts"] if first_edge else ANCHOR, first_outside["ts"] if first_outside else peak_ts),
        ("OUTSIDE_BREAK", first_outside["ts"] if first_outside else ANCHOR, peak_ts),
        ("PEAK_FORMATION", first_outside["ts"] if first_outside else ANCHOR, peak_ts),
        ("RECLAIM", peak_ts, reclaim_dt or CORE_END),
        ("POST_RECLAIM", reclaim_dt or peak_ts, CORE_END),
    ]

    if retest_ts and retest_ts <= CORE_END:
        phase_specs.append(("RETEST_ATTEMPT", reclaim_dt or peak_ts, retest_ts))
        phase_specs.append(("RETEST_RESOLUTION", retest_ts, CORE_END))
    elif retest_ts:
        phase_specs.append(("LATER_RESOLUTION", CORE_END, retest_ts))

    summaries: list[dict[str, Any]] = []
    for name, start, end in phase_specs:
        if start is None or end is None or start >= end:
            continue
        wf = window_trade_facts(trades, start, end, label=name)
        liq_chunk = []
        for e in liq_events:
            et = datetime.fromisoformat(e["event_time"].replace("Z", "+00:00"))
            if start <= et < end:
                liq_chunk.append(e)
        oi_chunk = [r for r in oi_rows if start <= r["ts"] < end]
        oi_start = oi_chunk[0]["oi"] if oi_chunk else None
        oi_end = oi_chunk[-1]["oi"] if oi_chunk else None
        oi_delta = (oi_end - oi_start) if oi_start is not None and oi_end is not None else None
        boundaries.append({"phase": name, "start": iso_z(start), "end": iso_z(end)})
        summaries.append(
            {
                "phase": name,
                "start_ts": iso_z(start),
                "end_ts": iso_z(end),
                "price_start": wf.get("first_price"),
                "price_end": wf.get("last_price"),
                "price_high": wf.get("high_price"),
                "price_low": wf.get("low_price"),
                "taker_buy_quote": wf.get("buy_notional"),
                "taker_sell_quote": wf.get("sell_notional"),
                "taker_delta": wf.get("delta_notional"),
                "trade_count": wf.get("trade_count"),
                "price_change_bps": wf.get("price_change_bps"),
                "short_liq_count": sum(1 for e in liq_chunk if e["liquidated_side"] == "LIQUIDATED_SHORT"),
                "short_liq_quote": sum(e["quote_notional"] for e in liq_chunk if e["liquidated_side"] == "LIQUIDATED_SHORT"),
                "long_liq_count": sum(1 for e in liq_chunk if e["liquidated_side"] == "LIQUIDATED_LONG"),
                "oi_start": oi_start,
                "oi_end": oi_end,
                "oi_delta": oi_delta,
                "oi_delta_pct": (oi_delta / oi_start * 100.0) if oi_start and oi_delta is not None else None,
                "oi_coverage_samples": len(oi_chunk),
            }
        )
    return boundaries, summaries
