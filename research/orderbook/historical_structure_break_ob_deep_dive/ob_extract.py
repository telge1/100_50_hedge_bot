"""Historical OB window reconstruction + wall lifecycle (ORDERBOOK_ONLY)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.orderbook.historical_bybit_replay import (
    DEFAULT_DATA_ROOT,
    HistoricalBybitReplayer,
    ObMessage,
    OrderBook,
    ReplayError,
    SequenceStatus,
    day_file_path,
    iter_messages,
)

DEPTH_BPS = (5, 10, 25, 50)
MARKERS = (
    ("PRE_5M", -300),
    ("PRE_2M", -120),
    ("PRE_1M", -60),
    ("PRE_30S", -30),
    ("PRE_10S", -10),
    ("FIRST_TOUCH", None),
    ("FIRST_BREAK", None),
    ("POST_5S", 5),
    ("POST_10S", 10),
    ("POST_20S", 20),
    ("POST_30S", 30),
    ("POST_60S", 60),
    ("POST_120S", 120),
    ("POST_300S", 300),
)


def ts_to_ms(ts: datetime | str) -> int:
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return int(ts.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def depth_within_bps(book: OrderBook, *, side: str, ref: float, bps: float) -> float:
    if ref <= 0:
        return 0.0
    band = ref * bps / 10_000.0
    levels = book.bids if side == "bid" else book.asks
    total = 0.0
    for px, qty in levels.items():
        p, q = float(px), float(qty)
        if q <= 0:
            continue
        if abs(p - ref) <= band:
            total += p * q
    return total


def strongest_wall(book: OrderBook, *, side: str, ref: float, max_bps: float = 50.0) -> dict[str, Any]:
    levels = book.bids if side == "bid" else book.asks
    best_px, best_n, best_q = None, 0.0, 0.0
    for px, qty in levels.items():
        p, q = float(px), float(qty)
        if q <= 0 or ref <= 0:
            continue
        if abs(p - ref) / ref * 1e4 > max_bps:
            continue
        n = p * q
        if n > best_n:
            best_n, best_px, best_q = n, p, q
    return {
        "price": best_px,
        "qty": best_q if best_px is not None else 0.0,
        "notional": best_n if best_px is not None else 0.0,
        "distance_to_ref_bps": None if best_px is None else (best_px - ref) / ref * 1e4,
    }


def book_snapshot(book: OrderBook, *, level: float, ts_ms: int, direction: str) -> dict[str, Any]:
    bb = book.best_bid()
    ba = book.best_ask()
    bb_f = float(bb) if bb is not None else None
    ba_f = float(ba) if ba is not None else None
    mid = None if bb_f is None or ba_f is None else (bb_f + ba_f) / 2.0
    spread = None if mid is None or mid <= 0 or bb_f is None or ba_f is None else (ba_f - bb_f) / mid * 1e4
    dist = None if mid is None or level <= 0 else (mid - level) / level * 1e4
    if direction == "bearish":
        beyond = bb_f is not None and bb_f < level
        rel = "below" if mid is not None and mid < level else "above_or_at"
    else:
        beyond = ba_f is not None and ba_f > level
        rel = "above" if mid is not None and mid > level else "below_or_at"
    out: dict[str, Any] = {
        "ts_ms": ts_ms,
        "ts": ms_to_iso(ts_ms),
        "mid": mid,
        "best_bid": bb_f,
        "best_ask": ba_f,
        "spread_bps": spread,
        "distance_to_level_bps": dist,
        "position_vs_level": rel,
        "bbo_beyond_level": int(bool(beyond)),
    }
    for bps in DEPTH_BPS:
        out[f"bid_depth_{bps}bps"] = depth_within_bps(book, side="bid", ref=level, bps=float(bps))
        out[f"ask_depth_{bps}bps"] = depth_within_bps(book, side="ask", ref=level, bps=float(bps))
    bid_w = strongest_wall(book, side="bid", ref=level)
    ask_w = strongest_wall(book, side="ask", ref=level)
    out["strongest_bid_wall_price"] = bid_w["price"]
    out["strongest_bid_wall_qty"] = bid_w["qty"]
    out["strongest_bid_wall_notional"] = bid_w["notional"]
    out["strongest_bid_wall_dist_bps"] = bid_w["distance_to_ref_bps"]
    out["strongest_ask_wall_price"] = ask_w["price"]
    out["strongest_ask_wall_qty"] = ask_w["qty"]
    out["strongest_ask_wall_notional"] = ask_w["notional"]
    out["strongest_ask_wall_dist_bps"] = ask_w["distance_to_ref_bps"]
    if direction == "bearish":
        out["support_wall_price"] = bid_w["price"]
        out["support_wall_notional"] = bid_w["notional"]
        out["break_wall_price"] = ask_w["price"]
        out["break_wall_notional"] = ask_w["notional"]
        out["near_support_depth_10"] = out["bid_depth_10bps"]
        out["near_break_depth_10"] = out["ask_depth_10bps"]
    else:
        out["support_wall_price"] = ask_w["price"]
        out["support_wall_notional"] = ask_w["notional"]
        out["break_wall_price"] = bid_w["price"]
        out["break_wall_notional"] = bid_w["notional"]
        out["near_support_depth_10"] = out["ask_depth_10bps"]
        out["near_break_depth_10"] = out["bid_depth_10bps"]
    return out


def replay_metric_samples(
    path: Path,
    *,
    symbol: str,
    level: float,
    direction: str,
    sample_ts_ms: list[int],
    end_ts_ms: int,
) -> tuple[dict[int, dict[str, Any]], SequenceStatus, dict[str, Any]]:
    """Single-pass causal replay; store metric snapshots (no book clones)."""
    multi, status, meta = replay_metric_samples_multi(
        path,
        symbol=symbol,
        specs=[
            {
                "event_id": "_single",
                "level": level,
                "direction": direction,
                "sample_ts_ms": sample_ts_ms,
            }
        ],
        end_ts_ms=end_ts_ms,
    )
    return multi.get("_single", {}), status, meta


def replay_metric_samples_multi(
    path: Path,
    *,
    symbol: str,
    specs: list[dict[str, Any]],
    end_ts_ms: int,
) -> tuple[dict[str, dict[int, dict[str, Any]]], SequenceStatus, dict[str, Any]]:
    """One day file pass; capture per-event metric snapshots at requested times."""
    replayer = HistoricalBybitReplayer()
    remaining: dict[str, list[int]] = {
        str(s["event_id"]): sorted(set(int(t) for t in s["sample_ts_ms"])) for s in specs
    }
    levels = {str(s["event_id"]): float(s["level"]) for s in specs}
    directions = {str(s["event_id"]): str(s["direction"]) for s in specs}
    samples: dict[str, dict[int, dict[str, Any]]] = {eid: {} for eid in remaining}
    meta: dict[str, Any] = {"messages_applied": 0, "path": str(path), "n_specs": len(specs)}

    def flush(ts_cap: int) -> None:
        for eid, rem in remaining.items():
            while rem and rem[0] < ts_cap:
                ts = rem.pop(0)
                samples[eid][ts] = book_snapshot(
                    replayer.book, level=levels[eid], ts_ms=ts, direction=directions[eid]
                )

    def flush_eq(ts_eq: int) -> None:
        for eid, rem in remaining.items():
            while rem and rem[0] == ts_eq:
                ts = rem.pop(0)
                samples[eid][ts] = book_snapshot(
                    replayer.book, level=levels[eid], ts_ms=ts, direction=directions[eid]
                )

    for item in iter_messages(path, expected_symbol=symbol, skip_malformed=True):
        if not isinstance(item, ObMessage):
            continue
        msg = item
        if msg.ts_ms > end_ts_ms:
            break
        flush(msg.ts_ms)
        replayer.apply_message(msg)
        meta["messages_applied"] = replayer._messages_applied
        flush_eq(msg.ts_ms)
    flush(end_ts_ms + 1)
    return samples, replayer.diag.status(), {
        **meta,
        "diag": {
            "snapshots": replayer.diag.snapshots_seen,
            "deltas": replayer.diag.deltas_applied,
            "u_gaps": replayer.diag.u_gap_count,
            "status": replayer.diag.status().value,
        },
    }


def detect_touch_break_from_snaps(
    samples: dict[int, dict[str, Any]],
    *,
    level: float,
    direction: str,
    touch_bps: float = 5.0,
) -> dict[str, int | None]:
    first_touch = None
    first_break = None
    for ts in sorted(samples):
        snap = samples[ts]
        mid = snap.get("mid")
        bb, ba = snap.get("best_bid"), snap.get("best_ask")
        if mid is None or level <= 0:
            continue
        dist = abs(mid - level) / level * 1e4
        if first_touch is None and dist <= touch_bps:
            first_touch = ts
        if direction == "bearish":
            if first_break is None and bb is not None and bb < level:
                first_break = ts
        else:
            if first_break is None and ba is not None and ba > level:
                first_break = ts
        if first_touch is not None and first_break is not None:
            break
    return {"first_touch_ts_ms": first_touch, "first_break_ts_ms": first_break}


def wall_lifecycle_from_snaps(
    samples: dict[int, dict[str, Any]],
    *,
    event_id: str,
) -> list[dict[str, Any]]:
    rows = []
    prev_n = None
    prev_px = None
    for ts in sorted(samples):
        snap = samples[ts]
        px = snap.get("support_wall_price")
        n = snap.get("support_wall_notional") or 0.0
        level = 1.0  # relative relocate uses absolute px delta via snap distances
        state = "ABSENT"
        pull = refill = relocate = 0
        if px is not None and n > 0:
            state = "PRESENT"
            if prev_n is not None and prev_n > 0:
                if n < prev_n * 0.7:
                    pull = 1
                    state = "SHRINKING"
                elif n > prev_n * 1.3:
                    refill = 1
                    state = "REFILLING"
                if prev_px is not None and abs(px - prev_px) / max(abs(px), 1e-12) * 1e4 > 2:
                    relocate = 1
                    state = "RELOCATED"
            prev_n, prev_px = n, px
        else:
            if prev_n is not None and prev_n > 0:
                state = "REMOVED"
                pull = 1
            prev_n, prev_px = 0.0, None
        rows.append(
            {
                "event_id": event_id,
                "ts": ms_to_iso(ts),
                "ts_ms": ts,
                "support_wall_price": px,
                "support_wall_notional": n,
                "wall_state": state,
                "pull_proxy": pull,
                "refill_proxy": refill,
                "relocate_proxy": relocate,
                "consumption_vs_pull": "UNKNOWN_NO_TRADES",
                "note": "ORDERBOOK_ONLY: cannot separate pull vs trade consumption",
            }
        )
    return rows


def classify_event_behavior(lifecycle: list[dict[str, Any]], timepoints: list[dict[str, Any]]) -> str:
    if not lifecycle or not timepoints:
        return "NO_CLEAR_WALL_BEHAVIOR"
    break_rows = [r for r in timepoints if r.get("marker") == "FIRST_BREAK"]
    pre = [r for r in lifecycle if break_rows and r["ts_ms"] < break_rows[0]["ts_ms"]]
    at = [r for r in lifecycle if break_rows and abs(r["ts_ms"] - break_rows[0]["ts_ms"]) <= 2000]
    post = [r for r in lifecycle if break_rows and 0 < r["ts_ms"] - break_rows[0]["ts_ms"] <= 120_000]

    pulled_before = any(
        r.get("pull_proxy") and r.get("wall_state") in {"SHRINKING", "REMOVED", "RELOCATED"} for r in pre[-6:]
    )
    removed_at = any(r.get("wall_state") == "REMOVED" for r in at) or (
        at and (at[0].get("support_wall_notional") or 0) < 1
    )
    refill_after = any(r.get("refill_proxy") or r.get("wall_state") == "REFILLING" for r in post)
    beyond_at = break_rows and break_rows[0].get("bbo_beyond_level")
    later = [r for r in timepoints if str(r.get("marker", "")).startswith("POST_")]
    reclaimed = any(not r.get("bbo_beyond_level") for r in later[2:6]) if later else False

    if pulled_before and beyond_at:
        return "WALL_PULLED_BEFORE_BREAK"
    if removed_at and beyond_at and not reclaimed:
        return "WALL_CONSUMED_OR_REMOVED_BREAK"
    if refill_after and reclaimed:
        return "REFILL_THEN_RECLAIM"
    if beyond_at and not reclaimed and later:
        return "BREAK_ACCEPTED_NO_QUICK_RECLAIM"
    if reclaimed:
        return "WALL_HELD_OR_RECLAIM"
    return "MIXED"


def _event_window_ms(event: dict[str, Any]) -> tuple[int, int]:
    """FIRST_BREAK±5m proxy: candle_open−15m … available_at+5m (touch buffer)."""
    avail_ms = ts_to_ms(event["available_at"])
    candle_open_ms = ts_to_ms(event["candle_open"])
    win_start = min(candle_open_ms - 900_000, avail_ms - 900_000)
    win_end = avail_ms + 300_000
    return win_start, win_end


def _finalize_event_from_snaps(
    event: dict[str, Any],
    snaps: dict[int, dict[str, Any]],
    *,
    status: SequenceStatus,
    meta: dict[str, Any],
    path: Path,
    win_start: int,
    win_end: int,
) -> dict[str, Any]:
    level = float(event["level"])
    direction = event["direction"]
    quality = {
        "event_id": event["event_id"],
        "symbol": event["symbol"],
        "date": event["date"],
        "ob_path": str(path),
        "ob_exists": True,
        "trades_available": False,
        "mode": "ORDERBOOK_ONLY",
        "sequence_status": status.value,
        "messages_applied": meta.get("messages_applied"),
        "window_start": ms_to_iso(win_start),
        "window_end": ms_to_iso(win_end),
    }
    avail_ms = ts_to_ms(event["available_at"])
    candle_open_ms = ts_to_ms(event["candle_open"])
    detected = detect_touch_break_from_snaps(snaps, level=level, direction=direction)
    touch_ms = detected["first_touch_ts_ms"] or candle_open_ms
    break_ms = detected["first_break_ts_ms"] or avail_ms

    # Expand window note if touch earlier than default ±5m around break
    if touch_ms < break_ms - 300_000:
        quality["window_expanded_for_touch"] = True

    sample_map: dict[str, int] = {}
    for name, off in MARKERS:
        if name == "FIRST_TOUCH":
            sample_map[name] = int(touch_ms)
        elif name == "FIRST_BREAK":
            sample_map[name] = int(break_ms)
        elif off is not None:
            sample_map[name] = int(break_ms + off * 1000)

    if status == SequenceStatus.INVALID:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sequence_invalid"
    elif status == SequenceStatus.RESET_SEEN:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sequence_reset_seen"
    elif len(snaps) < 20:
        quality["data_quality"] = "DATA_WARNING"
        quality["reason"] = "sparse_samples"
    else:
        quality["data_quality"] = "DATA_VALID"

    # Flag scanner-fallback breaks where BBO never crossed the level in-window
    br_snap = snaps.get(int(break_ms)) or (
        snaps[max(t for t in snaps if t <= break_ms)] if any(t <= break_ms for t in snaps) else None
    )
    if detected["first_break_ts_ms"] is None:
        quality["first_break_source"] = "scanner_available_at_fallback"
        if br_snap is not None and not br_snap.get("bbo_beyond_level"):
            quality["data_quality"] = "DATA_WARNING"
            quality["reason"] = (quality.get("reason") or "") + "|no_bbo_break_in_window"
    else:
        quality["first_break_source"] = "ob_bbo"

    def nearest(ts: int) -> dict[str, Any] | None:
        if ts in snaps:
            return snaps[ts]
        earlier = [t for t in snaps if t <= ts]
        return snaps[max(earlier)] if earlier else None

    timepoints = []
    for marker, ts in sample_map.items():
        snap = nearest(ts)
        if snap is None:
            continue
        row = {"event_id": event["event_id"], "marker": marker, "relative_to": "FIRST_BREAK", **snap}
        for lag_s, lab in ((10, "10s"), (30, "30s"), (60, "60s")):
            lag = nearest(ts - lag_s * 1000)
            if lag is None:
                row[f"support_wall_present_{lab}"] = None
                row[f"support_wall_notional_{lab}"] = None
                row[f"support_wall_size_change_{lab}"] = None
                continue
            row[f"support_wall_present_{lab}"] = int((lag.get("support_wall_notional") or 0) > 0)
            row[f"support_wall_notional_{lab}"] = lag.get("support_wall_notional")
            row[f"support_wall_size_change_{lab}"] = (snap.get("support_wall_notional") or 0) - (
                lag.get("support_wall_notional") or 0
            )
        timepoints.append(row)

    lifecycle = wall_lifecycle_from_snaps(snaps, event_id=event["event_id"])
    timeline = [
        {
            "event_id": event["event_id"],
            "marker": tp["marker"],
            "absolute_ts": tp["ts"],
            "price": tp.get("mid"),
            "distance_to_level_bps": tp.get("distance_to_level_bps"),
            "best_bid": tp.get("best_bid"),
            "best_ask": tp.get("best_ask"),
            "near_bid_depth": tp.get("bid_depth_10bps"),
            "near_ask_depth": tp.get("ask_depth_10bps"),
            "strongest_bid_wall": tp.get("strongest_bid_wall_notional"),
            "strongest_ask_wall": tp.get("strongest_ask_wall_notional"),
            "support_wall_notional": tp.get("support_wall_notional"),
            "support_wall_size_change_10s": tp.get("support_wall_size_change_10s"),
            "bbo_beyond_level": tp.get("bbo_beyond_level"),
            "trade_flow": "NA_ORDERBOOK_ONLY",
            "event_marker": tp["marker"],
        }
        for tp in timepoints
    ]
    classification = classify_event_behavior(lifecycle, timepoints)
    return {
        "quality": quality,
        "timepoints": timepoints,
        "lifecycle": lifecycle,
        "timeline": timeline,
        "classification": classification,
        "resolved_first_touch": ms_to_iso(int(touch_ms)),
        "resolved_first_break": ms_to_iso(int(break_ms)),
        "scanner_available_at": event["available_at"],
    }


def process_event_ob(event: dict[str, Any], *, data_root: Path = DEFAULT_DATA_ROOT) -> dict[str, Any]:
    return process_events_for_day([event], data_root=data_root)[event["event_id"]]


def process_events_for_day(
    events: list[dict[str, Any]],
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
) -> dict[str, dict[str, Any]]:
    """Replay one OB day once for all events on that (symbol, date).

    Events must share the same symbol and OB ``date`` file. Windows that start
    before that calendar day are clipped to day-start and flagged WARNING.
    """
    if not events:
        return {}
    symbol = events[0]["symbol"]
    date = events[0]["date"]
    path = day_file_path(symbol, date, data_root=data_root)
    out: dict[str, dict[str, Any]] = {}
    if not path.exists():
        for ev in events:
            out[ev["event_id"]] = {
                "quality": {
                    "event_id": ev["event_id"],
                    "symbol": symbol,
                    "date": date,
                    "ob_path": str(path),
                    "ob_exists": False,
                    "trades_available": False,
                    "mode": "ORDERBOOK_ONLY",
                    "data_quality": "DATA_INVALID",
                    "reason": "missing_ob_file",
                },
                "timepoints": [],
                "lifecycle": [],
                "timeline": [],
                "classification": "DATA_INVALID",
            }
        return out

    day_start_ms = ts_to_ms(f"{date}T00:00:00.000Z")
    windows: dict[str, tuple[int, int, bool]] = {}
    for ev in events:
        ws, we = _event_window_ms(ev)
        clipped = False
        if ws < day_start_ms:
            ws = day_start_ms
            clipped = True
        windows[ev["event_id"]] = (ws, we, clipped)

    end_ts = max(w[1] for w in windows.values())
    specs = []
    for ev in events:
        ws, we, _ = windows[ev["event_id"]]
        specs.append(
            {
                "event_id": ev["event_id"],
                "level": float(ev["level"]),
                "direction": ev["direction"],
                "sample_ts_ms": list(range(ws, we + 1, 5_000)),
            }
        )
    try:
        multi, status, meta = replay_metric_samples_multi(
            path, symbol=symbol, specs=specs, end_ts_ms=end_ts
        )
    except ReplayError as exc:
        for ev in events:
            out[ev["event_id"]] = {
                "quality": {
                    "event_id": ev["event_id"],
                    "symbol": symbol,
                    "date": date,
                    "ob_path": str(path),
                    "ob_exists": True,
                    "trades_available": False,
                    "mode": "ORDERBOOK_ONLY",
                    "data_quality": "DATA_INVALID",
                    "reason": f"replay_error:{exc}",
                },
                "timepoints": [],
                "lifecycle": [],
                "timeline": [],
                "classification": "DATA_INVALID",
            }
        return out

    for ev in events:
        ws, we, clipped = windows[ev["event_id"]]
        res = _finalize_event_from_snaps(
            ev,
            multi.get(ev["event_id"], {}),
            status=status,
            meta=meta,
            path=path,
            win_start=ws,
            win_end=we,
        )
        if clipped:
            q = res["quality"]
            if q.get("data_quality") == "DATA_VALID":
                q["data_quality"] = "DATA_WARNING"
            q["reason"] = (q.get("reason") or "") + "|window_clipped_to_ob_day"
            q["window_clipped"] = True
        out[ev["event_id"]] = res
    return out
