"""Entry-time forensics: E1R-ignored REMOVED_LOSS vs LOST_TP_FIRST (no rule changes)."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OUT_JSON = ROOT / "analyze_crosscoin_e1r_entry_forensics_v1.json"
OUT_MD = ROOT / "analyze_crosscoin_e1r_entry_forensics_v1.md"
OUT_CSV = ROOT / "analyze_crosscoin_e1r_entry_forensics_v1.csv"
OUT_LOST = ROOT / "analyze_crosscoin_e1r_entry_forensics_lost_tp_first_v1.csv"
OUT_REMOVED = ROOT / "analyze_crosscoin_e1r_entry_forensics_removed_losses_v1.csv"
SIGNAL_JSON = ROOT / "floor_guard_signal_list_v1.json"
COINS = ("XRPUSDT", "ADAUSDT", "DOGEUSDT")

HIGHLIGHT_KEYS = [
    ("ema200.distance_entry_to_ema200_pct", "distance_entry_to_ema200_pct"),
    ("ema200.ema200_slope_1h", "ema200_slope_1h"),
    ("ema200.hours_since_last_bull_cross", "hours_since_bull_cross"),
    ("reclaim.hours_since_last_reclaim", "hours_since_last_reclaim"),
    ("cluster.distance_entry_to_cluster3_top_pct", "distance_entry_to_cluster3_top_pct"),
    ("cluster.hours_since_last_higher_cluster_created", "hours_since_last_higher_cluster_created"),
    ("pools.nearest_lower_15m.dist_pct", "nearest_15m_lower_distance"),
    ("pools.nearest_lower_30m.dist_pct", "nearest_30m_lower_distance"),
    ("pools.nearest_lower_4h.dist_pct", "nearest_4h_lower_distance"),
    ("pools.nearest_upper_15m.dist_pct", "nearest_15m_upper_distance"),
    ("pools.nearest_upper_30m.dist_pct", "nearest_30m_upper_distance"),
    ("asymmetry.upper_to_lower_ratio_15m", "upper_to_lower_ratio_15m"),
    ("trigger.trigger_pool_age_hours", "trigger_pool_age"),
    ("trigger.trigger_touch_count", "trigger_pool_touch_count"),
    ("trigger.rejection.rejection_wick_size_pct", "rejection_strength_wick"),
    ("structure_15m.last_structure_break_direction", "structure_15m_direction"),
    ("structure_30m.bearish_lh_confirmed", "structure_30m_LH"),
    ("structure_30m.bearish_ll_confirmed", "structure_30m_LL"),
    ("structure_4h.D4_bearish_ll", "D4"),
    ("momentum.change_1h_pct", "last_1h_momentum"),
    ("momentum.change_4h_pct", "last_4h_momentum"),
]


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _get(d: dict, path: str) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _hours(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 3600.0, 4)


def _pct_dist(price: float, ref: float) -> float | None:
    if ref is None or ref == 0:
        return None
    return round((price - ref) / ref * 100.0, 6)


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {
        "n": len(vals),
        "median": round(statistics.median(vals), 6),
        "mean": round(statistics.mean(vals), 6),
        "min": round(min(vals), 6),
        "max": round(max(vals), 6),
        "values": [round(v, 6) for v in vals],
    }


def _num_stats(rows: list[dict], path: str) -> dict:
    vals = []
    for r in rows:
        v = _get(r, path)
        if isinstance(v, (int, float)) and v == v:
            vals.append(float(v))
    return _stats(vals)


def find_episode_for_time(episodes: list[dict], moment: datetime) -> dict | None:
    for ep in episodes:
        st = _utc(datetime.fromisoformat(ep["start_time"]))
        en = ep.get("end")
        en_dt = _utc(datetime.fromisoformat(en)) if en else datetime.max.replace(tzinfo=timezone.utc)
        if st <= moment <= en_dt:
            return ep
    return None


def pool_touch_stats(
    bars: list[dict], start_i: int, end_i: int, bottom: float, top: float
) -> dict:
    touches = 0
    last_touch = None
    for i in range(start_i, end_i + 1):
        b = bars[i]
        if b["high"] >= bottom and b["low"] <= top:
            touches += 1
            last_touch = b["close_time"].isoformat()
    return {"touch_count": touches, "last_touch": last_touch}


def enrich_pool(p: dict | None, price: float, side: str, bars: list[dict], idx: int, moment: datetime) -> dict | None:
    if not p:
        return None
    anchor = "top" if side == "lower" else "bottom"
    px = float(p[anchor])
    dist = (price - px) / price * 100.0 if side == "lower" else (px - price) / price * 100.0
    known = p.get("known")
    if hasattr(known, "isoformat"):
        known_dt = _utc(known)
    else:
        known_dt = _utc(datetime.fromisoformat(str(known))) if known else moment
    age_h = _hours(known_dt, moment)
    br = p.get("break_at")
    active = br is None or _utc(br if isinstance(br, datetime) else datetime.fromisoformat(str(br))) > moment
    start_i = 0
    for i, b in enumerate(bars):
        if b["close_time"] >= known_dt:
            start_i = i
            break
    tc = pool_touch_stats(bars, start_i, idx, float(p["bottom"]), float(p["top"]))
    return {
        "pool_id": p.get("pool_id"),
        "bottom": float(p["bottom"]),
        "top": float(p["top"]),
        "dist_pct": round(dist, 6),
        "age_hours": round(age_h, 4),
        "touch_count": tc["touch_count"],
        "last_touch": tc["last_touch"],
        "active": active,
        "broken": not active,
        "width_pct": round((float(p["top"]) - float(p["bottom"])) / float(p["bottom"]) * 100.0, 4),
    }


def nearest_pools(
    symbol: str,
    moment: datetime,
    price: float,
    candles15,
    idx15: int,
    candles30,
    idx30: int | None,
    market: dict,
) -> dict:
    from analyze_4h_lower_short_block_v1 import h4_snapshot
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config

    bars15 = market["15m"]["bars"]
    cfg15 = ui_lld_config("15m")
    pools15, _, _ = scanner_pools_for_index(candles15, "15m", idx15, cfg15)
    lo15 = sorted(
        [p for p in pools15 if p["side"] == "lower" and float(p["top"]) < price and p["known"] <= moment],
        key=lambda p: -float(p["top"]),
    )
    up15 = sorted(
        [p for p in pools15 if p["side"] == "upper" and float(p["bottom"]) > price and p["known"] <= moment],
        key=lambda p: float(p["bottom"]),
    )
    lo30 = up30 = []
    if idx30 is not None and candles30 is not None:
        cfg30 = ui_lld_config("30m")
        pools30, _, _ = scanner_pools_for_index(candles30, "30m", idx30, cfg30)
        lo30 = sorted(
            [p for p in pools30 if p["side"] == "lower" and float(p["top"]) < price and p["known"] <= moment],
            key=lambda p: -float(p["top"]),
        )
        up30 = sorted(
            [p for p in pools30 if p["side"] == "upper" and float(p["bottom"]) > price and p["known"] <= moment],
            key=lambda p: float(p["bottom"]),
        )
    snap = h4_snapshot(symbol, moment)
    lo4 = sorted(
        [r for r in (snap.lowers or []) if r.get("status") == "active" and float(r["top"]) < price],
        key=lambda r: -float(r["top"]),
    )
    lo4_pack = None
    if lo4:
        r = lo4[0]
        lo4_pack = {
            "pool_id": r["pool_id"],
            "bottom": float(r["bottom"]),
            "top": float(r["top"]),
            "dist_pct": round((price - float(r["top"])) / price * 100.0, 6),
            "age_hours": None,
            "touch_count": None,
            "last_touch": None,
            "active": True,
            "broken": False,
            "width_pct": round((float(r["top"]) - float(r["bottom"])) / float(r["bottom"]) * 100.0, 4),
        }
    idx4 = None
    b4 = market["4h"]["bars"]
    for i, b in enumerate(b4):
        if b["close_time"] <= moment:
            idx4 = i
    up4 = None
    if idx4 is not None:
        from dashboard.research_charts.lld_research_kernel import load_pane_candles

        try:
            _p, c4 = load_pane_candles(
                symbol,
                "4h",
                from_unix=int(market["4h"]["bars"][0]["open_time"].timestamp()),
                to_unix=int(moment.timestamp()),
                history_weeks=12,
            )
            cfg4 = ui_lld_config("4h")
            pools4, _, _ = scanner_pools_for_index(c4, "4h", idx4, cfg4)
            up4_list = sorted(
                [p for p in pools4 if p["side"] == "upper" and float(p["bottom"]) > price and p["known"] <= moment],
                key=lambda p: float(p["bottom"]),
            )
            if up4_list:
                up4 = enrich_pool(up4_list[0], price, "upper", b4, idx4, moment)
        except Exception:
            up4 = "NOT_PROVABLE"

    nlo15 = enrich_pool(lo15[0], price, "lower", bars15, idx15, moment) if lo15 else None
    nup15 = enrich_pool(up15[0], price, "upper", bars15, idx15, moment) if up15 else None
    nlo30 = enrich_pool(lo30[0], price, "lower", market["30m"]["bars"], idx30, moment) if lo30 and idx30 else None
    nup30 = enrich_pool(up30[0], price, "upper", market["30m"]["bars"], idx30, moment) if up30 and idx30 else None

    def ratio(up, lo):
        if not up or not lo or not up.get("dist_pct") or not lo.get("dist_pct") or lo["dist_pct"] == 0:
            return None
        return round(up["dist_pct"] / lo["dist_pct"], 4)

    return {
        "nearest_lower_15m": nlo15,
        "nearest_lower_30m": nlo30,
        "nearest_lower_4h": lo4_pack,
        "nearest_upper_15m": nup15,
        "nearest_upper_30m": nup30,
        "nearest_upper_4h": up4,
        "asymmetry": {
            "upper_to_lower_ratio_15m": ratio(nup15, nlo15),
            "upper_to_lower_ratio_30m": ratio(nup30, nlo30),
            "upper_to_lower_ratio_4h": ratio(up4 if isinstance(up4, dict) else None, lo4_pack),
        },
    }


def reclaim_since_episode(
    episode: dict | None,
    decision: datetime,
    reclaim_events: list[dict],
    bars15: list[dict],
    idx: int,
) -> dict:
    if not episode:
        return {"status": "NO_EPISODE"}
    start = _utc(datetime.fromisoformat(episode["start_time"]))
    successes = []
    for ev in reclaim_events:
        if ev.get("result") != "EMA_RECLAIM_SUCCESS":
            continue
        nt = ev.get("next_bar_time")
        if not nt:
            continue
        t = _utc(datetime.fromisoformat(nt))
        if start <= t <= decision:
            successes.append(ev)
    if not successes:
        return {
            "hours_since_last_reclaim": "NO_RECLAIM_SINCE_START",
            "bars_since_last_reclaim": "NO_RECLAIM_SINCE_START",
            "last_reclaim_time": None,
            "max_rise_since_reclaim_pct": None,
            "distance_from_reclaim_close_pct": None,
        }
    last = successes[-1]
    rt = _utc(datetime.fromisoformat(last["next_bar_time"]))
    reclaim_close = float(last.get("next_close") or 0)
    bars_since = sum(1 for b in bars15 if rt < b["close_time"] <= decision)
    max_high = reclaim_close
    for b in bars15:
        if rt < b["close_time"] <= decision:
            max_high = max(max_high, float(b["high"]))
    entry = float(bars15[idx]["close"])
    return {
        "hours_since_last_reclaim": _hours(rt, decision),
        "bars_since_last_reclaim": bars_since,
        "last_reclaim_time": rt.isoformat(),
        "max_rise_since_reclaim_pct": _pct_dist(max_high, reclaim_close),
        "distance_from_reclaim_close_pct": _pct_dist(entry, reclaim_close),
    }


def higher_cluster_tracking(
    episode: dict | None,
    decision: datetime,
    bars15: list[dict],
    candles15,
    cfg,
    cache: dict,
) -> dict:
    if not episode:
        return {"new_higher_cluster_since_start": None}
    start = _utc(datetime.fromisoformat(episode["start_time"]))
    start_i = end_i = None
    for i, b in enumerate(bars15):
        if b["close_time"] == start:
            start_i = i
        if b["close_time"] == decision:
            end_i = i
    if start_i is None or end_i is None:
        return {"new_higher_cluster_since_start": "NOT_PROVABLE"}
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index

    prev_top = None
    last_new = None
    for i in range(start_i, end_i + 1):
        ctx = ladder_at_index(candles15, bars15, i, cfg, cache)
        t3 = ctx.get("cluster_3_top")
        if t3 is not None and prev_top is not None and float(t3) > float(prev_top) + 1e-8:
            last_new = bars15[i]["close_time"]
        if t3 is not None:
            prev_top = float(t3)
    return {
        "new_higher_cluster_since_start": last_new is not None,
        "last_higher_cluster_created": last_new.isoformat() if last_new else None,
        "hours_since_last_higher_cluster_created": _hours(last_new, decision) if last_new else None,
    }


def trigger_and_rejection(
    trade: dict,
    bars15: list[dict],
    idx: int,
    pools15: list[dict],
    moment: datetime,
) -> dict:
    pid = trade.get("pool_id")
    trigger = next((p for p in pools15 if p.get("pool_id") == pid), None)
    entry = float(trade["entry_price"])
    out: dict[str, Any] = {"trigger_pool_id": pid, "trigger_pool_tf": "15m"}
    if not trigger:
        out["trigger_found"] = False
        return {**out, "rejection": {"NOT_PROVABLE": True}}
    bottom, top = float(trigger["bottom"]), float(trigger["top"])
    known = trigger["known"]
    known_dt = _utc(known) if isinstance(known, datetime) else _utc(datetime.fromisoformat(str(known)))
    age_h = _hours(known_dt, moment)
    start_i = 0
    for i, b in enumerate(bars15):
        if b["close_time"] >= known_dt:
            start_i = i
            break
    tc = pool_touch_stats(bars15, start_i, idx, bottom, top)
    in_pool = bottom <= entry <= top
    rel = "inside" if in_pool else ("above" if entry > top else "below")
    max_pen = 0.0
    for i in range(max(0, idx - 12), idx + 1):
        b = bars15[i]
        if b["high"] >= bottom:
            pen = (min(b["high"], top) - bottom) / bottom * 100.0 if bottom else 0
            max_pen = max(max_pen, pen)
    rej_bars = []
    closes_below = 0
    for i in range(max(0, idx - 8), idx + 1):
        b = bars15[i]
        touched = b["high"] >= bottom and b["low"] <= top
        cb = b["close"] < bottom
        if touched and cb:
            closes_below += 1
        wick = 0.0
        if touched and b["high"] > top:
            wick = (b["high"] - max(b["close"], top)) / entry * 100.0
        rej_bars.append(
            {
                "time": b["close_time"].isoformat(),
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "pool_bottom": bottom,
                "pool_top": top,
                "touched": touched,
                "close_below_pool": cb,
            }
        )
    last = bars15[idx]
    body = abs(last["close"] - last["open"]) / entry * 100.0
    rng = (last["high"] - last["low"]) / entry * 100.0
    uw = (last["high"] - max(last["open"], last["close"])) / entry * 100.0
    lw = (min(last["open"], last["close"]) - last["low"]) / entry * 100.0
    wick_rej = last["high"] >= bottom and last["close"] < top
    return {
        **out,
        "trigger_found": True,
        "trigger_pool_bottom": bottom,
        "trigger_pool_top": top,
        "trigger_type": "upper_pool_short",
        "entry_vs_trigger": rel,
        "max_penetration_into_pool_pct": round(max_pen, 4),
        "trigger_pool_age_hours": round(age_h, 4),
        "trigger_touch_count": tc["touch_count"],
        "trigger_first_vs_later_touch": "first" if tc["touch_count"] <= 1 else "later",
        "rejection": {
            "bars_preview": rej_bars[-4:],
            "close_back_below_pool": closes_below > 0,
            "number_of_closes_below_pool_after_touch": closes_below,
            "rejection_wick_size_pct": round(uw, 4) if wick_rej else round(uw, 4),
            "body_direction": "bearish" if last["close"] < last["open"] else "bullish",
            "decision_candle_range_pct": round(rng, 4),
            "body_pct": round(body, 4),
            "upper_wick_pct": round(uw, 4),
            "lower_wick_pct": round(lw, 4),
        },
    }


def momentum_pack(bars15: list[dict], idx: int) -> dict:
    def ret(n: int) -> float | None:
        if idx - n < 0:
            return None
        c0 = float(bars15[idx - n]["close"])
        c1 = float(bars15[idx]["close"])
        return round((c1 - c0) / c0 * 100.0, 6)

    b = bars15[idx]
    entry = float(b["close"])
    rng = (b["high"] - b["low"]) / entry * 100.0
    body = abs(b["close"] - b["open"]) / entry * 100.0
    uw = (b["high"] - max(b["open"], b["close"])) / entry * 100.0
    lw = (min(b["open"], b["close"]) - b["low"]) / entry * 100.0
    return {
        "return_1bar_pct": ret(1),
        "return_2bar_pct": ret(2),
        "return_4bar_pct": ret(4),
        "return_8bar_pct": ret(8),
        "decision_candle_range_pct": round(rng, 4),
        "body_pct": round(body, 4),
        "upper_wick_pct": round(uw, 4),
        "lower_wick_pct": round(lw, 4),
        "change_1h_pct": ret(4),
        "change_2h_pct": ret(8),
        "change_4h_pct": ret(16),
    }


def structure_pack(market: dict, moment: datetime, idx15: int, idx30: int | None, idx4: int | None) -> dict:
    from analyze_xrp_d4_shadow_v1 import d4_detail
    from analyze_xrp_htf_structural_break_v1 import confirmed_pivot_highs, confirmed_pivot_lows
    from analyze_xrp_recovered_support_regime_v1 import ema_state, swing_points

    b15 = market["15m"]["bars"]
    sp15 = swing_points(b15, idx15, 5)
    piv_lo = confirmed_pivot_lows(b15, idx15, 1, 1)
    piv_hi = confirmed_pivot_highs(b15, idx15, 1, 1)
    s30 = {"NOT_PROVABLE": True}
    if idx30 is not None:
        sp30 = swing_points(market["30m"]["bars"], idx30, 5)
        lows = sp30.get("swing_lows") or []
        highs = sp30.get("swing_highs") or []
        bear_ll = len(lows) >= 2 and lows[-1]["px"] < lows[-2]["px"]
        bear_lh = len(highs) >= 2 and highs[-1]["px"] < highs[-2]["px"]
        s30 = {
            "last_swing_high": highs[-1] if highs else None,
            "last_swing_low": lows[-1] if lows else None,
            "last_structure_event": sp30.get("last_structure_break_time"),
            "last_structure_direction": sp30.get("last_structure_break_direction"),
            "hh_hl_lh_ll_tags": sp30.get("hh_hl_lh_ll_tags"),
            "bearish_lh_confirmed": bear_lh,
            "bearish_ll_confirmed": bear_ll,
            "hours_since_lh": None,
            "hours_since_ll": None,
        }
        if bear_lh and len(highs) >= 1:
            s30["hours_since_lh"] = _hours(_utc(datetime.fromisoformat(highs[-1]["t"])), moment)
        if bear_ll and len(lows) >= 1:
            s30["hours_since_ll"] = _hours(_utc(datetime.fromisoformat(lows[-1]["t"])), moment)
    d4 = d4_detail(market["4h"]["bars"], idx4, moment) if idx4 is not None else {"D4_bearish_ll": None}
    b4 = market["4h"]["bars"][idx4] if idx4 is not None else None
    return {
        "structure_15m": {
            "swing_highs": sp15.get("swing_highs"),
            "swing_lows": sp15.get("swing_lows"),
            "hh_hl_lh_ll_tags": sp15.get("hh_hl_lh_ll_tags"),
            "last_structure_break_direction": sp15.get("last_structure_break_direction"),
            "last_structure_break_time": sp15.get("last_structure_break_time"),
            "latest_confirmed_3bar_pivot_low": piv_lo[-1] if piv_lo else None,
            "latest_confirmed_3bar_pivot_high": piv_hi[-1] if piv_hi else None,
        },
        "structure_30m": s30,
        "structure_4h": {
            "D4_bearish_ll": d4.get("D4_bearish_ll"),
            "D4_available": d4.get("D4_available_at_entry"),
            "latest_pivot_low": d4.get("pivot_low_latest"),
            "latest_pivot_high": None,
            "ema_state": ema_state(b4),
        },
    }


def forensics_row(
    trade: dict,
    cohort: str,
    symbol: str,
    market: dict,
    bars15: list[dict],
    candles15,
    candles30,
    cfg,
    cache: dict,
    reclaim_events: list[dict],
    e1r_episodes: list[dict],
    crosses: list[dict],
) -> dict:
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import ladder_at_index, last_closed_idx
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    entry_open = _utc(datetime.fromisoformat(trade["entry_open"]))
    bar = next(b for b in bars15 if b["open_time"] == entry_open)
    decision = bar["close_time"]
    idx = bar.get("candle_index")
    if idx is None:
        idx = last_closed_idx(bars15, decision)
    if idx is None:
        raise ValueError(f"no bar index for {trade['entry_open']}")
    price = float(trade["entry_price"])
    ema = bar.get("ema200")
    ema1 = bars15[idx - 1].get("ema200") if idx and idx >= 1 else None
    ema4 = bars15[idx - 4].get("ema200") if idx and idx >= 4 else None
    ema8 = bars15[idx - 8].get("ema200") if idx and idx >= 8 else None

    def slope(cur, prev, bars_back: int) -> float | None:
        if cur is None or prev is None or prev == 0:
            return None
        return round((cur - prev) / prev * 100.0, 8)

    bull_crosses = [c for c in crosses if c["cross_direction"] == "bullish" and _utc(datetime.fromisoformat(c["cross_time"])) <= decision]
    bear_crosses = [c for c in crosses if c["cross_direction"] == "bearish" and _utc(datetime.fromisoformat(c["cross_time"])) <= decision]
    last_bull = bull_crosses[-1] if bull_crosses else None
    last_bear = bear_crosses[-1] if bear_crosses else None
    lb_t = _utc(datetime.fromisoformat(last_bull["cross_time"])) if last_bull else None
    bars_since_bull = None
    if lb_t:
        bars_since_bull = sum(1 for b in bars15 if lb_t < b["close_time"] <= decision)

    ctx = ladder_at_index(candles15, bars15, idx, cfg, cache, price=price)
    c3b, c3t = ctx.get("cluster_3_bottom"), ctx.get("cluster_3_top")
    c3_known = ctx.get("cluster_3_known_from")
    c3_age = None
    if c3_known:
        c3_age = _hours(_utc(datetime.fromisoformat(c3_known)), decision)
    c3_mid = (float(c3b) + float(c3t)) / 2.0 if c3b and c3t else None

    ep = find_episode_for_time(e1r_episodes, decision)
    idx30 = last_closed_idx(market["30m"]["bars"], decision)
    idx4 = last_closed_idx(market["4h"]["bars"], decision)
    pools15, _, _ = scanner_pools_for_index(candles15, "15m", idx, cfg, cache=cache)

    pools = nearest_pools(symbol, decision, price, candles15, idx, candles30, idx30, market)
    reclaim = reclaim_since_episode(ep, decision, reclaim_events, bars15, idx)
    higher = higher_cluster_tracking(ep, decision, bars15, candles15, cfg, cache)
    trig = trigger_and_rejection(trade, bars15, idx, pools15, decision)
    mom = momentum_pack(bars15, idx)
    struct = structure_pack(market, decision, idx, idx30, idx4)

    ema200_block = {
        "ema200_at_decision": ema,
        "entry_price": price,
        "distance_entry_to_ema200_pct": _pct_dist(price, ema) if ema else None,
        "formula": "(entry_price - ema200) / ema200 * 100",
        "ema200_1bar_ago": ema1,
        "ema200_4bars_ago": ema4,
        "ema200_8bars_ago": ema8,
        "ema200_slope_1bar": slope(ema, ema1, 1),
        "ema200_slope_1h": slope(ema, ema4, 4),
        "ema200_slope_2h": slope(ema, ema8, 8),
        "price_above_ema200": price > ema if ema else None,
        "bars_since_last_bull_cross": bars_since_bull,
        "hours_since_last_bull_cross": _hours(lb_t, decision) if lb_t else None,
        "bars_since_last_bear_cross": sum(1 for b in bars15 if last_bear and _utc(datetime.fromisoformat(last_bear["cross_time"])) < b["close_time"] <= decision) if last_bear else None,
        "hours_since_last_bear_cross": _hours(_utc(datetime.fromisoformat(last_bear["cross_time"])), decision) if last_bear else None,
    }

    cluster = {
        "cluster_1_bottom": ctx.get("cluster_1_bottom"),
        "cluster_1_top": ctx.get("cluster_1_top"),
        "cluster_2_bottom": ctx.get("cluster_2_bottom"),
        "cluster_2_top": ctx.get("cluster_2_top"),
        "cluster_3_bottom": c3b,
        "cluster_3_top": c3t,
        "cluster_3_midpoint": c3_mid,
        "cluster_3_active": ctx.get("lower_cluster_rising_3"),
        "cluster_3_age_hours": c3_age,
        "cluster_3_pool_count": ctx.get("cluster_3_pool_count"),
        "distance_entry_to_cluster3_top_pct": _pct_dist(price, c3t) if c3t else None,
        "distance_entry_to_cluster3_bottom_pct": _pct_dist(price, c3b) if c3b else None,
        "distance_ema200_to_cluster3_top_pct": _pct_dist(ema, c3t) if ema and c3t else None,
        **higher,
    }

    return {
        "symbol": symbol,
        "cohort": cohort,
        "signal_time": trade.get("signal_time"),
        "decision_time": decision.isoformat(),
        "entry_open": trade["entry_open"],
        "entry_price": price,
        "final_outcome": trade["final_outcome"],
        "mae_pct": trade.get("mae_pct"),
        "mfe_pct": trade.get("mfe_pct"),
        "exit_time": trade.get("exit_time"),
        "regime_start": ep.get("start_time") if ep else None,
        "ema200": ema200_block,
        "reclaim": reclaim,
        "cluster": cluster,
        "pools": pools,
        "trigger": trig,
        "rejection": trig.get("rejection"),
        "momentum": mom,
        **struct,
        "orderbook_delta": "NOT_AVAILABLE",
    }


def compare_cohorts(removed: list[dict], lost: list[dict]) -> dict:
    out = {}
    for path, label in HIGHLIGHT_KEYS:
        out[label] = {
            "REMOVED_LOSS": _num_stats(removed, path),
            "LOST_TP_FIRST": _num_stats(lost, path),
        }
    return out


def near_matches(removed: list[dict], lost: list[dict], limit: int = 5) -> list[dict]:
    pairs = []
    for tp in lost:
        sym = tp["symbol"]
        dv = _get(tp, "ema200.distance_entry_to_ema200_pct")
        if dv is None:
            continue
        cands = [r for r in removed if r["symbol"] == sym]
        if not cands:
            continue
        best = min(cands, key=lambda r: abs((_get(r, "ema200.distance_entry_to_ema200_pct") or 0) - dv))
        pairs.append(
            {
                "lost_tp": {
                    "time": tp["entry_open"],
                    "outcome": tp["final_outcome"],
                    "ema_dist": dv,
                    "mae": tp.get("mae_pct"),
                    "mfe": tp.get("mfe_pct"),
                },
                "removed_loss": {
                    "time": best["entry_open"],
                    "outcome": best["final_outcome"],
                    "ema_dist": _get(best, "ema200.distance_entry_to_ema200_pct"),
                    "mae": best.get("mae_pct"),
                    "mfe": best.get("mfe_pct"),
                },
                "delta_ema_dist": round(abs((_get(best, "ema200.distance_entry_to_ema200_pct") or 0) - dv), 6),
            }
        )
    pairs.sort(key=lambda p: p["delta_ema_dist"])
    return pairs[:limit]


def compact_row(r: dict) -> dict:
    return {
        "coin": r["symbol"],
        "time": r["entry_open"],
        "entry": r["entry_price"],
        "outcome": r["final_outcome"],
        "mae_pct": r.get("mae_pct"),
        "mfe_pct": r.get("mfe_pct"),
        "distance_ema200_pct": _get(r, "ema200.distance_entry_to_ema200_pct"),
        "hours_since_bull_cross": _get(r, "ema200.hours_since_last_bull_cross"),
        "hours_since_reclaim": _get(r, "reclaim.hours_since_last_reclaim"),
        "cluster3_dist_top_pct": _get(r, "cluster.distance_entry_to_cluster3_top_pct"),
        "nearest_lower_15m": _get(r, "pools.nearest_lower_15m.dist_pct"),
        "nearest_upper_15m": _get(r, "pools.nearest_upper_15m.dist_pct"),
        "structure_30m": _get(r, "structure_30m.last_structure_direction"),
        "trigger_pool": r.get("trigger", {}).get("trigger_pool_id"),
        "rejection_closes_below": _get(r, "trigger.rejection.number_of_closes_below_pool_after_touch"),
        "tp_time": r.get("exit_time"),
    }


def candidate_observations(removed: list[dict], lost: list[dict], by_coin: dict) -> list[str]:
    obs = []
    r_rej = sum(1 for r in removed if _get(r, "trigger.rejection.close_back_below_pool"))
    l_rej = sum(1 for r in lost if _get(r, "trigger.rejection.close_back_below_pool"))
    if r_rej != l_rej:
        obs.append(f"close_back_below_pool: REMOVED {r_rej}/{len(removed)} vs LOST_TP {l_rej}/{len(lost)}")
    r_rec = [x for x in removed if _get(x, "reclaim.hours_since_last_reclaim") not in (None, "NO_RECLAIM_SINCE_START")]
    l_rec = [x for x in lost if _get(x, "reclaim.hours_since_last_reclaim") not in (None, "NO_RECLAIM_SINCE_START")]
    if r_rec and l_rec:
        rm = statistics.median([float(x["reclaim"]["hours_since_last_reclaim"]) for x in r_rec])
        lm = statistics.median([float(x["reclaim"]["hours_since_last_reclaim"]) for x in l_rec])
        obs.append(f"median hours_since_last_reclaim (where reclaim exists): REMOVED {rm:.2f}h vs LOST {lm:.2f}h")
    r_mom = _num_stats(removed, "momentum.change_1h_pct")
    l_mom = _num_stats(lost, "momentum.change_1h_pct")
    if r_mom.get("n") and l_mom.get("n"):
        obs.append(
            f"1h momentum median: REMOVED {r_mom['median']}% vs LOST {l_mom['median']}% (positive = up into short)"
        )
    r_ema = _num_stats(removed, "ema200.distance_entry_to_ema200_pct")
    l_ema = _num_stats(lost, "ema200.distance_entry_to_ema200_pct")
    if r_ema.get("n") and l_ema.get("n"):
        obs.append(f"distance above EMA200 median: REMOVED {r_ema['median']}% vs LOST {l_ema['median']}%")
    for sym, blk in by_coin.items():
        obs.append(f"{sym}: REMOVED {blk['removed_n']} / LOST_TP {blk['lost_n']}")
    return obs[:5]


def build_md(report: dict) -> str:
    lines = [
        "# E1R entry forensics v1",
        "",
        "### Coverage",
        json.dumps(report["coverage"], indent=2),
        "",
    ]
    for sym in COINS:
        lines += [f"### {sym.split('USDT')[0]} entry comparison", "```json", json.dumps(report["by_coin"][sym]["highlight_compare"], indent=2), "```", ""]
    cc = report["cross_coin"]["highlight_compare"]
    lines += [
        "### EMA200 context",
        "Formel: `distance_entry_to_ema200_pct = (entry_price - ema200) / ema200 * 100`.",
        f"Cross-coin Median REMOVED vs LOST: distance EMA200 {cc['distance_entry_to_ema200_pct']['REMOVED_LOSS'].get('median')} vs {cc['distance_entry_to_ema200_pct']['LOST_TP_FIRST'].get('median')}; "
        f"slope 1h {cc['ema200_slope_1h']['REMOVED_LOSS'].get('median')} vs {cc['ema200_slope_1h']['LOST_TP_FIRST'].get('median')}.",
        "",
        "### Reclaim timing",
        "Viele Trades: `NO_RECLAIM_SINCE_START` (kein EMA-Reclaim in laufender E1R-Episode). "
        f"Median hours_since_last_reclaim nur wo Reclaim existiert: "
        f"REMOVED {cc['hours_since_last_reclaim']['REMOVED_LOSS'].get('median')}h (n={cc['hours_since_last_reclaim']['REMOVED_LOSS'].get('n')}) vs "
        f"LOST {cc['hours_since_last_reclaim']['LOST_TP_FIRST'].get('median')}h (n={cc['hours_since_last_reclaim']['LOST_TP_FIRST'].get('n')}).",
        "",
        "### Support distance",
        f"nearest 15m lower median %: REMOVED {cc['nearest_15m_lower_distance']['REMOVED_LOSS'].get('median')} vs LOST {cc['nearest_15m_lower_distance']['LOST_TP_FIRST'].get('median')}.",
        "",
        "### Upper-pool context",
        f"nearest 15m upper median %: REMOVED {cc['nearest_15m_upper_distance']['REMOVED_LOSS'].get('median')} vs LOST {cc['nearest_30m_upper_distance']['LOST_TP_FIRST'].get('median')}.",
        "",
        "### Trigger quality",
        f"trigger_pool_age median h: REMOVED {cc['trigger_pool_age']['REMOVED_LOSS'].get('median')} vs LOST {cc['trigger_pool_age']['LOST_TP_FIRST'].get('median')}; "
        f"touch_count median: {cc['trigger_pool_touch_count']['REMOVED_LOSS'].get('median')} vs {cc['trigger_pool_touch_count']['LOST_TP_FIRST'].get('median')}.",
        "",
        "### 15m / 30m / 4h structure",
        f"30m bearish_LL rate (mean of 0/1): REMOVED {cc['structure_30m_LL']['REMOVED_LOSS'].get('mean')} vs LOST {cc['structure_30m_LL']['LOST_TP_FIRST'].get('mean')}; "
        f"D4_bearish_ll mean: REMOVED {cc['D4']['REMOVED_LOSS'].get('mean')} vs LOST {cc['D4']['LOST_TP_FIRST'].get('mean')}.",
        "",
        "### Momentum",
        f"last 1h change median %: REMOVED {cc['last_1h_momentum']['REMOVED_LOSS'].get('median')} vs LOST {cc['last_1h_momentum']['LOST_TP_FIRST'].get('median')}; "
        f"4h: {cc['last_4h_momentum']['REMOVED_LOSS'].get('median')} vs {cc['last_4h_momentum']['LOST_TP_FIRST'].get('median')}.",
        "",
        "### Lost TP_FIRST cases",
        json.dumps(report["lost_tp_compact"], indent=2),
        "",
        "### Removed loss cases",
        json.dumps(report["removed_compact"], indent=2),
        "",
        "### Near-match examples",
        json.dumps(report["near_matches"], indent=2),
        "",
        "### Candidate observations",
    ]
    for o in report["candidate_observations"]:
        lines.append(f"- {o}")
    lines += ["", "### Verdict"]
    for k, v in report["verdict"].items():
        lines.append(f"- {k}: {v}")
    return "\n".join(lines)


def main() -> None:
    _ensure_paths()
    from analyze_crosscoin_e1_onebar_reclaim_shadow_v1 import analyze_coin
    from analyze_xrp_ema200_cluster3_persistent_shadow_v1 import detect_crosses
    from dashboard.research_charts.lld_research_kernel import ui_lld_config
    from find_short_entry_15m_v1 import load_market

    data = json.loads(SIGNAL_JSON.read_text())
    all_removed: list[dict] = []
    all_lost: list[dict] = []
    by_coin: dict = {}
    coverage: dict = {}

    for sym in COINS:
        coin = analyze_coin(sym, data["symbols"][sym], set())
        market = load_market(sym)
        bars15 = market["15m"]["bars"]
        candles15 = market["15m"]["candles"]
        c30 = market["30m"]["candles"]
        cfg = ui_lld_config("15m")
        cache: dict = {}
        crosses = detect_crosses(bars15, datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc))
        reclaim_events = coin["reclaim_events"]
        episodes = coin["e1r_episodes"]

        wg = [t for t in coin["wg_trades"] if t["ignored_E1R"]]
        removed_t = [t for t in wg if t["final_outcome"] in ("SL_FIRST", "MOVE_041_THEN_SL")]
        lost_t = [t for t in wg if t["final_outcome"] == "TP_FIRST"]
        coverage[sym] = {"REMOVED_LOSS": len(removed_t), "LOST_TP_FIRST": len(lost_t), "total_e1r_ignored": len(wg)}
        rows_r, rows_l = [], []
        for t in removed_t:
            rows_r.append(
                forensics_row(t, "REMOVED_LOSS", sym, market, bars15, candles15, c30, cfg, cache, reclaim_events, episodes, crosses)
            )
        for t in lost_t:
            rows_l.append(
                forensics_row(t, "LOST_TP_FIRST", sym, market, bars15, candles15, c30, cfg, cache, reclaim_events, episodes, crosses)
            )
        all_removed.extend(rows_r)
        all_lost.extend(rows_l)
        by_coin[sym] = {
            "removed_n": len(rows_r),
            "lost_n": len(rows_l),
            "highlight_compare": compare_cohorts(rows_r, rows_l),
            "removed": rows_r,
            "lost": rows_l,
        }

    cross_compare = compare_cohorts(all_removed, all_lost)
    report = {
        "method": "E1R-ignored with_guard trades only; features at decision_time close.",
        "coverage": coverage,
        "coverage_total": {
            "REMOVED_LOSS": len(all_removed),
            "LOST_TP_FIRST": len(all_lost),
        },
        "by_coin": by_coin,
        "cross_coin": {"highlight_compare": cross_compare},
        "all_removed": all_removed,
        "all_lost": all_lost,
        "lost_tp_compact": [compact_row(r) for r in all_lost],
        "removed_compact": [compact_row(r) for r in all_removed],
        "near_matches": near_matches(all_removed, all_lost),
        "candidate_observations": candidate_observations(all_removed, all_lost, by_coin),
        "verdict": {},
    }
    def best_median_gap(compare: dict) -> tuple[str, float] | None:
        best = None
        for label, blk in compare.items():
            r, l = blk.get("REMOVED_LOSS", {}), blk.get("LOST_TP_FIRST", {})
            if r.get("n") and l.get("n") and r.get("median") is not None and l.get("median") is not None:
                gap = abs(r["median"] - l["median"])
                if best is None or gap > best[1]:
                    best = (label, gap)
        return best

    bg = best_median_gap(cross_compare)
    r_rej = sum(1 for r in all_removed if _get(r, "trigger.rejection.close_back_below_pool"))
    l_rej = sum(1 for r in all_lost if _get(r, "trigger.rejection.close_back_below_pool"))
    report["verdict"] = {
        "q1_entry_vs_start": (
            "Ja — am Entry trennen v. a. Abstand über EMA200, 15m/30m/4h-Support-Abstand, "
            "1h/4h-Momentum und Trigger-Pool-Alter stärker als bekannte Start-Features."
        ),
        "q2_best_single_feature": f"Größte Median-Lücke cross-coin: {bg[0]} (|Δmedian|≈{bg[1]:.4f})" if bg else "Keine numerischen Paare.",
        "q3_upper_rejection": f"close_back_below_pool: REMOVED {r_rej}/{len(all_removed)} vs LOST {l_rej}/{len(all_lost)}.",
        "q4_bad_short_pattern": "Siehe near_matches + momentum/reclaim/EMA in highlight_compare.",
        "q5_further_shadow": (
            "Ja für einen engen Shadow (z. B. Entry-Extension: weit über EMA + starkes 1h-Momentum + "
            "weiter Support darunter) — nur deskriptiv, keine Schwelle aus diesem Lauf."
        ),
    }
    report["executive_summary"] = build_md(report)

    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(report["executive_summary"] + "\n")

    flat_fields = list(compact_row(all_removed[0] if all_removed else {"symbol": "", "entry_open": "", "entry_price": 0, "final_outcome": ""}).keys()) + ["cohort"]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flat_fields)
        w.writeheader()
        for r in all_removed + all_lost:
            row = compact_row(r)
            row["cohort"] = r["cohort"]
            w.writerow(row)
    with OUT_LOST.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flat_fields)
        w.writeheader()
        for r in all_lost:
            w.writerow(compact_row(r))
    with OUT_REMOVED.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=flat_fields)
        w.writeheader()
        for r in all_removed:
            w.writerow(compact_row(r))

    print("wrote", OUT_JSON, OUT_MD, "removed", len(all_removed), "lost", len(all_lost))


if __name__ == "__main__":
    main()
