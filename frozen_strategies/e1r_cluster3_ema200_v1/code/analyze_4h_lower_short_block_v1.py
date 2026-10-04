"""Analyze 4h-lower short-block rules (no scanner changes). Prefix 4h pane only."""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
FOCUS_FROM = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)
FOCUS_TO = datetime(2026, 6, 8, 23, 59, 59, tzinfo=timezone.utc)
CONTROL_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
CONTROL_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)
OUT_JSON = ROOT / "short_block_4h_lower_analysis_v1.json"
OUT_MD = ROOT / "short_block_4h_lower_analysis_v1.md"
CAUSAL_CSV = ROOT / "short_entry_15m_causal_v1.csv"
HORIZON_BARS = 48 * 4  # 48h on 15m
MIN_POOL_GAP_PCT = 0.5
FLOOR_EPISODE_HOURS_DEFAULT = 72
FLOOR_EPISODE_PCT_DEFAULT = 7.0
FLOOR_EPISODE_HOURS_GRID = (24, 48, 72)
FLOOR_EPISODE_PCT_GRID = (3.0, 5.0, 7.0)
PROBLEM_SIGNAL_NUMS = (4, 5, 6, 7)
R1_R2_BLOCK_RULE = "R1_top_within_3.0pct"  # plus R2 zone rules


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


@dataclass
class H4Snapshot:
    decision_time: datetime
    pane_4h_len: int
    tip: str
    lowers: list[dict]
    deepest_active: dict | None
    pool_below_deepest: bool
    gap_pct_below: float | None


def load_15m_bars(symbol: str) -> list[dict]:
    from backtester.short.dashboard.research_charts.lld_research_kernel import load_pane_candles
    from pool_scan.clock import bar_close

    _packed, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
    )
    bars = []
    for candle in candles:
        ot = _utc(candle.timestamp)
        bars.append(
            {
                "open_time": ot,
                "close_time": bar_close(ot, "15m"),
                "open": float(candle.open),
                "high": float(candle.high),
                "low": float(candle.low),
                "close": float(candle.close),
            }
        )
    return bars


_4H_CACHE: dict[tuple[str, datetime], H4Snapshot] = {}
_4H_CANDLES: list | None = None


def _load_4h_candles(symbol: str) -> list:
    global _4H_CANDLES
    if _4H_CANDLES is not None:
        return _4H_CANDLES
    from backtester.short.dashboard.research_charts.lld_research_kernel import load_pane_candles

    _packed, candles = load_pane_candles(
        symbol,
        "4h",
        from_unix=int(PANE_FROM.timestamp()),
        to_unix=int(PANE_TO.timestamp()),
    )
    _4H_CANDLES = candles
    return candles


def h4_snapshot(symbol: str, decision_time: datetime) -> H4Snapshot:
    from backtester.short.dashboard.research_charts.lld_research_kernel import (
        compute_lld_selected,
        filter_rows_at_decision,
        pool_known_at,
        pool_row,
        resolve_tip,
        ui_lld_config,
    )
    from pool_scan.clock import bar_close

    decision_time = _utc(decision_time)
    # cache key: last closed 4h bar close at decision
    key_moment = decision_time
    for c in _load_4h_candles(symbol):
        ct = bar_close(_utc(c.timestamp), "4h")
        if ct <= decision_time:
            key_moment = ct
    cache_key = (symbol, key_moment)
    if cache_key in _4H_CACHE:
        return _4H_CACHE[cache_key]

    prefix = []
    for c in _load_4h_candles(symbol):
        ot = _utc(c.timestamp)
        if bar_close(ot, "4h") <= decision_time:
            prefix.append(c)
    if not prefix:
        snap = H4Snapshot(
            decision_time=decision_time,
            pane_4h_len=0,
            tip="",
            lowers=[],
            deepest_active=None,
            pool_below_deepest=False,
            gap_pct_below=None,
        )
        _4H_CACHE[cache_key] = snap
        return snap
    cfg = ui_lld_config("4h")
    selected, _cfg, _res, _tip = compute_lld_selected(
        prefix, "4h", cfg, tip=resolve_tip(prefix, None) if prefix else None
    )
    rows = []
    for pool in selected:
        row = pool_row(pool)
        if not row or row["side"] != "lower":
            continue
        known = _utc(row["known"])
        active = pool_known_at(row, decision_time)
        rows.append(
            {
                "pool_id": row["pool_id"],
                "bottom": row["bottom"],
                "top": row["top"],
                "known": known.isoformat(),
                "break_at": row["break_at"].isoformat() if row.get("break_at") else None,
                "status": "active" if active else "broken_or_unknown",
                "known_at_decision": known <= decision_time,
            }
        )
    active_lowers = [
        r
        for r in rows
        if r["status"] == "active" and r["known_at_decision"]
    ]
    active_lowers.sort(key=lambda r: r["bottom"])
    deepest = active_lowers[0] if active_lowers else None
    pool_below = False
    gap_below = None
    if deepest:
        for r in active_lowers[1:]:
            if r["top"] < deepest["bottom"]:
                gap = (deepest["bottom"] - r["top"]) / deepest["bottom"] * 100.0
                if gap >= MIN_POOL_GAP_PCT:
                    pool_below = True
                    gap_below = gap
                    break
    snap = H4Snapshot(
        decision_time=decision_time,
        pane_4h_len=len(prefix),
        tip=resolve_tip(prefix, None).isoformat() if prefix else "",
        lowers=rows,
        deepest_active=deepest,
        pool_below_deepest=pool_below,
        gap_pct_below=gap_below,
    )
    _4H_CACHE[cache_key] = snap
    return snap


def price_vs_pool(close: float, pool: dict) -> dict:
    b, t = pool["bottom"], pool["top"]
    dist_top_pct = (close - t) / close * 100.0
    dist_bottom_pct = (close - b) / close * 100.0
    if b <= close <= t:
        pos = "inside_zone"
    elif close > t:
        pos = "above_top"
    else:
        pos = "below_bottom"
    return {
        "dist_to_top_pct": round(dist_top_pct, 4),
        "dist_to_bottom_pct": round(dist_bottom_pct, 4),
        "position": pos,
    }


def build_touch_timeline(bars: list[dict], snap_by_close: dict[datetime, H4Snapshot]) -> dict:
    """Track touches of deepest-active zone (zone changes when deepest changes)."""
    last_touch: datetime | None = None
    last_bounce_time: datetime | None = None
    bounce_pct: float | None = None
    touch_low: float | None = None
    current_deepest_id: str | None = None
    events: list[dict] = []

    for bar in bars:
        ct = bar["close_time"]
        if ct < FOCUS_FROM or ct > FOCUS_TO:
            continue
        snap = snap_by_close.get(ct)
        if snap is None or snap.deepest_active is None:
            continue
        d = snap.deepest_active
        if d["pool_id"] != current_deepest_id:
            current_deepest_id = d["pool_id"]
            last_touch = None
            touch_low = None
        b, t = d["bottom"], d["top"]
        touched = bar["low"] <= t and bar["high"] >= b
        if touched:
            if last_touch is None or (ct - last_touch) > timedelta(hours=8):
                events.append({"type": "touch", "time": ct.isoformat(), "pool_id": d["pool_id"], "low": bar["low"]})
            last_touch = ct
            touch_low = bar["low"] if touch_low is None else min(touch_low, bar["low"])
        if touch_low is not None and bar["high"] > touch_low * 1.005:
            if last_bounce_time is None or ct > last_bounce_time:
                bounce_pct = (bar["high"] - touch_low) / touch_low * 100.0
                if bounce_pct >= 1.0:
                    last_bounce_time = ct
                    events.append(
                        {
                            "type": "bounce_1pct",
                            "time": ct.isoformat(),
                            "from_low": touch_low,
                            "high": bar["high"],
                            "bounce_pct": round(bounce_pct, 4),
                        }
                    )
    return {"events": events}


def forward_outcome(bars: list[dict], entry_idx: int, entry: float, stop: float, tp: float) -> dict:
    mfe = 0.0
    mae = 0.0
    outcome = "open"
    exit_bar = None
    mfe_first_idx: int | None = None
    mae_first_idx: int | None = None
    end = min(len(bars), entry_idx + 1 + HORIZON_BARS)
    for i in range(entry_idx + 1, end):
        bar = bars[i]
        fav = (entry - bar["low"]) / entry * 100.0
        adv = (bar["high"] - entry) / entry * 100.0
        if fav > 0.01 and mfe_first_idx is None:
            mfe_first_idx = i
        if adv > 0.01 and mae_first_idx is None:
            mae_first_idx = i
        mfe = max(mfe, fav)
        mae = max(mae, adv)
        if bar["high"] >= stop and outcome == "open":
            outcome = "stop"
            exit_bar = bar["close_time"].isoformat()
            break
        if bar["low"] <= tp and outcome == "open":
            outcome = "tp"
            exit_bar = bar["close_time"].isoformat()
            break
    if outcome == "open":
        outcome = "horizon"
        exit_bar = bars[end - 1]["close_time"].isoformat() if end > entry_idx + 1 else None
    good = outcome == "tp" or (mfe >= 0.8 and mfe > mae)
    bad = outcome == "stop" or (mae >= 0.8 and mae >= mfe)
    if good and not bad:
        result_status = "good"
    elif bad and not good:
        result_status = "bad"
    else:
        result_status = "unresolved"
    mae_before_mfe = mae_first_idx is not None and (
        mfe_first_idx is None or mae_first_idx < mfe_first_idx
    )
    return {
        "outcome": outcome,
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "mae_before_mfe": mae_before_mfe,
        "mfe_ge_0_41_pct": mfe >= 0.41,
        "result_status": result_status,
        "quality": result_status,
        "exit_bar": exit_bar,
    }


def build_floor_episode_by_close(bars: list[dict], episode_hours: float) -> dict[datetime, dict]:
    """Floor episode: no reset when a deeper 4h pool appears; window extends on each touch."""
    first_touch: datetime | None = None
    episode_start: datetime | None = None
    session_floor_top: float | None = None
    block_until: datetime | None = None
    out: dict[datetime, dict] = {}
    for bar in bars:
        ct = bar["close_time"]
        if block_until is not None and ct >= block_until:
            first_touch = None
            episode_start = None
            session_floor_top = None
            block_until = None
        snap = h4_snapshot("XRPUSDT", ct)
        d = snap.deepest_active
        if d and not snap.pool_below_deepest:
            touched = bar["low"] <= d["top"] and bar["high"] >= d["bottom"]
            if touched:
                if first_touch is None:
                    first_touch = ct
                    episode_start = ct
                session_floor_top = (
                    d["top"]
                    if session_floor_top is None
                    else min(session_floor_top, d["top"])
                )
                block_until = ct + timedelta(hours=episode_hours)
        in_window = bool(block_until and ct < block_until and session_floor_top is not None)
        dist = None
        if session_floor_top is not None:
            dist = (bar["close"] - session_floor_top) / bar["close"] * 100.0
        hrs_since = None
        if first_touch is not None:
            hrs_since = (ct - first_touch).total_seconds() / 3600.0
        out[ct] = {
            "episode_start": episode_start.isoformat() if episode_start else None,
            "session_floor_top": session_floor_top,
            "first_touch_time": first_touch.isoformat() if first_touch else None,
            "hours_since_first_touch": round(hrs_since, 4) if hrs_since is not None else None,
            "dist_to_session_floor_top_pct": round(dist, 4) if dist is not None else None,
            "in_episode_window": in_window,
        }
    return out


def floor_episode_blocks(close: float, state: dict | None, max_dist_pct: float) -> bool:
    if not state or not state.get("in_episode_window"):
        return False
    dist = state.get("dist_to_session_floor_top_pct")
    if dist is None:
        return False
    return dist <= max_dist_pct


def blocked_by_r1_r2(rules: dict[str, bool]) -> bool:
    return bool(
        rules.get(R1_R2_BLOCK_RULE)
        or rules.get("R2_inside_zone")
        or rules.get("R2_below_bottom")
    )


def load_signals() -> list[dict]:
    rows = []
    with CAUSAL_CSV.open() as f:
        for row in csv.DictReader(f):
            if row["symbol"] != "XRPUSDT":
                continue
            row["_entry"] = _utc(datetime.fromisoformat(row["entry_open"]))
            row["_close"] = float(row["close"])
            row["_stop"] = float(row["stop_price"])
            row["_tp"] = float(row["tp_top"]) if row["tp_top"] else None
            rows.append(row)
    return rows


def rule_states(
    close: float,
    snap: H4Snapshot,
    *,
    sticky_until: datetime | None,
    bounce_block_until: datetime | None,
) -> dict[str, bool]:
    d = snap.deepest_active
    if d is None:
        base = {k: False for k in ("R1", "R2", "R3", "R4")}
        base["no_deepest_pool"] = True
        return base
    pv = price_vs_pool(close, d)
    no_lower_below = not snap.pool_below_deepest
    rules: dict[str, bool] = {"no_deepest_pool": False, "no_lower_below": no_lower_below}
    # R1 distance to deepest top (only when nothing meaningful below)
    for pct in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0):
        rules[f"R1_top_within_{pct}pct"] = no_lower_below and pv["dist_to_top_pct"] <= pct
    rules["R2_inside_zone"] = no_lower_below and pv["position"] == "inside_zone"
    rules["R2_below_bottom"] = no_lower_below and pv["position"] == "below_bottom"
    rules["R3_sticky"] = sticky_until is not None and snap.decision_time < sticky_until
    rules["R4_bounce_sticky"] = bounce_block_until is not None and snap.decision_time < bounce_block_until
    # 4h reclaim: close back above deepest top after having been inside/below
    rules["R4_reclaim_above_top"] = no_lower_below and pv["position"] == "above_top" and pv["dist_to_top_pct"] < 3.0
    return rules


def simulate_sticky(bars: list[dict], hours: float) -> dict[datetime, datetime | None]:
    """After first touch of deepest zone (no pool below), block for `hours`."""
    sticky: dict[datetime, datetime | None] = {}
    active_until: datetime | None = None
    for bar in bars:
        ct = bar["close_time"]
        snap = h4_snapshot("XRPUSDT", ct)
        sticky[ct] = active_until
        if snap.deepest_active is None or snap.pool_below_deepest:
            continue
        d = snap.deepest_active
        touched = bar["low"] <= d["top"] and bar["high"] >= d["bottom"]
        if touched and active_until is None:
            active_until = ct + timedelta(hours=hours)
        # reset when new deeper pool known (bottom drops)
    return sticky


def main() -> None:
    _ensure_paths()
    bars = load_15m_bars("XRPUSDT")
    by_close = {b["close_time"]: b for b in bars}
    bar_index = {b["close_time"]: i for i, b in enumerate(bars)}

    snap_by_close: dict[datetime, H4Snapshot] = {}
    for bar in bars:
        if bar["close_time"] < FOCUS_FROM - timedelta(days=1) or bar["close_time"] > FOCUS_TO:
            continue
        snap_by_close[bar["close_time"]] = h4_snapshot("XRPUSDT", bar["close_time"])

    signals = load_signals()
    focus_signals = []
    for i, sig in enumerate(signals, start=1):
        sig["_signal_num"] = i
        if FOCUS_FROM <= sig["_entry"] <= FOCUS_TO:
            focus_signals.append(sig)
    june_july = [s for s in signals if CONTROL_FROM <= s["_entry"] <= CONTROL_TO]

    # Sticky maps (recompute with state machine)
    sticky_24: dict[datetime, datetime | None] = {}
    sticky_48: dict[datetime, datetime | None] = {}
    bounce_sticky: dict[datetime, datetime | None] = {}
    last_deepest_bottom: float | None = None
    sticky24_until: datetime | None = None
    sticky48_until: datetime | None = None
    bounce_until: datetime | None = None
    touch_low: float | None = None
    had_touch = False
    last_touch: datetime | None = None

    chronicle: list[dict] = []
    # Sticky state must run from pane start so June 7 sees prior touches.
    for bar in bars:
        ct = bar["close_time"]
        if ct < PANE_FROM or ct > CONTROL_TO + timedelta(days=7):
            continue
        snap = h4_snapshot("XRPUSDT", ct)
        sticky_24[ct] = sticky24_until
        sticky_48[ct] = sticky48_until
        bounce_sticky[ct] = bounce_until

        d = snap.deepest_active
        if d and (last_deepest_bottom is None or d["bottom"] < last_deepest_bottom - 1e-6):
            sticky24_until = None
            sticky48_until = None
            bounce_until = None
            had_touch = False
            touch_low = None
            last_touch = None
            last_deepest_bottom = d["bottom"]

        if d and not snap.pool_below_deepest:
            touched = bar["low"] <= d["top"] and bar["high"] >= d["bottom"]
            if touched:
                had_touch = True
                last_touch = ct
                touch_low = bar["low"] if touch_low is None else min(touch_low, bar["low"])
                if sticky24_until is None:
                    sticky24_until = ct + timedelta(hours=24)
                if sticky48_until is None:
                    sticky48_until = ct + timedelta(hours=48)
            if had_touch and touch_low and bar["high"] >= touch_low * 1.01:
                bounce_until = ct + timedelta(hours=36)

        sig_hit = [s for s in focus_signals if s["_entry"] == bar["open_time"]]
        entry = {
            "close_time": ct.isoformat(),
            "open_time": bar["open_time"].isoformat(),
            "close": bar["close"],
            "signal": sig_hit[0]["_signal_num"] if sig_hit else None,
        }
        if d:
            entry.update(price_vs_pool(bar["close"], d))
            entry["deepest"] = d
            entry["pool_below_deepest"] = snap.pool_below_deepest
            entry["hours_since_touch"] = None
        else:
            entry["deepest"] = None
        rules = rule_states(
            bar["close"],
            snap,
            sticky_until=sticky24_until,
            bounce_block_until=bounce_until,
        )
        entry["block_R1_3pct"] = rules.get("R1_top_within_3.0pct", False)
        entry["block_R2_inside"] = rules.get("R2_inside_zone", False)
        entry["block_sticky_24h"] = rules.get("R3_sticky", False)
        entry["block_bounce_36h"] = rules.get("R4_bounce_sticky", False)
        if ct < FOCUS_FROM or ct > FOCUS_TO:
            continue
        entry["active_4h_lowers"] = [r for r in snap.lowers if r["status"] == "active"]
        entry["all_4h_lowers"] = snap.lowers
        if touch_low is not None and last_touch is not None:
            entry["hours_since_touch"] = (ct - last_touch).total_seconds() / 3600.0
        chronicle.append(entry)

    touch_tl = build_touch_timeline(bars, snap_by_close)

    # Rule grid on all june/july signals
    r1_keys = [f"R1_top_within_{p}pct" for p in (0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)]
    rule_names = r1_keys + ["R2_inside_zone", "R2_below_bottom", "R3_sticky_24h", "R3_sticky_48h", "R4_bounce_36h", "R4_reclaim_above_top_3pct"]

    grid: dict[str, dict] = {
        n: {"blocked_bad": 0, "blocked_good": 0, "blocked_unresolved": 0, "missed_block_bad": 0}
        for n in rule_names
    }

    floor_maps = {h: build_floor_episode_by_close(bars, h) for h in FLOOR_EPISODE_HOURS_GRID}

    signal_eval = []
    for sig in june_july:
        ot = _utc(datetime.fromisoformat(sig["entry_open"]))
        bar_match = next((b for b in bars if b["open_time"] == ot), None)
        close_t = bar_match["close_time"] if bar_match else ot + timedelta(minutes=15)
        idx = bar_index.get(close_t) if bar_match else None
        snap = h4_snapshot("XRPUSDT", close_t)
        close = float(sig["close"])
        tp = sig["_tp"] or close * 0.99
        outcome = forward_outcome(bars, idx, close, sig["_stop"], tp) if idx is not None else {}
        sticky24 = sticky_24.get(close_t)
        sticky48 = sticky_48.get(close_t)
        bounce_u = bounce_sticky.get(close_t)
        rules = rule_states(close, snap, sticky_until=sticky48, bounce_block_until=bounce_u)
        rules["R3_sticky_24h"] = sticky24 is not None and close_t < sticky24
        rules["R3_sticky_48h"] = sticky48 is not None and close_t < sticky48
        rules["R4_bounce_36h"] = bounce_u is not None and close_t < bounce_u
        if snap.deepest_active:
            pv = price_vs_pool(close, snap.deepest_active)
            rules["R4_reclaim_above_top_3pct"] = (
                not snap.pool_below_deepest and pv["position"] == "above_top" and pv["dist_to_top_pct"] < 3.0
            )
        else:
            rules["R4_reclaim_above_top_3pct"] = False

        blocked_by = [n for n in rule_names if rules.get(n)]
        floor_state = floor_maps[FLOOR_EPISODE_HOURS_DEFAULT].get(close_t)
        floor_active = floor_episode_blocks(close, floor_state, FLOOR_EPISODE_PCT_DEFAULT)
        r1_r2_block = blocked_by_r1_r2(rules)
        rec = {
            "signal_num": sig["_signal_num"],
            "entry_open": sig["entry_open"],
            "decision_close": close_t.isoformat(),
            "close": close,
            "result_status": outcome.get("result_status"),
            "mfe_pct": outcome.get("mfe_pct"),
            "mae_pct": outcome.get("mae_pct"),
            "mae_before_mfe": outcome.get("mae_before_mfe"),
            "mfe_ge_0_41_pct": outcome.get("mfe_ge_0_41_pct"),
            "outcome": outcome.get("outcome"),
            "deepest_4h": snap.deepest_active,
            "pool_below": snap.pool_below_deepest,
            "price_vs_deepest": price_vs_pool(close, snap.deepest_active) if snap.deepest_active else None,
            "blocked_by": blocked_by,
            "blocked_by_r1_r2": r1_r2_block,
            "blocked_by_floor_episode": floor_active,
            "floor_episode_active": floor_active,
            "floor_episode_hours": FLOOR_EPISODE_HOURS_DEFAULT,
            "floor_episode_max_dist_pct": FLOOR_EPISODE_PCT_DEFAULT,
            "episode_start": floor_state.get("episode_start") if floor_state else None,
            "session_floor_top": floor_state.get("session_floor_top") if floor_state else None,
            "first_touch_time": floor_state.get("first_touch_time") if floor_state else None,
            "hours_since_first_touch": floor_state.get("hours_since_first_touch") if floor_state else None,
            "dist_to_session_floor_top_pct": floor_state.get("dist_to_session_floor_top_pct") if floor_state else None,
        }
        signal_eval.append(rec)
        q = outcome.get("result_status", "unresolved")
        for n in rule_names:
            if rules.get(n):
                grid[n][f"blocked_{q}"] = grid[n].get(f"blocked_{q}", 0) + 1
            elif q == "bad":
                grid[n]["missed_block_bad"] += 1

    floor_sensitivity: list[dict] = []
    for hours in FLOOR_EPISODE_HOURS_GRID:
        for pct in FLOOR_EPISODE_PCT_GRID:
            block_problem = 0
            block_good = 0
            block_mfe041 = 0
            for rec in signal_eval:
                ct = _utc(datetime.fromisoformat(rec["decision_close"]))
                st = floor_maps[hours].get(ct)
                blocked = floor_episode_blocks(rec["close"], st, pct)
                if not blocked:
                    continue
                if rec["signal_num"] in PROBLEM_SIGNAL_NUMS:
                    block_problem += 1
                if rec["result_status"] == "good":
                    block_good += 1
                if rec.get("mfe_ge_0_41_pct"):
                    block_mfe041 += 1
            floor_sensitivity.append(
                {
                    "episode_hours": hours,
                    "max_dist_to_session_floor_top_pct": pct,
                    "problem_signals_blocked": block_problem,
                    "problem_signals_total": len(PROBLEM_SIGNAL_NUMS),
                    "good_signals_blocked": block_good,
                    "mfe_ge_0_41_pct_blocked": block_mfe041,
                    "signals_total": len(signal_eval),
                }
            )

    problem_blocked = [r for r in signal_eval if r["signal_num"] in PROBLEM_SIGNAL_NUMS and r["blocked_by_floor_episode"]]
    false_block_good = [
        r for r in signal_eval if r["blocked_by_floor_episode"] and r["result_status"] == "good"
    ]

    # activation timeline for key rules
    activation: dict[str, dict] = {}
    for name in ("R1_top_within_3.0pct", "R2_inside_zone", "R3_sticky_24h", "R4_bounce_36h", "R4_reclaim_above_top_3pct"):
        on = None
        resets = []
        prev = False
        for bar in bars:
            ct = bar["close_time"]
            if ct < FOCUS_FROM or ct > FOCUS_TO + timedelta(days=7):
                continue
            snap = h4_snapshot("XRPUSDT", ct)
            rules = rule_states(
                bar["close"],
                snap,
                sticky_until=sticky_24.get(ct),
                bounce_block_until=bounce_sticky.get(ct),
            )
            if name == "R4_reclaim_above_top_3pct":
                if snap.deepest_active:
                    pv = price_vs_pool(bar["close"], snap.deepest_active)
                    val = not snap.pool_below_deepest and pv["position"] == "above_top" and pv["dist_to_top_pct"] < 3.0
                else:
                    val = False
            elif name == "R3_sticky_24h":
                su = sticky_24.get(ct)
                val = su is not None and ct < su
            elif name == "R4_bounce_36h":
                bu = bounce_sticky.get(ct)
                val = bu is not None and ct < bu
            else:
                val = rules.get(name, False)
            if val and not prev:
                if on is None:
                    on = ct.isoformat()
            if prev and not val:
                resets.append(ct.isoformat())
            prev = val
        activation[name] = {"first_on": on, "resets": resets[:20]}

    problem = [r for r in signal_eval if r["signal_num"] in PROBLEM_SIGNAL_NUMS]

    report = {
        "method": {
            "4h_pane": "prefix to last closed 4h at 15m decision; compute_lld_selected; tip=last prefix bar",
            "pools": "UI selection; known/break at decision_time",
            "signals": str(CAUSAL_CSV),
            "outcome_horizon": f"{HORIZON_BARS} x 15m bars; stop/tp from CSV else horizon",
        },
        "focus_window": [FOCUS_FROM.isoformat(), FOCUS_TO.isoformat()],
        "touch_timeline": touch_tl,
        "activation_focus_plus_week": activation,
        "chronicle_sample": chronicle,
        "floor_episode_default": {
            "episode_hours": FLOOR_EPISODE_HOURS_DEFAULT,
            "max_dist_to_session_floor_top_pct": FLOOR_EPISODE_PCT_DEFAULT,
            "no_reset_on_deeper_4h_pool": True,
        },
        "floor_episode_sensitivity": floor_sensitivity,
        "problem_signals_4_7_floor_blocked": problem_blocked,
        "floor_episode_false_blocks_good": false_block_good,
        "problem_signals_4_7": problem,
        "rule_grid_june_july": grid,
        "all_signals_june_july": signal_eval,
    }
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")

    lines = [
        "# 4h-Lower Short-Block Analyse (XRPUSDT)",
        "",
        "## Problem-Signale 4–7 (07.06. 01:30–03:15)",
        "",
    ]
    lines.extend(
        [
            "",
            f"Floor-Episode Default: **{FLOOR_EPISODE_HOURS_DEFAULT}h** / **{FLOOR_EPISODE_PCT_DEFAULT}%** "
            f"zu `session_floor_top` (kein Reset bei tieferem 4h-Pool).",
            "",
            "### Problematische Shorts (4–7), durch Floor-Episode blockiert",
            "",
        ]
    )
    for p in problem_blocked:
        lines.append(
            f"- **Signal {p['signal_num']}** {p['entry_open']} close={p['close']} "
            f"session_floor_top={p.get('session_floor_top')} dist={p.get('dist_to_session_floor_top_pct')}% "
            f"since_touch_h={p.get('hours_since_first_touch')} "
            f"R1/R2={p.get('blocked_by_r1_r2')} episode={p.get('blocked_by_floor_episode')} "
            f"status={p.get('result_status')} MFE={p.get('mfe_pct')}% MAE={p.get('mae_pct')}% "
            f"mae_before_mfe={p.get('mae_before_mfe')}"
        )
    lines.extend(["", "### Fälschlich blockierte gute Shorts (Floor-Episode Default)", ""])
    for p in false_block_good:
        lines.append(
            f"- **Signal {p['signal_num']}** {p['entry_open']} close={p['close']} "
            f"session_floor_top={p.get('session_floor_top')} dist={p.get('dist_to_session_floor_top_pct')}% "
            f"MFE={p.get('mfe_pct')}% MAE={p.get('mae_pct')}% mfe_ge_0.41={p.get('mfe_ge_0_41_pct')}"
        )
    lines.extend(
        [
            "",
            "## Floor-Episode Sensitivität (Juni/Juli, 34 Signale)",
            "",
            "| hours | max_dist_% | problem blocked | good blocked | MFE≥0.41% blocked |",
            "|-------|------------|-----------------|--------------|-------------------|",
        ]
    )
    for row in floor_sensitivity:
        lines.append(
            f"| {row['episode_hours']} | {row['max_dist_to_session_floor_top_pct']} | "
            f"{row['problem_signals_blocked']}/{row['problem_signals_total']} | "
            f"{row['good_signals_blocked']} | {row['mfe_ge_0_41_pct_blocked']} |"
        )
    lines.extend(["", "## Regel-Aktivierung (05.06.–15.06.)", ""])
    for k, v in activation.items():
        lines.append(f"- **{k}**: first_on={v['first_on']} resets={len(v['resets'])}")
    lines.extend(["", "## Schwellen-Raster Juni/Juli (XRP)", ""])
    lines.append("| rule | block good | block bad | block unresolved | missed bad |")
    lines.append("|------|------------|-----------|------------------|------------|")
    for n in rule_names:
        g = grid[n]
        lines.append(
            f"| {n} | {g.get('blocked_good',0)} | {g.get('blocked_bad',0)} | "
            f"{g.get('blocked_unresolved',0)} | {g['missed_block_bad']} |"
        )
    OUT_MD.write_text("\n".join(lines) + "\n")
    print("wrote", OUT_JSON, OUT_MD)
    print("problem blocks:")
    for p in problem:
        print(p["signal_num"], p["blocked_by"], p["price_vs_deepest"])


if __name__ == "__main__":
    main()
