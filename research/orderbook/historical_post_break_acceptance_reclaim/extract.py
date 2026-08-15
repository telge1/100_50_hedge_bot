"""Causal post-break feature extraction (OB + trades)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.orderbook.historical_break_pull_consumption.trades import (
    aggressor_side_for_direction,
    day_trade_csv_path,
    load_trades_window,
    wall_book_side,
)
from research.orderbook.historical_break_pull_consumption.walls import zone_qty
from research.orderbook.historical_bybit_replay import (
    HistoricalBybitReplayer,
    ObMessage,
    SequenceStatus,
    day_file_path,
    iter_messages,
)
from research.orderbook.historical_post_break_acceptance_reclaim import (
    CUTOFFS_S,
    DEFAULT_OB_ROOT,
    DEFAULT_TRADE_ROOT,
    DEPTH_BPS,
    OUTCOME_AMBIGUOUS,
    SAMPLE_EVERY_MS,
    ZONE_BPS,
)
from research.orderbook.historical_post_break_acceptance_reclaim.outcomes import (
    distance_beyond_bps,
    is_beyond,
    label_from_post_path,
    map_event_outcome,
)
from research.orderbook.historical_structure_break_ob_deep_dive.ob_extract import (
    book_snapshot,
    depth_within_bps,
)


def ts_to_ms(ts: str | datetime) -> int:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def assert_causal_ob(samples: list[dict[str, Any]], *, cutoff_ms: int) -> None:
    for s in samples:
        if s["ts_ms"] > cutoff_ms:
            raise AssertionError(f"OB lookahead: {s['ts_ms']} > {cutoff_ms}")


def assert_causal_trades(trades: list[Any], *, cutoff_ms: int) -> None:
    for t in trades:
        if t.ts_ms > cutoff_ms:
            raise AssertionError(f"trade lookahead: {t.ts_ms} > {cutoff_ms}")


def _defensive_side(direction: str) -> str:
    """Old support/resistance side before break."""
    return wall_book_side(direction)  # bearish→bid, bullish→ask


def _blocking_side(direction: str) -> str:
    """New side that should block reclaim after acceptance."""
    return "ask" if direction == "bearish" else "bid"


def _break_aggressor(direction: str) -> str:
    return aggressor_side_for_direction(direction)


def _reclaim_aggressor(direction: str) -> str:
    return "Buy" if direction == "bearish" else "Sell"


def _trade_beyond(trade_price: float, level: float, direction: str) -> bool:
    if direction == "bearish":
        return trade_price < level
    return trade_price > level


def extract_event(
    event: dict[str, Any],
    *,
    ob_root: Path = DEFAULT_OB_ROOT,
    trade_root: Path = DEFAULT_TRADE_ROOT,
    cohort: str = "selected_15",
) -> dict[str, Any]:
    symbol = event["symbol"]
    date = event["date"]
    level = float(event["level"])
    direction = event["direction"]
    event_id = event["event_id"]
    break_ms = ts_to_ms(event["first_break_ts"] or event["available_at"])

    win_start = break_ms - 10_000
    win_end = break_ms + 180_000

    quality: dict[str, Any] = {
        "event_id": event_id,
        "symbol": symbol,
        "date": date,
        "cohort": cohort,
        "ob_path": str(day_file_path(symbol, date, data_root=ob_root)),
    }
    ob_path = day_file_path(symbol, date, data_root=ob_root)
    trade_path = day_trade_csv_path(trade_root, symbol, date)
    quality["ob_exists"] = ob_path.exists()
    quality["trade_path"] = str(trade_path) if trade_path else None
    quality["trade_exists"] = bool(trade_path and trade_path.exists())

    if not ob_path.exists() or not trade_path or not trade_path.exists():
        quality["data_quality"] = "DATA_INVALID"
        quality["reason"] = "missing_ob_or_trades"
        return {
            "quality": quality,
            "inventory": {
                "event_id": event_id,
                "symbol": symbol,
                "date": date,
                "direction": direction,
                "timeframe": event.get("timeframe"),
                "level": level,
                "first_break_ts": ms_to_iso(break_ms),
                "first_break_ts_ms": break_ms,
                "outcome": OUTCOME_AMBIGUOUS,
                "cohort": cohort,
                "data_quality": "DATA_INVALID",
            },
            "timepoints": [],
            "timeline": [],
        }

    trades_all = load_trades_window(
        trade_path, start_ms=win_start, end_ms=win_end, expected_symbol=symbol
    )
    quality["trades_in_window"] = len(trades_all)

    replayer = HistoricalBybitReplayer()
    samples: list[dict[str, Any]] = []
    cutoff_snaps: dict[int, dict[str, Any]] = {}
    remaining_cutoffs = sorted(break_ms + c * 1000 for c in CUTOFFS_S)
    remaining_cutoffs = [break_ms] + remaining_cutoffs  # include t0
    last_sample_ts = None
    snap0: dict[str, Any] | None = None

    for item in iter_messages(ob_path, expected_symbol=symbol, skip_malformed=True):
        if not isinstance(item, ObMessage):
            continue
        msg = item
        if msg.ts_ms > win_end:
            break

        # Flush cutoffs that are now past (use last book state)
        while remaining_cutoffs and remaining_cutoffs[0] < msg.ts_ms:
            t = remaining_cutoffs.pop(0)
            if replayer.book.has_snapshot:
                cutoff_snaps[t] = book_snapshot(
                    replayer.book, level=level, ts_ms=t, direction=direction
                )

        replayer.apply_message(msg)
        if not replayer.book.has_snapshot:
            continue

        while remaining_cutoffs and remaining_cutoffs[0] == msg.ts_ms:
            t = remaining_cutoffs.pop(0)
            cutoff_snaps[t] = book_snapshot(
                replayer.book, level=level, ts_ms=t, direction=direction
            )

        if msg.ts_ms < win_start:
            continue

        if last_sample_ts is None or msg.ts_ms - last_sample_ts >= SAMPLE_EVERY_MS:
            snap = book_snapshot(replayer.book, level=level, ts_ms=msg.ts_ms, direction=direction)
            # add zone depths
            def_side = _defensive_side(direction)
            blk_side = _blocking_side(direction)
            snap["old_level_defensive_qty"] = zone_qty(
                replayer.book, book_side=def_side, level=level, zone_bps=ZONE_BPS
            )
            snap["new_side_blocking_qty"] = zone_qty(
                replayer.book, book_side=blk_side, level=level, zone_bps=ZONE_BPS
            )
            for bps in DEPTH_BPS:
                snap[f"defensive_depth_{bps}bps"] = depth_within_bps(
                    replayer.book, side=def_side, ref=level, bps=float(bps)
                )
                snap[f"blocking_depth_{bps}bps"] = depth_within_bps(
                    replayer.book, side=blk_side, ref=level, bps=float(bps)
                )
            samples.append(snap)
            last_sample_ts = msg.ts_ms
            if snap0 is None and msg.ts_ms >= break_ms:
                snap0 = snap

    while remaining_cutoffs:
        t = remaining_cutoffs.pop(0)
        if replayer.book.has_snapshot:
            cutoff_snaps[t] = book_snapshot(
                replayer.book, level=level, ts_ms=t, direction=direction
            )

    seq = replayer.diag.status()
    quality["sequence_status"] = seq.value
    if seq == SequenceStatus.INVALID:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sequence_invalid"
    elif len(samples) < 20:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sparse_samples"
    else:
        quality["data_quality"] = "DATA_VALID"

    # Enrich cutoff snaps with zone qty via nearest sample
    def nearest_sample(ts: int) -> dict[str, Any] | None:
        cand = [s for s in samples if s["ts_ms"] <= ts]
        return cand[-1] if cand else None

    path_label = label_from_post_path(
        samples, break_ms=break_ms, level=level, direction=direction
    )
    outcome_info = map_event_outcome(
        ob_classification=event.get("ob_classification"),
        path_label=path_label,
    )

    # first retest: return toward level while still beyond / touch level from beyond
    first_retest_ms = None
    for s in samples:
        if s["ts_ms"] <= break_ms + 500:
            continue
        d = distance_beyond_bps(mid=s.get("mid"), level=level, direction=direction)
        if d is not None and d <= 2.0:
            first_retest_ms = s["ts_ms"]
            break

    inventory = {
        "event_id": event_id,
        "symbol": symbol,
        "date": date,
        "direction": direction,
        "timeframe": event.get("timeframe"),
        "level": level,
        "first_break_ts": ms_to_iso(break_ms),
        "first_break_ts_ms": break_ms,
        "outcome": outcome_info["outcome"],
        "outcome_source": outcome_info["outcome_source"],
        "path_outcome": outcome_info["path_outcome"],
        "path_reason": outcome_info["path_reason"],
        "legacy_ob_classification": outcome_info.get("legacy_ob_classification"),
        "first_reclaim_ts": (
            ms_to_iso(outcome_info["first_reclaim_ts_ms"])
            if outcome_info.get("first_reclaim_ts_ms")
            else None
        ),
        "seconds_to_reclaim": outcome_info.get("seconds_to_reclaim"),
        "first_retest_ts": ms_to_iso(first_retest_ms) if first_retest_ms else None,
        "seconds_to_retest": (
            (first_retest_ms - break_ms) / 1000.0 if first_retest_ms else None
        ),
        "cohort": cohort,
        "data_quality": quality["data_quality"],
    }

    # Baseline defensive/blocking at break
    s_break = nearest_sample(break_ms) or snap0
    def0 = float(s_break.get("old_level_defensive_qty") or 0) if s_break else 0.0
    blk0 = float(s_break.get("new_side_blocking_qty") or 0) if s_break else 0.0

    timepoints: list[dict[str, Any]] = []
    timeline: list[dict[str, Any]] = []

    break_aggr = _break_aggressor(direction)
    reclaim_aggr = _reclaim_aggressor(direction)

    for cutoff_s in CUTOFFS_S:
        cutoff_ms = break_ms + cutoff_s * 1000
        path = [s for s in samples if break_ms <= s["ts_ms"] <= cutoff_ms]
        assert_causal_ob(path, cutoff_ms=cutoff_ms)
        trades = [t for t in trades_all if break_ms <= t.ts_ms <= cutoff_ms]
        assert_causal_trades(trades, cutoff_ms=cutoff_ms)

        # Ensure no future reclaim leakage into features
        if outcome_info.get("first_reclaim_ts_ms") and outcome_info["first_reclaim_ts_ms"] > cutoff_ms:
            # reclaim time is label-only; features must not include it
            pass

        cur = nearest_sample(cutoff_ms) or (path[-1] if path else None)
        row: dict[str, Any] = {
            "event_id": event_id,
            "cohort": cohort,
            "outcome": outcome_info["outcome"],
            "symbol": symbol,
            "direction": direction,
            "timeframe": event.get("timeframe"),
            "cutoff": cutoff_s,
            "seconds_after_break": cutoff_s,
            "cutoff_ts": ms_to_iso(cutoff_ms),
        }

        # --- PRICE ---
        beyond_series = []
        dist_series = []
        for s in path:
            bey = is_beyond(
                best_bid=s.get("best_bid"),
                best_ask=s.get("best_ask"),
                level=level,
                direction=direction,
            )
            beyond_series.append(bey)
            d = distance_beyond_bps(mid=s.get("mid"), level=level, direction=direction)
            if d is not None:
                dist_series.append(d)

        cur_dist = (
            distance_beyond_bps(mid=cur.get("mid"), level=level, direction=direction)
            if cur
            else None
        )
        row["distance_beyond_level_bps"] = cur_dist
        row["max_distance_beyond_so_far"] = max(dist_series) if dist_series else None
        row["min_distance_back_toward_level"] = min(dist_series) if dist_series else None
        row["fraction_of_time_beyond_level"] = (
            sum(1 for b in beyond_series if b) / len(beyond_series) if beyond_series else None
        )
        # recrosses: beyond→not or not→beyond
        recross = 0
        for i in range(1, len(beyond_series)):
            if beyond_series[i] != beyond_series[i - 1]:
                recross += 1
        row["n_recrosses"] = recross
        row["mid_beyond_level"] = int(cur_dist is not None and cur_dist > 0) if cur else None
        row["bbo_beyond_level"] = (
            int(
                is_beyond(
                    best_bid=cur.get("best_bid"),
                    best_ask=cur.get("best_ask"),
                    level=level,
                    direction=direction,
                )
            )
            if cur
            else None
        )
        # velocity: change in beyond-distance over last min(cutoff, 5s)
        vel = None
        if dist_series and len(path) >= 2:
            look_ms = min(cutoff_s, 5) * 1000
            early = [s for s in path if s["ts_ms"] >= cutoff_ms - look_ms]
            if len(early) >= 2:
                d0 = distance_beyond_bps(mid=early[0].get("mid"), level=level, direction=direction)
                d1 = distance_beyond_bps(mid=early[-1].get("mid"), level=level, direction=direction)
                dt = max(1e-3, (early[-1]["ts_ms"] - early[0]["ts_ms"]) / 1000.0)
                if d0 is not None and d1 is not None:
                    vel = (d1 - d0) / dt
        row["velocity_away_bps_per_s"] = vel
        row["best_bid"] = cur.get("best_bid") if cur else None
        row["best_ask"] = cur.get("best_ask") if cur else None
        row["mid"] = cur.get("mid") if cur else None
        row["spread_bps"] = cur.get("spread_bps") if cur else None

        # --- TRADES / FLOW ---
        break_notional = 0.0
        reclaim_notional = 0.0
        beyond_notional = 0.0
        beyond_break_notional = 0.0
        beyond_reclaim_notional = 0.0
        total_notional = 0.0
        largest = 0.0
        for t in trades:
            n = t.price * t.size
            total_notional += n
            largest = max(largest, n)
            if t.side == break_aggr:
                break_notional += n
            elif t.side == reclaim_aggr:
                reclaim_notional += n
            if _trade_beyond(t.price, level, direction):
                beyond_notional += n
                if t.side == break_aggr:
                    beyond_break_notional += n
                elif t.side == reclaim_aggr:
                    beyond_reclaim_notional += n

        row["total_trade_notional"] = total_notional
        row["trade_count"] = len(trades)
        row["largest_trade_notional"] = largest
        row["break_flow"] = break_notional
        row["reclaim_flow"] = reclaim_notional
        row["signed_aggressive_flow"] = break_notional - reclaim_notional
        row["flow_imbalance"] = (
            (break_notional - reclaim_notional) / total_notional if total_notional > 0 else None
        )
        row["flow_reversal_ratio"] = (
            reclaim_notional / break_notional if break_notional > 1e-12 else None
        )
        row["burst_intensity"] = total_notional / max(cutoff_s, 1)
        row["volume_beyond_level"] = beyond_notional
        row["directional_volume_beyond_level"] = beyond_break_notional
        row["opposite_volume_beyond_level"] = beyond_reclaim_notional
        row["fraction_volume_beyond_level"] = (
            beyond_notional / total_notional if total_notional > 0 else None
        )

        # --- OB / FLIP / REFILL ---
        def_qty = float(cur.get("old_level_defensive_qty") or 0) if cur else 0.0
        blk_qty = float(cur.get("new_side_blocking_qty") or 0) if cur else 0.0
        row["old_level_defensive_depth"] = def_qty
        row["new_side_blocking_depth"] = blk_qty
        row["flip_depth_ratio"] = blk_qty / def_qty if def_qty > 1e-12 else (None if blk_qty <= 0 else 99.0)
        row["break_side_depth_change"] = blk_qty - blk0
        row["defensive_depth_change"] = def_qty - def0
        # refill on old defensive side
        refill = max(0.0, def_qty - def0)
        row["gross_refill"] = refill
        row["net_refill"] = def_qty - def0
        row["refill_ratio"] = refill / def0 if def0 > 1e-12 else None
        row[f"refill_{cutoff_s}s"] = refill
        for bps in DEPTH_BPS:
            row[f"defensive_depth_{bps}bps"] = cur.get(f"defensive_depth_{bps}bps") if cur else None
            row[f"blocking_depth_{bps}bps"] = cur.get(f"blocking_depth_{bps}bps") if cur else None
        # imbalance near level
        bid10 = cur.get("bid_depth_10bps") if cur else None
        ask10 = cur.get("ask_depth_10bps") if cur else None
        if bid10 is not None and ask10 is not None and (bid10 + ask10) > 0:
            # direction-normalized: positive = more blocking (new side) than defensive
            if direction == "bearish":
                row["near_depth_imbalance"] = (ask10 - bid10) / (ask10 + bid10)
            else:
                row["near_depth_imbalance"] = (bid10 - ask10) / (ask10 + bid10)
        else:
            row["near_depth_imbalance"] = None

        # retest info only if retest already happened by cutoff
        if first_retest_ms is not None and first_retest_ms <= cutoff_ms:
            row["retest_occurred"] = 1
            row["seconds_to_retest"] = (first_retest_ms - break_ms) / 1000.0
            # flow near retest within ±2s up to cutoff
            rt_lo = first_retest_ms - 2000
            rt_hi = min(cutoff_ms, first_retest_ms + 2000)
            rt_trades = [t for t in trades if rt_lo <= t.ts_ms <= rt_hi]
            row["retest_reclaim_flow"] = sum(
                t.price * t.size for t in rt_trades if t.side == reclaim_aggr
            )
            row["retest_break_flow"] = sum(
                t.price * t.size for t in rt_trades if t.side == break_aggr
            )
        else:
            row["retest_occurred"] = 0
            row["seconds_to_retest"] = None
            row["retest_reclaim_flow"] = None
            row["retest_break_flow"] = None

        # Explicit: do not store future reclaim as feature
        row["label_only_outcome"] = outcome_info["outcome"]

        timepoints.append(row)

    # Deep-dive timeline (1s steps up to 60s + markers)
    for off in list(range(0, 61, 1)) + [120]:
        t = break_ms + off * 1000
        s = nearest_sample(t)
        if s is None:
            continue
        trades_to_t = [tr for tr in trades_all if break_ms <= tr.ts_ms <= t]
        bf = sum(tr.price * tr.size for tr in trades_to_t if tr.side == break_aggr)
        rf = sum(tr.price * tr.size for tr in trades_to_t if tr.side == reclaim_aggr)
        timeline.append(
            {
                "event_id": event_id,
                "cohort": cohort,
                "outcome": outcome_info["outcome"],
                "relative_s": off,
                "ts": ms_to_iso(t),
                "mid": s.get("mid"),
                "distance_beyond_bps": distance_beyond_bps(
                    mid=s.get("mid"), level=level, direction=direction
                ),
                "best_bid": s.get("best_bid"),
                "best_ask": s.get("best_ask"),
                "bbo_beyond": int(
                    is_beyond(
                        best_bid=s.get("best_bid"),
                        best_ask=s.get("best_ask"),
                        level=level,
                        direction=direction,
                    )
                ),
                "old_defensive_qty": s.get("old_level_defensive_qty"),
                "new_blocking_qty": s.get("new_side_blocking_qty"),
                "break_flow_cum": bf,
                "reclaim_flow_cum": rf,
                "outcome_marker": (
                    "RECLAIM"
                    if outcome_info.get("first_reclaim_ts_ms")
                    and abs(t - outcome_info["first_reclaim_ts_ms"]) <= 500
                    else ("BREAK" if off == 0 else "")
                ),
            }
        )

    return {
        "quality": quality,
        "inventory": inventory,
        "timepoints": timepoints,
        "timeline": timeline,
    }
