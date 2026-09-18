"""Public-trade attribution funnel audit (reuses attribute_intervals; no new match engine)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from obfull_research_engine.drilldown.aggregation_100ms import _as_dt
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (
    CanonicalTrade,
    build_canonical_trades,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.mass_balance import (
    decompose_mass_balance,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (
    attack_aggressor_side,
    attribute_intervals,
    band_bounds,
    is_wall_attack_trade,
    wall_flow_event_to_row,
)

from . import SYMBOL


def _parse(ts: Any) -> datetime:
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc) if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return _as_dt(ts)


def build_trade_funnel(
    *,
    raw_trade_rows: list[dict[str, Any]],
    nodes: list[Any],
    wall_side: str,
    wall_price: float,
    band_ticks: int,
    tick_size: float,
    wall_visible_at: datetime,
    analysis_start: datetime,
    analysis_end_exclusive: datetime,
    wall_id: str,
    zone_id: str,
    episode_id: str,
    event_id: str,
) -> dict[str, Any]:
    """Classify every trade and run canonical attribute_intervals for attributed fills."""
    band_low, band_high = band_bounds(wall_price, tick_size=tick_size, band_ticks=band_ticks)
    aggressor = attack_aggressor_side(wall_side)

    kept, dedup, dropped = build_canonical_trades(
        raw_trade_rows, symbol=SYMBOL, source_file="orderbook_analysis.public_trades_canonical"
    )

    rejections: list[dict[str, Any]] = []
    # Missing IDs already counted in dedup
    for _ in range(int(dedup.rejected_missing_trade_id)):
        rejections.append(
            {
                "event_id": event_id,
                "trade_id": None,
                "reason": "MISSING_OR_INVALID_TRADE_ID",
                "qty": None,
            }
        )
    for d in dropped:
        rejections.append(
            {
                "event_id": event_id,
                "trade_id": d.trade_id,
                "reason": "DUPLICATE_TRADE_ID",
                "qty": d.size_base,
                "exchange_event_time": d.exchange_event_time,
                "event_available_at": d.event_available_at,
                "collector_received_at": d.collector_received_at,
                "price": d.price,
                "taker_side": d.taker_side,
            }
        )

    in_window: list[CanonicalTrade] = []
    in_band: list[CanonicalTrade] = []
    correct_side: list[CanonicalTrade] = []

    for tr in kept:
        ts = tr.exchange_dt()
        if ts < analysis_start or ts >= analysis_end_exclusive:
            rejections.append(
                {
                    "event_id": event_id,
                    "trade_id": tr.trade_id,
                    "reason": "OUTSIDE_EVENT_WINDOW",
                    "qty": tr.size_base,
                    "exchange_event_time": tr.exchange_event_time,
                    "event_available_at": tr.event_available_at,
                    "collector_received_at": tr.collector_received_at,
                    "price": tr.price,
                    "taker_side": tr.taker_side,
                }
            )
            continue
        in_window.append(tr)
        # band check (exact price view uses wall; funnel uses defended_band)
        if not (band_low - 1e-12 <= float(tr.price) <= band_high + 1e-12):
            rejections.append(
                {
                    "event_id": event_id,
                    "trade_id": tr.trade_id,
                    "reason": "OUTSIDE_DEFENDED_BAND",
                    "qty": tr.size_base,
                    "exchange_event_time": tr.exchange_event_time,
                    "event_available_at": tr.event_available_at,
                    "collector_received_at": tr.collector_received_at,
                    "price": tr.price,
                    "taker_side": tr.taker_side,
                    "band_low": band_low,
                    "band_high": band_high,
                }
            )
            continue
        in_band.append(tr)
        if tr.taker_side != aggressor:
            rejections.append(
                {
                    "event_id": event_id,
                    "trade_id": tr.trade_id,
                    "reason": "WRONG_AGGRESSOR_SIDE",
                    "qty": tr.size_base,
                    "exchange_event_time": tr.exchange_event_time,
                    "event_available_at": tr.event_available_at,
                    "collector_received_at": tr.collector_received_at,
                    "price": tr.price,
                    "taker_side": tr.taker_side,
                    "required_aggressor": aggressor,
                }
            )
            continue
        correct_side.append(tr)

    # Canonical attribution (engine contract)
    events, stats = attribute_intervals(
        nodes=nodes,
        trades=kept,
        view="defended_band",
        wall_price=wall_price,
        band_low=band_low,
        band_high=band_high,
        wall_visible_at=wall_visible_at,
        analysis_end_exclusive=analysis_end_exclusive,
        episode_id=episode_id,
        zone_id=zone_id,
        wall_id=wall_id,
        wall_side=wall_side,
    )

    attributed_ids: set[str] = set()
    attributed_qty = 0.0
    attributed_count = 0
    fill_exceeds = 0
    for ev in events:
        attributed_ids.update(ev.attributed_trade_ids)
        attributed_qty += float(ev.attributed_hit_qty)
        attributed_count += int(ev.attributed_trade_count)
        book_dec = max(float(ev.queue_before) - float(ev.queue_after), 0.0)
        if float(ev.attributed_hit_qty) > book_dec + 1e-9 and book_dec >= 0:
            fill_exceeds += 1  # diagnostic: engine implies refill

    # Correct-side trades not attributed → NO_MATCHING_BOOK_DECREASE or other
    # (engine attributes any attack trade in an interval between book nodes;
    # if no interval covers the trade exchange time with a following book node, skipped)
    for tr in correct_side:
        if tr.trade_id in attributed_ids:
            continue
        # Before wall visible
        if tr.exchange_dt() < wall_visible_at:
            reason = "NO_MATCHING_BOOK_DECREASE"  # not yet wall-visible for attribution
        else:
            reason = "NO_MATCHING_BOOK_DECREASE"
        rejections.append(
            {
                "event_id": event_id,
                "trade_id": tr.trade_id,
                "reason": reason,
                "qty": tr.size_base,
                "exchange_event_time": tr.exchange_event_time,
                "event_available_at": tr.event_available_at,
                "collector_received_at": tr.collector_received_at,
                "price": tr.price,
                "taker_side": tr.taker_side,
            }
        )

    # EXCEEDS_BOOK_DECREASE_CAP: not enforced by engine — count diagnostic only
    exceeds_cap_count = 0  # always 0 under current contract
    if fill_exceeds:
        # note in funnel, not as rejection of trades
        pass

    total_qty = sum(t.size_base for t in kept)
    in_band_qty = sum(t.size_base for t in in_band)
    correct_qty = sum(t.size_base for t in correct_side)
    unmatched = [t for t in correct_side if t.trade_id not in attributed_ids]
    unmatched_qty = sum(t.size_base for t in unmatched)

    def _pct(num: float, den: float) -> float | None:
        if den <= 0:
            return None
        return 100.0 * float(num) / float(den)

    funnel = {
        "event_id": event_id,
        "wall_id": wall_id,
        "wall_side": wall_side,
        "wall_price": wall_price,
        "band_low": band_low,
        "band_high": band_high,
        "required_aggressor": aggressor,
        "raw_trade_rows": len(raw_trade_rows),
        "total_unique_trade_count": dedup.unique_count,
        "duplicate_count": dedup.duplicate_count,
        "rejected_missing_trade_id": dedup.rejected_missing_trade_id,
        "total_trade_qty": total_qty,
        "in_window_trade_count": len(in_window),
        "in_band_trade_count": len(in_band),
        "in_band_trade_qty": in_band_qty,
        "correct_aggressor_trade_count": len(correct_side),
        "correct_aggressor_trade_qty": correct_qty,
        "attributed_trade_count": attributed_count,
        "attributed_fill_qty": attributed_qty,
        "unmatched_count": len(unmatched),
        "unmatched_qty": unmatched_qty,
        "attribution_rate_vs_total_pct": _pct(attributed_qty, total_qty),
        "attribution_rate_vs_in_band_pct": _pct(attributed_qty, in_band_qty),
        "attribution_rate_vs_correct_aggressor_pct": _pct(attributed_qty, correct_qty),
        "engine_intervals": stats.get("intervals"),
        "engine_trades_attributed": stats.get("trades_attributed"),
        "engine_mass_balance_violations": stats.get("mass_balance_violations"),
        "intervals_fill_exceeds_decrease_diagnostic": fill_exceeds,
        "exceeds_book_decrease_cap_rejections": exceeds_cap_count,
        "note_fill_cap": "ENGINE_DOES_NOT_HARD_CAP_FILL_TO_DECREASE",
        "receive_time_present_count": dedup.receive_time_present_count,
        "receive_time_missing_count": dedup.receive_time_missing_count,
    }

    # Rejection reason counts
    reason_counts: dict[str, int] = {}
    reason_qty: dict[str, float] = {}
    for r in rejections:
        reason = str(r.get("reason") or "OTHER")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        try:
            reason_qty[reason] = reason_qty.get(reason, 0.0) + float(r.get("qty") or 0)
        except (TypeError, ValueError):
            pass
    funnel["rejection_counts"] = reason_counts
    funnel["rejection_qty"] = reason_qty

    return {
        "funnel": funnel,
        "rejections": rejections,
        "wall_flow_events": [wall_flow_event_to_row(e) for e in events],
        "band_events_raw": events,
        "stats": stats,
        "band_low": band_low,
        "band_high": band_high,
        "dedup": {
            "raw_count": dedup.raw_count,
            "unique_count": dedup.unique_count,
            "duplicate_count": dedup.duplicate_count,
            "rejected_missing_trade_id": dedup.rejected_missing_trade_id,
            "identity_rule": dedup.identity_rule,
        },
    }
