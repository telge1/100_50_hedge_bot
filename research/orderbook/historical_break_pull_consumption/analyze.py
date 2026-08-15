"""Per-event OB+trade pull/consumption analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.orderbook.historical_break_pull_consumption import MARKER_OFFSETS_S
from research.orderbook.historical_break_pull_consumption.classify import classify_mechanism
from research.orderbook.historical_break_pull_consumption.trades import (
    aggressor_side_for_direction,
    day_trade_csv_path,
    load_trades_window,
    ms_to_iso,
    wall_book_side,
)
from research.orderbook.historical_break_pull_consumption.walls import (
    aggressive_flow_in_window,
    build_actions_from_snaps,
    snapshot_wall,
)
from research.orderbook.historical_bybit_replay import (
    DEFAULT_DATA_ROOT as DEFAULT_OB_ROOT,
    HistoricalBybitReplayer,
    ObMessage,
    SequenceStatus,
    day_file_path,
    iter_messages,
)


def ts_to_ms(ts) -> int:
    from datetime import datetime, timezone

    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


DEFAULT_TRADE_ROOT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/data/bybit_historical_trades"
)


def analyze_event(
    event: dict[str, Any],
    *,
    ob_root: Path = DEFAULT_OB_ROOT,
    trade_root: Path = DEFAULT_TRADE_ROOT,
) -> dict[str, Any]:
    symbol = event["symbol"]
    date = event["date"]
    level = float(event["level"])
    direction = event["direction"]
    event_id = event["event_id"]
    book_side = wall_book_side(direction)
    aggressor = aggressor_side_for_direction(direction)

    break_ms = ts_to_ms(event["first_break_ts"] or event["available_at"])
    touch_ms = ts_to_ms(event["first_touch_ts"]) if event.get("first_touch_ts") else break_ms
    win_start = min(break_ms - 300_000, touch_ms - 60_000)
    win_end = break_ms + 300_000
    detail_start = break_ms - 60_000
    detail_end = break_ms + 120_000

    quality: dict[str, Any] = {
        "event_id": event_id,
        "symbol": symbol,
        "date": date,
        "mode": "OB_PLUS_TRADES",
    }

    ob_path = day_file_path(symbol, date, data_root=ob_root)
    trade_path = day_trade_csv_path(trade_root, symbol, date)
    quality["ob_path"] = str(ob_path)
    quality["ob_exists"] = ob_path.exists()
    quality["trade_path"] = str(trade_path) if trade_path else None
    quality["trade_exists"] = bool(trade_path and trade_path.exists())

    if not ob_path.exists() or not trade_path or not trade_path.exists():
        quality["data_quality"] = "DATA_INVALID"
        quality["reason"] = "missing_ob_or_trades"
        return {
            "quality": quality,
            "mechanism": {"mechanism_class": "NO_CLEAR_MECHANISM", "confidence": "LOW"},
            "actions": [],
            "matches": [],
            "lifecycle": [],
            "timeline": [],
            "summary": {"event_id": event_id, "mechanism_class": "NO_CLEAR_MECHANISM"},
        }

    trades = load_trades_window(
        trade_path,
        start_ms=win_start,
        end_ms=win_end,
        expected_symbol=symbol,
    )
    quality["trades_in_window"] = len(trades)

    replayer = HistoricalBybitReplayer()
    wall_snaps: list[Any] = []
    marker_snaps: dict[int, Any] = {}
    marker_targets = {break_ms + s * 1000: s for s in MARKER_OFFSETS_S}
    remaining_markers = sorted(marker_targets)
    prev_detail_ts = None
    SAMPLE_EVERY_MS = 250  # message-dense sampling in detail window

    # Flip / depth trackers post break
    post_flip_rows: list[dict[str, Any]] = []

    for item in iter_messages(ob_path, expected_symbol=symbol, skip_malformed=True):
        if not isinstance(item, ObMessage):
            continue
        msg = item
        if msg.ts_ms > win_end:
            break

        # Capture markers causally before applying? same as prior: apply then capture at ts
        while remaining_markers and remaining_markers[0] < msg.ts_ms:
            t = remaining_markers.pop(0)
            if replayer.book.has_snapshot:
                marker_snaps[t] = snapshot_wall(
                    replayer.book, ts_ms=t, level=level, book_side=book_side
                )

        replayer.apply_message(msg)

        while remaining_markers and remaining_markers[0] == msg.ts_ms:
            t = remaining_markers.pop(0)
            marker_snaps[t] = snapshot_wall(
                replayer.book, ts_ms=t, level=level, book_side=book_side
            )

        if not replayer.book.has_snapshot:
            continue

        if detail_start <= msg.ts_ms <= detail_end:
            if prev_detail_ts is None or msg.ts_ms - prev_detail_ts >= SAMPLE_EVERY_MS:
                wall_snaps.append(
                    snapshot_wall(
                        replayer.book, ts_ms=msg.ts_ms, level=level, book_side=book_side
                    )
                )
                prev_detail_ts = msg.ts_ms

    # flush remaining markers at end state
    while remaining_markers:
        t = remaining_markers.pop(0)
        if t <= win_end and replayer.book.has_snapshot:
            marker_snaps[t] = snapshot_wall(
                replayer.book, ts_ms=t, level=level, book_side=book_side
            )

    seq = replayer.diag.status()
    quality["sequence_status"] = seq.value
    quality["messages_applied"] = replayer._messages_applied
    if seq == SequenceStatus.INVALID:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sequence_invalid"
    elif seq == SequenceStatus.RESET_SEEN:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sequence_reset_seen"
    elif len(wall_snaps) < 10:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sparse_wall_samples"
    else:
        quality["data_quality"] = "DATA_VALID"

    actions = build_actions_from_snaps(
        event_id,
        wall_snaps,
        level=level,
        trades=trades,
        aggressor_side=aggressor,
    )

    matches = []
    for a in actions:
        if a.action not in {"DECREASE", "DELETE"}:
            continue
        matches.append(
            {
                "event_id": event_id,
                "ts": ms_to_iso(a.ts_ms),
                "ts_ms": a.ts_ms,
                "action": a.action,
                "wall_price": a.wall_price,
                "removed_qty": max(0.0, -a.delta_qty),
                "matched_aggressive_qty": a.matched_aggressive_qty,
                "matched_trade_count": a.matched_trade_count,
                "unmatched_removal_qty": a.unmatched_removal_qty,
                "consumption_ratio": a.consumption_ratio,
                "mechanism_hint": a.mechanism_hint,
                "match_time_ms_tol": 750,
                "aggressor_side": aggressor,
            }
        )

    lifecycle = [
        {
            "event_id": event_id,
            "ts": ms_to_iso(a.ts_ms),
            "ts_ms": a.ts_ms,
            "action": a.action,
            "wall_price": a.wall_price,
            "qty_before": a.qty_before,
            "qty_after": a.qty_after,
            "delta_qty": a.delta_qty,
            "matched_aggressive_qty": a.matched_aggressive_qty,
            "refill_proxy": int(a.action in {"INCREASE", "REAPPEAR"}),
            "mechanism_hint": a.mechanism_hint,
            "best_bid": a.best_bid,
            "best_ask": a.best_ask,
            "distance_to_level_bps": a.distance_to_level_bps,
        }
        for a in actions
    ]

    peak_wall = max((s.zone_qty for s in wall_snaps), default=0.0)
    wall_price_ref = next((s.wall_price for s in reversed(wall_snaps) if s.wall_price), level)
    # Pre-break aggressive flow near level (60s)
    agg_pre = aggressive_flow_in_window(
        trades,
        start_ms=break_ms - 60_000,
        end_ms=break_ms,
        aggressor_side=aggressor,
        ref_price=float(wall_price_ref or level),
    )

    def beyond_at(offset_s: int) -> bool | None:
        t = break_ms + offset_s * 1000
        snap = marker_snaps.get(t)
        if snap is None:
            earlier = [k for k in marker_snaps if k <= t]
            snap = marker_snaps[max(earlier)] if earlier else None
        if snap is None:
            return None
        if direction == "bearish":
            return snap.best_bid is not None and snap.best_bid < level
        return snap.best_ask is not None and snap.best_ask > level

    mech = classify_mechanism(
        actions=actions,
        snaps=wall_snaps,
        break_ms=break_ms,
        aggressive_qty_pre_break=agg_pre,
        peak_wall_qty=peak_wall,
        beyond_at_break=bool(beyond_at(0)),
        beyond_at_60s=beyond_at(60),
        prior_ob_class=event.get("ob_classification"),
    )

    # Support/resistance flip snapshot at +30s/+60s
    flip_notes = []
    for off in (30, 60):
        t = break_ms + off * 1000
        earlier = [k for k in marker_snaps if k <= t]
        snap = marker_snaps[max(earlier)] if earlier else None
        if snap is None:
            continue
        # opposite side depth near level via wall_qty on opposite — approximate using mid vs level
        if direction == "bearish":
            # after break: ask building near old support?
            flip_notes.append(
                f"+{off}s mid={snap.mid} zone_bid={snap.zone_qty} beyond={beyond_at(off)}"
            )
        else:
            flip_notes.append(
                f"+{off}s mid={snap.mid} zone_ask={snap.zone_qty} beyond={beyond_at(off)}"
            )

    # Timeline at markers
    timeline = []
    for off in MARKER_OFFSETS_S:
        t = break_ms + off * 1000
        earlier = [k for k in marker_snaps if k <= t]
        snap = marker_snaps[max(earlier)] if earlier else None
        if snap is None:
            continue
        # trades in last 1s ending at marker (causal)
        recent = [tr for tr in trades if t - 1000 < tr.ts_ms <= t]
        buy_q = sum(tr.size for tr in recent if tr.side == "Buy")
        sell_q = sum(tr.size for tr in recent if tr.side == "Sell")
        # nearest action
        near_acts = [a for a in actions if abs(a.ts_ms - t) <= 500]
        wall_delta = near_acts[-1].delta_qty if near_acts else 0.0
        wall_action = near_acts[-1].action if near_acts else ""
        matched = near_acts[-1].matched_aggressive_qty if near_acts else 0.0
        marker_name = "FIRST_BREAK" if off == 0 else (f"PRE_{abs(off)}S" if off < 0 else f"POST_{off}S")
        mech_marker = ""
        if mech.get("pull_start_ts_ms") and abs(t - mech["pull_start_ts_ms"]) <= 500:
            mech_marker = "PULL_START"
        if mech.get("consumption_start_ts_ms") and abs(t - mech["consumption_start_ts_ms"]) <= 500:
            mech_marker = "CONSUMPTION_START"
        if mech.get("refill_start_ts_ms") and abs(t - mech["refill_start_ts_ms"]) <= 500:
            mech_marker = "REFILL_START"
        if event.get("first_touch_ts") and abs(t - touch_ms) <= 500:
            mech_marker = mech_marker or "FIRST_TOUCH"
        timeline.append(
            {
                "event_id": event_id,
                "relative_ts_s": off,
                "absolute_ts": ms_to_iso(t),
                "price": snap.mid,
                "structure_level": level,
                "best_bid": snap.best_bid,
                "best_ask": snap.best_ask,
                "wall_price": snap.wall_price,
                "wall_qty": snap.zone_qty,
                "wall_delta": wall_delta,
                "wall_action": wall_action,
                "aggressive_buy_qty": buy_q,
                "aggressive_sell_qty": sell_q,
                "matched_trade_qty": matched,
                "net_wall_change": wall_delta,
                "refill_qty": max(0.0, wall_delta),
                "mechanism_marker": mech_marker,
                "event_marker": marker_name,
            }
        )

    outcome = "UNKNOWN"
    b60 = beyond_at(60)
    prior = event.get("ob_classification") or ""
    if prior.startswith("BREAK_ACCEPTED") or prior == "WALL_CONSUMED_OR_REMOVED_BREAK":
        outcome = "BREAK_ACCEPTED"
    elif prior in {"REFILL_THEN_RECLAIM", "WALL_HELD_OR_RECLAIM"} or "HELD" in prior:
        outcome = "RECLAIM_OR_HOLD"
    elif b60 is True:
        outcome = "BREAK_ACCEPTED"
    elif b60 is False:
        outcome = "RECLAIM_OR_HOLD"

    summary = {
        "event_id": event_id,
        "symbol": symbol,
        "direction": direction,
        "structure_type": event.get("structure_type"),
        "timeframe": event.get("timeframe"),
        "level": level,
        "first_break_ts": event.get("first_break_ts"),
        "first_touch_ts": event.get("first_touch_ts"),
        "prior_ob_classification": prior,
        "outcome": outcome,
        "important_wall_price": wall_price_ref,
        "book_side": book_side,
        "aggressor_side": aggressor,
        "flip_notes": " | ".join(flip_notes),
        **{k: mech[k] for k in mech if k != "thresholds"},
        "data_quality": quality.get("data_quality"),
    }

    return {
        "quality": quality,
        "mechanism": mech,
        "actions": actions,
        "matches": matches,
        "lifecycle": lifecycle,
        "timeline": timeline,
        "summary": summary,
        "thresholds": mech.get("thresholds"),
    }
