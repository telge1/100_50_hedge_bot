"""EMA200 transition shadow: skip first opposite signal after cross (analysis only)."""

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
OUT_JSON = ROOT / "analyze_xrp_ema200_transition_shadow_v1.json"
OUT_MD = ROOT / "analyze_xrp_ema200_transition_shadow_v1.md"
OUT_CSV = ROOT / "analyze_xrp_ema200_transition_shadow_trades_v1.csv"

SYMBOL = "XRPUSDT"
BAD_FIRST = "2026-06-11T19:15:00+00:00"
BAD_STREAK = {
    "2026-06-11T19:15:00+00:00",
    "2026-06-14T21:45:00+00:00",
    "2026-06-14T22:30:00+00:00",
    "2026-06-14T23:30:00+00:00",
}
MFE_THRESH = 0.41
PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
REPORT_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
REPORT_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _ensure_paths() -> None:
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _median(vals: list[float]) -> float | None:
    return round(statistics.median(vals), 4) if vals else None


def detect_ema200_crosses(bars: list[dict], timeframe: str) -> list[dict]:
    out = []
    for i in range(1, len(bars)):
        prev, cur = bars[i - 1], bars[i]
        e0, e1 = prev.get("ema200"), cur.get("ema200")
        if e0 is None or e1 is None:
            continue
        direction = None
        if prev["close"] <= e0 and cur["close"] > e1:
            direction = "bullish"
        elif prev["close"] >= e0 and cur["close"] < e1:
            direction = "bearish"
        if not direction:
            continue
        out.append(
            {
                "cross_id": f"{timeframe}:{cur['close_time'].isoformat()}:{direction}",
                "cross_time": cur["close_time"].isoformat(),
                "timeframe": timeframe,
                "close_before": prev["close"],
                "ema200_before": e0,
                "close_after": cur["close"],
                "ema200_after": e1,
                "direction": direction,
                "causal_known_at": cur["close_time"].isoformat(),
                "bar_index": i,
            }
        )
    return out


def mfe_before_sl(bars: list[dict], entry_idx: int, entry: float, stop: float, thresh: float) -> bool:
    from analyze_4h_lower_short_block_v1 import HORIZON_BARS

    target = entry * (1.0 - thresh / 100.0)
    end = min(len(bars), entry_idx + 1 + HORIZON_BARS)
    for i in range(entry_idx + 1, end):
        bar = bars[i]
        if bar["high"] >= stop:
            return False
        if bar["low"] <= target:
            return True
    return False


def ignored_outcome_class(trade: dict, bars15: list[dict], by_open: dict) -> str:
    ot = _utc(datetime.fromisoformat(trade["entry_open"]))
    bar = by_open[ot]
    idx = bar["candle_index"]
    entry = float(trade["entry_price"])
    stop = float(trade["stop"])
    fh = trade.get("first_hit")
    if fh == "TP":
        return "IGNORED_TP"
    if fh == "SL":
        if mfe_before_sl(bars15, idx, entry, stop, MFE_THRESH):
            return "IGNORED_MOVE_THEN_SL"
        return "IGNORED_SL"
    return "IGNORED_OTHER"


def active_lowers(pools: list[dict], moment: datetime, price: float) -> list[dict]:
    rows = [
        p
        for p in pools
        if p["side"] == "lower"
        and p["known"] <= moment
        and (p.get("break_at") is None or p["break_at"] > moment)
    ]
    rows.sort(key=lambda p: (p["known"], p["top"]))
    return rows


def active_uppers(pools: list[dict], moment: datetime) -> list[dict]:
    rows = [
        p
        for p in pools
        if p["side"] == "upper"
        and p["known"] <= moment
        and (p.get("break_at") is None or p["break_at"] > moment)
    ]
    rows.sort(key=lambda p: (p["known"], p["bottom"]))
    return rows


def ladder_metrics(lowers: list[dict]) -> dict:
    """Last up to 3 lowers by known time (chronological)."""
    last3 = lowers[-3:] if len(lowers) >= 3 else lowers[:]
    slots = {}
    for i, p in enumerate(last3[-3:], start=1):
        slots[f"last_lower_pool_{i}"] = {
            "pool_id": p.get("pool_id"),
            "known_at": p["known"].isoformat() if hasattr(p["known"], "isoformat") else p["known"],
            "top": float(p["top"]),
            "bottom": float(p["bottom"]),
            "active_at_time": True,
        }
    tops = [float(p["top"]) for p in last3]
    rising_2 = len(tops) >= 2 and tops[-1] > tops[-2]
    rising_3 = len(tops) >= 3 and tops[0] < tops[1] < tops[2]
    return {
        **slots,
        "rising_2_step": rising_2,
        "rising_3_step": rising_3,
        "tops_chronological": tops,
    }


def falling_upper_ladder(uppers: list[dict]) -> dict:
    last3 = uppers[-3:] if len(uppers) >= 3 else uppers[:]
    slots = {}
    for i, p in enumerate(last3[-3:], start=1):
        slots[f"last_upper_pool_{i}"] = {
            "pool_id": p.get("pool_id"),
            "known_at": p["known"].isoformat() if hasattr(p["known"], "isoformat") else p["known"],
            "top": float(p["top"]),
            "bottom": float(p["bottom"]),
        }
    bottoms = [float(p["bottom"]) for p in last3]
    falling_2 = len(bottoms) >= 2 and bottoms[-1] < bottoms[-2]
    falling_3 = len(bottoms) >= 3 and bottoms[0] > bottoms[1] > bottoms[2]
    return {**slots, "falling_2_step": falling_2, "falling_3_step": falling_3, "bottoms_chronological": bottoms}


def gap_to_previous_cluster(new_lower: dict, prior_lowers: list[dict]) -> dict:
    if not prior_lowers:
        return {"status": "no_prior_cluster", "gap_abs": None, "gap_pct": None}
    cluster_top = max(float(p["top"]) for p in prior_lowers)
    new_b = float(new_lower["bottom"])
    gap_abs = new_b - cluster_top
    gap_pct = gap_abs / cluster_top * 100.0 if cluster_top else None
    if gap_abs > 1e-8:
        status = "positive_gap"
    elif gap_abs < -1e-8:
        status = "overlap"
    else:
        status = "touching"
    return {
        "status": status,
        "gap_abs": round(gap_abs, 8),
        "gap_pct": round(gap_pct, 6) if gap_pct is not None else None,
        "previous_cluster_top": cluster_top,
        "new_lower_bottom": new_b,
    }


def gap_upper_mirror(new_upper: dict, prior_uppers: list[dict]) -> dict:
    if not prior_uppers:
        return {"status": "no_prior_cluster", "gap_abs": None, "gap_pct": None}
    cluster_bottom = min(float(p["bottom"]) for p in prior_uppers)
    new_t = float(new_upper["top"])
    gap_abs = cluster_bottom - new_t
    gap_pct = gap_abs / cluster_bottom * 100.0 if cluster_bottom else None
    if gap_abs > 1e-8:
        status = "positive_gap"
    elif gap_abs < -1e-8:
        status = "overlap"
    else:
        status = "touching"
    return {
        "status": status,
        "gap_abs": round(gap_abs, 8),
        "gap_pct": round(gap_pct, 6) if gap_pct is not None else None,
        "previous_cluster_bottom": cluster_bottom,
        "new_upper_top": new_t,
    }


def pools_at(candles, idx: int, moment: datetime, by_idx: dict, scan_cache: dict) -> list[dict]:
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index, ui_lld_config

    if idx not in by_idx:
        cfg = ui_lld_config("15m")
        pools, _, _ = scanner_pools_for_index(candles, "15m", idx, cfg, cache=scan_cache)
        by_idx[idx] = pools
    return by_idx[idx]


def ema_state_label(close: float, ema200: float | None) -> str:
    if ema200 is None:
        return "unknown"
    if close > ema200:
        return "above_ema200"
    if close < ema200:
        return "below_ema200"
    return "at_ema200"


def max_sl_streak(trades: list[dict]) -> int:
    best = cur = 0
    for t in trades:
        if t.get("first_hit") == "SL":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def cohort_stats(trades: list[dict]) -> dict:
    if not trades:
        return {
            "n": 0,
            "sl": 0,
            "tp": 0,
            "mfe_ge_041": 0,
            "median_mae": None,
            "median_mfe": None,
            "max_sl_streak": 0,
        }
    maes = [float(t["mae_pct"]) for t in trades if t.get("mae_pct") is not None]
    mfes = [float(t["mfe_pct"]) for t in trades if t.get("mfe_pct") is not None]
    return {
        "n": len(trades),
        "sl": sum(1 for t in trades if t.get("first_hit") == "SL"),
        "tp": sum(1 for t in trades if t.get("first_hit") == "TP"),
        "mfe_ge_041": sum(1 for t in trades if float(t.get("mfe_pct") or 0) >= MFE_THRESH),
        "median_mae": _median(maes),
        "median_mfe": _median(mfes),
        "max_sl_streak": max_sl_streak(trades),
    }


def per_cross_first_short_skip(signals: list[dict], bull_crosses: list[dict]) -> None:
    """Per-cross rule (forensic): each bull cross marks only the immediate next short."""
    shorts = sorted(signals, key=lambda s: s["decision_time"])
    for cross in bull_crosses:
        ct = _utc(datetime.fromisoformat(cross["cross_time"]))
        nxt = next((s for s in shorts if s["decision_dt"] > ct), None)
        if nxt:
            nxt.setdefault("per_cross_skip_short_for_crosses", []).append(cross["cross_id"])
    for s in signals:
        s["per_cross_first_short_after_any_bull"] = bool(s.get("per_cross_skip_short_for_crosses"))


def assign_ignore_flags(
    signals: list[dict],
    bull_crosses: list[dict],
    bear_crosses: list[dict],
) -> None:
    """
    State machine (shadow only):
    SEARCH_SHORT + bullish cross -> TRANSITION_AFTER_BULL_CROSS, arm skip-next-short.
    First SHORT in transition -> ignored, then back to SEARCH_SHORT.
    Repeated crosses while already in transition do not arm another skip.
    Bearish mirror arms skip-next-long (no long signals in XRP list).
    """
    shorts = sorted(signals, key=lambda s: s["decision_time"])
    all_crosses = sorted(bull_crosses + bear_crosses, key=lambda c: c["cross_time"])
    events: list[tuple[str, datetime, Any]] = []
    for c in all_crosses:
        events.append(("cross", _utc(datetime.fromisoformat(c["cross_time"])), c))
    for s in shorts:
        events.append(("short", s["decision_dt"], s))
    events.sort(key=lambda x: x[1])

    state = "SEARCH_SHORT"
    skip_next_short = False
    skip_next_long = False
    last_arm_cross: dict | None = None

    for typ, _t, obj in events:
        if typ == "cross":
            if obj["direction"] == "bullish":
                skip_next_long = False
                if state in ("SEARCH_SHORT", "TRANSITION_AFTER_BEAR_CROSS"):
                    state = "TRANSITION_AFTER_BULL_CROSS"
                    skip_next_short = True
                    last_arm_cross = obj
            elif obj["direction"] == "bearish":
                skip_next_short = False
                if state in ("SEARCH_SHORT", "TRANSITION_AFTER_BULL_CROSS"):
                    state = "TRANSITION_AFTER_BEAR_CROSS"
                    skip_next_long = True
                    last_arm_cross = obj
        else:
            if skip_next_short:
                obj["ignored_due_to_fresh_bull_cross"] = True
                if last_arm_cross:
                    obj["ignore_cross_id"] = last_arm_cross.get("cross_id")
                    last_arm_cross["skip_applied_to"] = obj["entry_open"]
                skip_next_short = False
                state = "SEARCH_SHORT"
            elif skip_next_long:
                # Short does not consume bear-cross long-skip; shorts allowed again.
                if state == "TRANSITION_AFTER_BEAR_CROSS":
                    state = "SEARCH_SHORT"
            else:
                if state.startswith("TRANSITION"):
                    state = "SEARCH_SHORT"

    for cross in bear_crosses:
        if "skip_applied_to" not in cross:
            cross["skip_applied_to"] = None
        cross.setdefault("note", "no_long_signals_in_dataset")


def next_signal_after(ignored: dict, all_signals: list[dict]) -> dict | None:
    t0 = ignored["decision_dt"]
    for s in sorted(all_signals, key=lambda x: x["decision_time"]):
        if s["decision_dt"] > t0:
            return s
    return None


def analyze_next_after_ignored(
    ignored: dict,
    nxt: dict | None,
    bars15: list[dict],
    by_open: dict,
    candles,
    pool_by_idx: dict,
    scan_cache: dict,
) -> dict:
    if not nxt:
        return {"classification": "NO_NEXT_SIGNAL"}
    bar = by_open[_utc(datetime.fromisoformat(nxt["entry_open"]))]
    pools = pools_at(candles, bar["candle_index"], bar["close_time"], pool_by_idx, scan_cache)
    lowers = active_lowers(pools, bar["close_time"], float(nxt["entry_price"]))
    uppers = active_uppers(pools, bar["close_time"])
    lad = ladder_metrics(lowers)
    ema = bar.get("ema200")
    cls = "NEXT_SHORT"
    return {
        "classification": cls,
        "entry_open": nxt["entry_open"],
        "mae_pct": nxt.get("mae_pct"),
        "mfe_pct": nxt.get("mfe_pct"),
        "first_hit": nxt.get("first_hit"),
        "price_above_ema200": float(nxt["entry_price"]) > ema if ema else None,
        "rising_lower_ladder": lad,
        "falling_upper_ladder": falling_upper_ladder(uppers),
        "new_lower_after_cross": len(lowers) > 0,
    }


def build_june11_timeline(
    bars15: list[dict],
    crosses_15m: list[dict],
    signals: list[dict],
    candles,
    pool_by_idx: dict,
    scan_cache: dict,
) -> list[dict]:
    t0 = datetime(2026, 6, 5, tzinfo=timezone.utc)
    t1 = datetime(2026, 6, 20, tzinfo=timezone.utc)
    rows = []
    sig_by_dec = {s["decision_dt"]: s for s in signals}
    cross_times = {
        _utc(datetime.fromisoformat(c["cross_time"]))
        for c in crosses_15m
        if c["direction"] == "bullish"
    }
    last_bull = None
    pending_skip = False
    state = "SEARCH_SHORT"
    for bar in bars15:
        ct = bar["close_time"]
        if ct < t0 or ct > t1:
            continue
        ev = None
        if ct in cross_times:
            last_bull = ct
            pending_skip = True
            state = "TRANSITION_AFTER_BULL_CROSS"
            ev = "ema200_bull_cross"
        ema = bar.get("ema200")
        price = bar["close"]
        pool_ev = ""
        pools = pools_at(candles, bar["candle_index"], ct, pool_by_idx, scan_cache)
        uppers = active_uppers(pools, ct)
        if uppers and price >= min(float(u["bottom"]) for u in uppers[-3:]):
            pool_ev = "in_upper_zone"
        lowers = active_lowers(pools, ct, price)
        if lowers and any(price <= float(l["top"]) for l in lowers[-3:]):
            pool_ev = (pool_ev + ";touch_lower") if pool_ev else "touch_lower"
        sig = sig_by_dec.get(ct)
        sig_dir = ""
        outcome = ""
        ignored = ""
        if sig:
            sig_dir = "SHORT"
            outcome = sig.get("first_hit", "")
            if sig.get("ignored_due_to_fresh_bull_cross"):
                ignored = "yes"
                pending_skip = False
            elif pending_skip and sig_dir == "SHORT":
                ignored = "would_skip"
        if sig and ignored == "yes":
            state = "TRANSITION_AFTER_BULL_CROSS"
        rows.append(
            {
                "timestamp": ct.isoformat(),
                "price": price,
                "ema200": ema,
                "ema200_state": ema_state_label(price, ema),
                "signal_direction": sig_dir,
                "signal_outcome": outcome,
                "ignored_shadow": ignored,
                "pool_event": pool_ev,
                "transition_state": state,
                "rising_2": ladder_metrics(lowers)["rising_2_step"] if lowers else False,
            }
        )
    return rows


def main() -> None:
    _ensure_paths()
    from find_short_entry_15m_v1 import HISTORY_WEEKS, build_15m_bars, load_market
    from dashboard.research_charts.lld_research_kernel import load_pane_candles

    data = json.loads((ROOT / "floor_guard_signal_list_v1.json").read_text())
    sym = data["symbols"]["XRPUSDT"]
    with_guard = sym["with_guard"]
    without = sym["without_guard"]
    wg_opens = {t["entry_open"] for t in with_guard}

    market = load_market(SYMBOL)
    _p, candles = load_pane_candles(
        SYMBOL, "15m", from_unix=int(PANE_FROM.timestamp()), to_unix=int(PANE_TO.timestamp()), history_weeks=HISTORY_WEEKS
    )
    bars15 = build_15m_bars(candles)
    by_open = {b["open_time"]: b for b in bars15}
    bars30 = market["30m"]["bars"]

    crosses_15m = [
        c
        for c in detect_ema200_crosses(bars15, "15m")
        if REPORT_FROM <= _utc(datetime.fromisoformat(c["cross_time"])) <= REPORT_TO
    ]
    crosses_30m = [
        c
        for c in detect_ema200_crosses(bars30, "30m")
        if REPORT_FROM <= _utc(datetime.fromisoformat(c["cross_time"])) <= REPORT_TO
    ]
    bull_15 = [c for c in crosses_15m if c["direction"] == "bullish"]
    bear_15 = [c for c in crosses_15m if c["direction"] == "bearish"]

    pool_by_idx: dict = {}
    scan_cache: dict = {}
    signals: list[dict] = []
    for t in without:
        ot = _utc(datetime.fromisoformat(t["entry_open"]))
        if ot not in by_open:
            continue
        bar = by_open[ot]
        decision = bar["close_time"]
        if decision < REPORT_FROM or decision > REPORT_TO:
            continue
        pools = pools_at(candles, bar["candle_index"], decision, pool_by_idx, scan_cache)
        lowers = active_lowers(pools, decision, float(t["entry_price"]))
        lad = ladder_metrics(lowers)
        new_lower = lowers[-1] if lowers else None
        prior = lowers[:-1] if len(lowers) > 1 else []
        gap = gap_to_previous_cluster(new_lower, prior) if new_lower else {}
        rec = {
            **t,
            "signal_time": ot.isoformat(),
            "decision_time": decision.isoformat(),
            "decision_dt": decision,
            "direction": "SHORT",
            "guard_blocked": t["entry_open"] not in wg_opens,
            "ignored_due_to_fresh_bull_cross": False,
            "ignored_due_to_fresh_bear_cross_long": False,
            "pool_ladder": lad,
            "lower_gap_to_prior_cluster": gap,
        }
        signals.append(rec)

    assign_ignore_flags(signals, bull_15, bear_15)
    per_cross_first_short_skip(signals, bull_15)

    for s in signals:
        if s.get("ignored_due_to_fresh_bull_cross"):
            s["ignored_outcome_class"] = ignored_outcome_class(s, bars15, by_open)
            nxt = next_signal_after(s, signals)
            s["next_after_ignored"] = analyze_next_after_ignored(
                s, nxt, bars15, by_open, candles, pool_by_idx, scan_cache
            )

    ignored_all = [s for s in signals if s.get("ignored_due_to_fresh_bull_cross")]
    ignored_wg = [s for s in ignored_all if not s["guard_blocked"]]

    shadow_trades = [t for t in with_guard if t["entry_open"] not in {s["entry_open"] for s in ignored_wg}]

    current_stats = cohort_stats(with_guard)
    shadow_stats = cohort_stats(shadow_trades)

    # SL streak on ordered with_guard
    wg_sorted = sorted(with_guard, key=lambda x: x["entry_open"])
    sh_sorted = sorted(shadow_trades, key=lambda x: x["entry_open"])
    streak_impact = {
        "current_max_sl_streak": max_sl_streak(wg_sorted),
        "shadow_max_sl_streak": max_sl_streak(sh_sorted),
        "bad4_in_current": sum(1 for t in wg_sorted if t["entry_open"] in BAD_STREAK),
        "bad4_in_shadow": sum(1 for t in sh_sorted if t["entry_open"] in BAD_STREAK),
        "bad4_removed_by_shadow": [s["entry_open"] for s in ignored_wg if s["entry_open"] in BAD_STREAK],
    }

    june_anchor = next(s for s in signals if s["entry_open"] == BAD_FIRST)
    bull_before_june = [
        c for c in bull_15 if _utc(datetime.fromisoformat(c["cross_time"])) < june_anchor["decision_dt"]
    ]
    last_bull = max(bull_before_june, key=lambda c: c["cross_time"]) if bull_before_june else None

    cross_1745 = next((c for c in bull_15 if c["cross_time"] == "2026-06-11T17:45:00+00:00"), None)
    first_short_after_1745 = None
    if cross_1745:
        ct = _utc(datetime.fromisoformat(cross_1745["cross_time"]))
        first_short_after_1745 = next((s for s in signals if s["decision_dt"] > ct), None)

    june_forensic = {
        "anchor_short": BAD_FIRST,
        "is_first_short_after_last_bull_cross": june_anchor.get("ignored_due_to_fresh_bull_cross"),
        "last_bull_cross_before_short": last_bull,
        "bull_cross_2026_06_11_1745": cross_1745,
        "first_short_after_1745_cross": first_short_after_1745["entry_open"] if first_short_after_1745 else None,
        "state_machine_would_ignore": june_anchor.get("ignored_due_to_fresh_bull_cross"),
        "per_cross_would_ignore_11_06_short": (
            first_short_after_1745 is not None and first_short_after_1745["entry_open"] == BAD_FIRST
        ),
        "would_ignore": june_anchor.get("ignored_due_to_fresh_bull_cross"),
        "ignored_outcome_class": june_anchor.get("ignored_outcome_class"),
        "next_after_ignored": june_anchor.get("next_after_ignored"),
        "pool_ladder_at_entry": june_anchor.get("pool_ladder"),
        "long_signals_in_dataset": 0,
        "note_long": "Keine separaten Long-Signale in floor_guard_signal_list; Bear-Cross→Long-Skip nur vorbereitet.",
    }

    timeline = build_june11_timeline(bars15, crosses_15m, signals, candles, pool_by_idx, scan_cache)
    compact_tl = [r for r in timeline if r["signal_direction"] or r["pool_event"] or "bull" in r.get("pool_event", "") or r["ignored_shadow"]]
    # add cross rows
    for c in crosses_15m:
        ct = c["cross_time"]
        if "2026-06-05" <= ct <= "2026-06-20":
            compact_tl.append(
                {
                    "timestamp": ct,
                    "price": c["close_after"],
                    "ema200": c["ema200_after"],
                    "ema200_state": "cross_" + c["direction"],
                    "signal_direction": "",
                    "signal_outcome": "",
                    "pool_event": "ema200_cross",
                    "transition_state": "TRANSITION_AFTER_BULL_CROSS" if c["direction"] == "bullish" else "TRANSITION_AFTER_BEAR_CROSS",
                }
            )
    compact_tl.sort(key=lambda r: r["timestamp"])

    ignored_stats = {
        "total_ignored_hypothetical": len(ignored_all),
        "ignored_guard_allowed": len(ignored_wg),
        "IGNORED_SL": sum(1 for s in ignored_all if s.get("ignored_outcome_class") == "IGNORED_SL"),
        "IGNORED_TP": sum(1 for s in ignored_all if s.get("ignored_outcome_class") == "IGNORED_TP"),
        "IGNORED_MOVE_THEN_SL": sum(1 for s in ignored_all if s.get("ignored_outcome_class") == "IGNORED_MOVE_THEN_SL"),
        "IGNORED_OTHER": sum(1 for s in ignored_all if s.get("ignored_outcome_class") == "IGNORED_OTHER"),
        "median_mae": _median([float(s["mae_pct"]) for s in ignored_all if s.get("mae_pct") is not None]),
        "median_mfe": _median([float(s["mfe_pct"]) for s in ignored_all if s.get("mfe_pct") is not None]),
    }

    next_dirs = {"NEXT_SHORT": 0, "NEXT_LONG": 0, "NO_NEXT_SIGNAL": 0}
    for s in ignored_all:
        na = s.get("next_after_ignored") or {}
        cl = na.get("classification", "NO_NEXT_SIGNAL")
        if cl == "NEXT_SHORT":
            next_dirs["NEXT_SHORT"] += 1
        elif cl == "NEXT_LONG":
            next_dirs["NEXT_LONG"] += 1
        else:
            next_dirs["NO_NEXT_SIGNAL"] += 1

    ladder_desc = {
        "ignored_with_rising_2": sum(1 for s in ignored_all if (s.get("pool_ladder") or {}).get("rising_2_step")),
        "ignored_with_rising_3": sum(1 for s in ignored_all if (s.get("pool_ladder") or {}).get("rising_3_step")),
    }

    strategy_note = (
        "Produktion filtert Shorts u.a. mit down_4h() auf 4h-EMA200 (bearish stack). "
        "Diese Shadow-Analyse nutzt bewusst 15m-EMA200-Crosses (30m nur Kontextliste, nicht kombiniert)."
    )

    verdict = (
        f"11.06. erster Short nach Bull-Cross: ignore={june_forensic['would_ignore']}, "
        f"Klasse={june_forensic.get('ignored_outcome_class')}. "
        f"Shadow entfernt {len(streak_impact['bad4_removed_by_shadow'])}/4 BAD-Streak aus with_guard. "
        f"Kosten: {ignored_stats['IGNORED_TP']} IGNORED_TP unter allen Ignores, "
        f"{ignored_stats['ignored_guard_allowed']} ignorierte erlaubte Trades."
    )

    report = {
        "meta": {
            "analysis_only": True,
            "strategy_ema_note": strategy_note,
            "start_state": "SEARCH_SHORT",
        },
        "ema200_cross_coverage": {
            "15m_bullish": len(bull_15),
            "15m_bearish": len(bear_15),
            "30m_bullish": sum(1 for c in crosses_30m if c["direction"] == "bullish"),
            "30m_bearish": sum(1 for c in crosses_30m if c["direction"] == "bearish"),
            "crosses_15m_sample": crosses_15m[:5],
            "crosses_30m_count_only": len(crosses_30m),
        },
        "first_opposite_after_cross": ignored_stats,
        "june_11_forensic": june_forensic,
        "june_11_timeline_compact": compact_tl,
        "pool_ladder_after_cross": ladder_desc,
        "next_signal_after_ignored": next_dirs,
        "comparison": {
            "CURRENT": current_stats,
            "EMA200_TRANSITION_SHADOW": shadow_stats,
            "ignored_count_shadow": len(ignored_wg),
        },
        "sl_streak_impact": streak_impact,
        "symmetry": {
            "bear_cross_count": len(bear_15),
            "long_signals": 0,
            "long_ignores_applied": 0,
            "mirror_ready": True,
        },
        "verdict": verdict,
        "signals": signals,
        "crosses_15m": crosses_15m,
        "crosses_30m": crosses_30m,
    }

    exec_md = build_md(report)
    report["executive_summary"] = exec_md
    OUT_JSON.write_text(json.dumps(report, indent=2, default=str) + "\n")
    OUT_MD.write_text(exec_md + "\n")

    fields = [
        "signal_time",
        "decision_time",
        "entry_open",
        "entry_price",
        "guard_blocked",
        "direction",
        "ignored_due_to_fresh_bull_cross",
        "ignored_outcome_class",
        "mae_pct",
        "mfe_pct",
        "first_hit",
        "outcome",
        "rising_2_step",
        "rising_3_step",
    ]
    with OUT_CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for s in signals:
            row = {**s}
            pl = s.get("pool_ladder") or {}
            row["rising_2_step"] = pl.get("rising_2_step")
            row["rising_3_step"] = pl.get("rising_3_step")
            w.writerow(row)

    print("wrote", OUT_JSON, OUT_MD, OUT_CSV)


def build_md(report: dict) -> str:
    cov = report["ema200_cross_coverage"]
    ign = report["first_opposite_after_cross"]
    cmp = report["comparison"]
    streak = report["sl_streak_impact"]
    lines = [
        "# XRP EMA200 Transition Shadow v1",
        "",
        "Nur Shadow-Analyse. Keine Produktionsänderung.",
        "",
        report["meta"]["strategy_ema_note"],
        "",
        "### EMA200 cross coverage",
        f"15m: {cov['15m_bullish']} bullish, {cov['15m_bearish']} bearish (Jun–Jul). "
        f"30m Kontext: {cov['30m_bullish']} bull / {cov['30m_bearish']} bear (nicht kombiniert).",
        "",
        "### First opposite signal after cross",
        f"Ignoriert (hypothetisch): {ign['total_ignored_hypothetical']}, davon Guard-erlaubt: {ign['ignored_guard_allowed']}. "
        f"SL={ign['IGNORED_SL']} TP={ign['IGNORED_TP']} MOVE_THEN_SL={ign['IGNORED_MOVE_THEN_SL']} OTHER={ign['IGNORED_OTHER']}. "
        f"Median MAE={ign['median_mae']}, MFE={ign['median_mfe']}.",
        "",
        "### XRP 11 June forensic timeline",
        json.dumps(report["june_11_forensic"], indent=2),
        "",
        "Timeline (kompakt): siehe `june_11_timeline_compact` in JSON.",
        "",
        "### Pool ladder after cross",
        json.dumps(report["pool_ladder_after_cross"], indent=2),
        "",
        "### Next signal after ignored trade",
        json.dumps(report["next_signal_after_ignored"], indent=2),
        "",
        "### Current vs transition shadow",
        "| Metrik | CURRENT | SHADOW |",
        "|--------|--------:|-------:|",
        f"| Trades | {cmp['CURRENT'].get('n')} | {cmp['EMA200_TRANSITION_SHADOW'].get('n')} |",
        f"| SL | {cmp['CURRENT'].get('sl')} | {cmp['EMA200_TRANSITION_SHADOW'].get('sl')} |",
        f"| TP | {cmp['CURRENT'].get('tp')} | {cmp['EMA200_TRANSITION_SHADOW'].get('tp')} |",
        f"| >=0.41% MFE | {cmp['CURRENT'].get('mfe_ge_041')} | {cmp['EMA200_TRANSITION_SHADOW'].get('mfe_ge_041')} |",
        f"| median MAE | {cmp['CURRENT'].get('median_mae')} | {cmp['EMA200_TRANSITION_SHADOW'].get('median_mae')} |",
        f"| median MFE | {cmp['CURRENT'].get('median_mfe')} | {cmp['EMA200_TRANSITION_SHADOW'].get('median_mfe')} |",
        "",
        "### SL streak impact",
        json.dumps(streak, indent=2),
        "",
        "### MAE / MFE impact",
        f"CURRENT median MAE {cmp['CURRENT'].get('median_mae')} / MFE {cmp['CURRENT'].get('median_mfe')}; "
        f"SHADOW median MAE {cmp['EMA200_TRANSITION_SHADOW'].get('median_mae')} / MFE {cmp['EMA200_TRANSITION_SHADOW'].get('median_mfe')}.",
        "",
        "### Symmetry",
        json.dumps(report["symmetry"], indent=2),
        "",
        "### Verdict",
        report["verdict"],
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    main()
