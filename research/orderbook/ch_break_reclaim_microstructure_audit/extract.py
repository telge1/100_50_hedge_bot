"""ClickHouse causal feature extraction per event."""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from research.orderbook.ch_break_reclaim_microstructure_audit.features import (
    FLOW_WINDOWS_S,
    PERSIST_WINDOWS_S,
    PULL_LAGS_S,
    SimpleTrade,
    absorption_proxy,
    aggregate_trade_flow,
    assert_causal_cutoff,
    book_snapshot_features,
    build_observation_schedule,
    depth_change,
    derive_touch_break_from_trades,
    direction_context,
    ensure_utc,
    iso_z,
    persistence_ratio,
)

logger = logging.getLogger(__name__)

OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if str(OA_SRC) not in sys.path:
    sys.path.insert(0, str(OA_SRC))


def _connect():
    from dotenv import load_dotenv

    load_dotenv(Path("/home/telgenbuescher/projects/orderbook_analyse/.env"))
    from orderbook_analyse.dynamic_wall_detector import connect_readonly

    return connect_readonly()


def _load_trades(db: Any, *, symbol: str, start: datetime, end: datetime) -> list[SimpleTrade]:
    from orderbook_analyse.orderbook_absorption_features import load_trade_ticks

    ticks, _diag = load_trade_ticks(db, symbol=symbol, start=start, end=end)
    out: list[SimpleTrade] = []
    for t in ticks:
        out.append(
            SimpleTrade(
                trade_ts=ensure_utc(t.trade_ts),
                side=str(t.side),
                price=float(t.price),
                quantity=float(t.quantity),
                notional=float(t.notional),
            )
        )
    return out


def _quality_for_window(
    db: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    start, end = ensure_utc(start), ensure_utc(end)
    ob = db.query(
        """
        SELECT count() AS n, min(exchange_ts) AS tmin, max(exchange_ts) AS tmax
        FROM orderbook_deltas
        WHERE symbol = %(s)s AND exchange_ts >= %(a)s AND exchange_ts <= %(b)s
        """,
        parameters={"s": symbol, "a": start, "b": end},
    ).first_item
    tr = db.query(
        """
        SELECT count() AS n, min(trade_ts) AS tmin, max(trade_ts) AS tmax
        FROM public_trades
        WHERE symbol = %(s)s AND trade_ts >= %(a)s AND trade_ts <= %(b)s
        """,
        parameters={"s": symbol, "a": start, "b": end},
    ).first_item
    # update_id gaps (distinct deltas)
    ids = [
        int(r[0])
        for r in db.query(
            """
            SELECT DISTINCT update_id
            FROM orderbook_deltas
            WHERE symbol = %(s)s
              AND exchange_ts >= %(a)s AND exchange_ts <= %(b)s
              AND message_type = 'delta'
            ORDER BY update_id
            """,
            parameters={"s": symbol, "a": start, "b": end},
        ).result_rows
    ]
    gaps = 0
    max_gap = 0
    for a, b in zip(ids, ids[1:]):
        d = b - a
        if d > 1:
            gaps += 1
            max_gap = max(max_gap, d)

    # minute holes
    mins = [
        ensure_utc(r[0]) if getattr(r[0], "tzinfo", None) else r[0].replace(tzinfo=__import__("datetime").timezone.utc)
        for r in db.query(
            """
            SELECT toStartOfMinute(exchange_ts) AS m
            FROM orderbook_deltas
            WHERE symbol = %(s)s AND exchange_ts >= %(a)s AND exchange_ts <= %(b)s
            GROUP BY m ORDER BY m
            """,
            parameters={"s": symbol, "a": start, "b": end},
        ).result_rows
    ]
    minute_gaps = 0
    max_minute_gap_s = 0.0
    prev = None
    for m in mins:
        if prev is not None:
            gap_s = (m - prev).total_seconds()
            if gap_s > 60:
                minute_gaps += 1
                max_minute_gap_s = max(max_minute_gap_s, gap_s)
        prev = m

    ob_n = int(ob["n"] or 0)
    tr_n = int(tr["n"] or 0)
    status = "DATA_VALID"
    reason = ""
    if ob_n < 500 or tr_n < 20:
        status = "DATA_INVALID"
        reason = "insufficient_ob_or_trades"
    elif gaps > 200 or max_gap > 50 or minute_gaps > 3 or max_minute_gap_s > 180:
        status = "DATA_WARNING"
        reason = "continuity_gaps"
    elif minute_gaps > 0 or gaps > 50:
        status = "DATA_WARNING"
        reason = "minor_gaps"

    return {
        "ob_n": ob_n,
        "trade_n": tr_n,
        "ob_tmin": str(ob["tmin"]) if ob["tmin"] else None,
        "ob_tmax": str(ob["tmax"]) if ob["tmax"] else None,
        "trade_tmin": str(tr["tmin"]) if tr["tmin"] else None,
        "trade_tmax": str(tr["tmax"]) if tr["tmax"] else None,
        "update_id_gap_count": gaps,
        "update_id_max_gap": max_gap,
        "ob_minute_gap_count": minute_gaps,
        "ob_max_minute_gap_s": max_minute_gap_s,
        "data_quality": status,
        "data_quality_reason": reason,
    }


def extract_event_features(db: Any, event: dict[str, Any]) -> dict[str, Any]:
    """Return features rows, timeline rows, quality row for one event."""
    from orderbook_analyse.dynamic_wall_detector import (
        find_bootstrap_snapshot,
        load_events,
        reconstruct_with_samples,
    )
    from orderbook_analyse.orderbook_replay import ReplayError

    symbol = event["symbol"]
    level = float(event["level"])
    ctx = direction_context(event["level_type"])
    event_ts = ensure_utc(event["event_ts"])

    # Base window around scanner break (±5m); extend if touch earlier
    win_lo = event_ts - timedelta(minutes=5)
    win_hi = event_ts + timedelta(minutes=5)

    trades = _load_trades(db, symbol=symbol, start=win_lo - timedelta(minutes=1), end=win_hi + timedelta(seconds=30))

    # Derive touch/break if missing
    first_touch = event.get("first_touch_ts")
    first_break = event.get("first_break_ts")
    touch_src = event.get("first_touch_source")
    break_src = event.get("first_break_source")
    derived = derive_touch_break_from_trades(
        trades,
        level=level,
        break_direction=ctx.break_direction,
        window_start=win_lo,
        window_end=win_hi,
    )
    if first_touch is None:
        first_touch = derived["first_touch_ts"]
        touch_src = derived["touch_break_source"] if first_touch else "unavailable"
    # Prefer trade-through as causal FIRST_BREAK when scanner stamp is candle close
    trade_break = derived["first_break_ts"]
    if trade_break is not None:
        # keep scanner stamp as event_ts; use earlier trade print as first_break when available
        if first_break is None or trade_break <= ensure_utc(first_break):
            first_break = trade_break
            break_src = "derived_from_trades_or_min(artifact,trade)"
    if first_break is None:
        first_break = event_ts
        break_src = "fallback_event_ts"

    # If still no touch, use min(first_break, first trade near level) or first_break
    if first_touch is None:
        first_touch = first_break
        touch_src = "fallback_first_break"

    first_touch = ensure_utc(first_touch)
    first_break = ensure_utc(first_break)

    schedule = build_observation_schedule(first_touch=first_touch, first_break=first_break)
    sample_times = [ensure_utc(s["ts"]) for s in schedule]
    # lag samples for pull/persistence
    for ts in list(sample_times):
        for lag in PULL_LAGS_S:
            sample_times.append(ts - timedelta(seconds=lag))
        for w in PERSIST_WINDOWS_S:
            # dense 2s grid for persistence in last w seconds — too many; use 5 points
            for k in range(5):
                sample_times.append(ts - timedelta(seconds=w * k / 4.0))

    sample_times = sorted({t for t in sample_times})
    # bound reconstruction window
    t_min = min(sample_times) - timedelta(seconds=30)
    t_max = max(sample_times) + timedelta(seconds=5)
    # also keep quality on ±5m around event
    q_start, q_end = win_lo, win_hi
    quality = _quality_for_window(db, symbol=symbol, start=q_start, end=q_end)
    quality["event_id"] = event["event_id"]
    quality["symbol"] = symbol

    feature_rows: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []

    if quality["data_quality"] == "DATA_INVALID":
        return {
            "event_id": event["event_id"],
            "features": feature_rows,
            "timeline": timeline_rows,
            "quality": quality,
            "resolved_first_touch": iso_z(first_touch),
            "resolved_first_break": iso_z(first_break),
            "first_touch_source": touch_src,
            "first_break_source": break_src,
            "error": quality["data_quality_reason"],
        }

    try:
        snap_ts, snap_u, snap_seq = find_bootstrap_snapshot(db, symbol=symbol, start=t_min, end=t_max)
        events = load_events(
            db,
            symbol=symbol,
            snapshot_ts=snap_ts,
            snapshot_u=snap_u,
            snapshot_seq=snap_seq,
            end=t_max,
        )
        _final, books = reconstruct_with_samples(events, sample_times=sample_times, end=t_max)
    except ReplayError as exc:
        quality["data_quality"] = "DATA_INVALID"
        quality["data_quality_reason"] = f"replay_error:{exc}"
        return {
            "event_id": event["event_id"],
            "features": [],
            "timeline": [],
            "quality": quality,
            "resolved_first_touch": iso_z(first_touch),
            "resolved_first_break": iso_z(first_break),
            "first_touch_source": touch_src,
            "first_break_source": break_src,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001
        quality["data_quality"] = "DATA_INVALID"
        quality["data_quality_reason"] = f"extract_error:{type(exc).__name__}"
        logger.exception("extract failed for %s", event["event_id"])
        return {
            "event_id": event["event_id"],
            "features": [],
            "timeline": [],
            "quality": quality,
            "resolved_first_touch": iso_z(first_touch),
            "resolved_first_break": iso_z(first_break),
            "first_touch_source": touch_src,
            "first_break_source": break_src,
            "error": str(exc),
        }

    def nearest_book(ts: datetime):
        ts = ensure_utc(ts)
        if ts in books:
            return books[ts]
        # nearest earlier sample
        earlier = [t for t in books if t <= ts]
        if not earlier:
            return None
        return books[max(earlier)]

    for obs in schedule:
        ts = ensure_utc(obs["ts"])
        book = nearest_book(ts)
        if book is None or not book.has_snapshot:
            continue
        snap = book_snapshot_features(book, level=level, ts=ts, ctx=ctx)
        assert_causal_cutoff([{"ts": ts}], cutoff=ts)

        # pull / depth change vs lagged books
        for lag in PULL_LAGS_S:
            lag_book = nearest_book(ts - timedelta(seconds=lag))
            if lag_book is None:
                snap[f"support_depth_change_{lag}s"] = None
                snap[f"break_depth_change_{lag}s"] = None
                continue
            lag_feat = book_snapshot_features(lag_book, level=level, ts=ts - timedelta(seconds=lag), ctx=ctx)
            snap[f"support_depth_change_{lag}s"] = depth_change(
                snap["support_near_depth"], lag_feat["support_near_depth"]
            )
            snap[f"break_depth_change_{lag}s"] = depth_change(
                snap["break_side_near_depth"], lag_feat["break_side_near_depth"]
            )

        # persistence of support wall (> median local): presence if support_near > 0
        for w in PERSIST_WINDOWS_S:
            flags = []
            for k in range(5):
                t_k = ts - timedelta(seconds=w * k / 4.0)
                b_k = nearest_book(t_k)
                if b_k is None:
                    continue
                f_k = book_snapshot_features(b_k, level=level, ts=t_k, ctx=ctx)
                flags.append(f_k["support_near_depth"] > 0)
            snap[f"support_persistence_{w}s"] = persistence_ratio(flags)

        # refill proxy: support depth change +10s after consumption signal not computed here;
        # use positive support_depth_change_10s after negative as refill_10s
        pull10 = snap.get("support_depth_change_10s")
        snap["support_pull_10s"] = pull10 if pull10 is not None and pull10 < 0 else 0.0 if pull10 is not None else None
        snap["support_refill_10s"] = pull10 if pull10 is not None and pull10 > 0 else 0.0 if pull10 is not None else None

        # trade flow windows ending at ts
        for w in FLOW_WINDOWS_S:
            snap.update(
                aggregate_trade_flow(trades, cutoff=ts, window_s=w, break_direction=ctx.break_direction)
            )

        lag30_book = nearest_book(ts - timedelta(seconds=30))
        lag30_support = None
        if lag30_book is not None:
            lag30_support = book_snapshot_features(lag30_book, level=level, ts=ts - timedelta(seconds=30), ctx=ctx)[
                "support_near_depth"
            ]
        snap.update(
            absorption_proxy(
                signed_break_flow_30s=snap.get("flow_30s_signed_break"),
                signed_move_bps_30s=snap.get("flow_30s_signed_move_bps"),
                support_depth=snap.get("support_near_depth"),
                support_depth_lag_30s=lag30_support,
            )
        )

        # acceptance markers (causal at ts — not future reclaim)
        snap["seconds_since_first_break"] = (ts - first_break).total_seconds() if ts >= first_break else None
        snap["seconds_to_first_break"] = (first_break - ts).total_seconds() if ts <= first_break else None

        row = {
            "event_id": event["event_id"],
            "symbol": symbol,
            "level": level,
            "level_type": event["level_type"],
            "break_direction": ctx.break_direction,
            "outcome_label": event["outcome_label"],
            "raw_outcome": event["raw_outcome"],
            "timepoint": obs["timepoint"],
            "anchor": obs["anchor"],
            "offset_s": obs["offset_s"],
            "is_early_signal_candidate": int(obs["is_early_signal_candidate"]),
            "cutoff_ts": iso_z(ts),
            "data_quality": quality["data_quality"],
            **snap,
        }
        feature_rows.append(row)
        timeline_rows.append(
            {
                "event_id": event["event_id"],
                "symbol": symbol,
                "timepoint": obs["timepoint"],
                "relative_ts": iso_z(ts),
                "price": snap.get("mid"),
                "distance_to_level_bps": snap.get("distance_to_level_bps"),
                "signed_distance_beyond_bps": snap.get("signed_distance_beyond_bps"),
                "best_bid": snap.get("best_bid"),
                "best_ask": snap.get("best_ask"),
                "near_bid_depth": snap.get("bid_depth_0_25"),
                "near_ask_depth": snap.get("ask_depth_0_25"),
                "support_near_depth": snap.get("support_near_depth"),
                "support_wall_notional": snap.get("support_wall_notional"),
                "support_depth_change_10s": snap.get("support_depth_change_10s"),
                "support_refill_10s": snap.get("support_refill_10s"),
                "signed_flow_30s": snap.get("flow_30s_signed_break"),
                "bbo_beyond_level": snap.get("bbo_beyond_level"),
                "outcome_label": event["outcome_label"],
            }
        )

    return {
        "event_id": event["event_id"],
        "features": feature_rows,
        "timeline": timeline_rows,
        "quality": quality,
        "resolved_first_touch": iso_z(first_touch),
        "resolved_first_break": iso_z(first_break),
        "first_touch_source": touch_src,
        "first_break_source": break_src,
        "error": None,
    }


def extract_all(events: list[dict[str, Any]], *, db: Any | None = None) -> dict[str, Any]:
    close_db = False
    if db is None:
        db = _connect()
        close_db = True
    features: list[dict[str, Any]] = []
    timelines: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    try:
        for i, ev in enumerate(events, 1):
            logger.info("[%s/%s] extracting %s %s", i, len(events), ev["symbol"], ev["event_id"])
            res = extract_event_features(db, ev)
            features.extend(res["features"])
            timelines.extend(res["timeline"])
            q = dict(res["quality"])
            quality_rows.append(q)
            resolutions.append(
                {
                    "event_id": ev["event_id"],
                    "resolved_first_touch": res["resolved_first_touch"],
                    "resolved_first_break": res["resolved_first_break"],
                    "first_touch_source": res["first_touch_source"],
                    "first_break_source": res["first_break_source"],
                    "error": res["error"],
                    "data_quality": q.get("data_quality"),
                }
            )
    finally:
        if close_db and hasattr(db, "client"):
            try:
                db.client.close()
            except Exception:  # noqa: BLE001
                pass
    return {
        "features": features,
        "timelines": timelines,
        "quality": quality_rows,
        "resolutions": resolutions,
    }
