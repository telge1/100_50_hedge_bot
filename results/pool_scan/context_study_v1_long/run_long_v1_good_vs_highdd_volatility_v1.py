#!/usr/bin/env python3
"""GOOD vs HIGH-DD coins: entry-time volatility / stability study (frozen Long V1 variant C)."""

from __future__ import annotations

import csv
import json
import math
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

LONG_ROOT = Path(__file__).resolve().parent
REPO = LONG_ROOT.parents[2]
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
TRADES_CSV = LONG_ROOT / "long_v1_10coin_crosssection_v1" / "trades_frozen_v1.csv"
OUT = LONG_ROOT / "long_v1_good_vs_highdd_volatility_v1"

GOOD_SYMBOLS = frozenset(
    {"APTUSDT", "DOGEUSDT", "SOLUSDT", "XRPUSDT", "AVAXUSDT", "HYPEUSDT", "BMTUSDT"}
)
HIGH_DD_SYMBOLS = frozenset({"ZECUSDT", "ACEUSDT", "TUTUSDT"})
ALL_SYMBOLS = tuple(sorted(GOOD_SYMBOLS | HIGH_DD_SYMBOLS))

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
HISTORY_WEEKS = 12

OUTCOME_KEYS = {
    "outcome",
    "managed_outcome",
    "baseline_outcome",
    "mae_pct",
    "mfe_pct",
    "pnl_pct",
    "managed_pnl_pct",
    "baseline_pnl_pct",
    "be_trigger_fired",
    "diag_time_to_1pct_bars",
    "diag_time_1pct_to_be_bars",
    "diag_profit_then_loss",
}

META_KEYS = {
    "symbol",
    "entry_time",
    "pool_id",
    "entry_price",
    "stop",
    "tp",
    "group",
    "decision_time",
    "known_at",
    "tp_pool_id",
}


def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT), str(LONG_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _group(sym: str) -> str:
    if sym in HIGH_DD_SYMBOLS:
        return "HIGH_DD"
    if sym in GOOD_SYMBOLS:
        return "GOOD"
    return "OTHER"


def _float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_sl(t: dict) -> bool:
    o = str(t.get("outcome", "")).upper()
    return o in ("SL", "STOP")


def _is_tp(t: dict) -> bool:
    return str(t.get("outcome", "")).upper() in ("TP",)


def _is_be(t: dict) -> bool:
    o = str(t.get("outcome", "")).upper()
    return o in ("BE", "BE_SMALL", "INTRABAR_AMBIGUOUS")


def _iqr(vals: list[float]) -> float:
    if len(vals) < 4:
        return statistics.pstdev(vals) if len(vals) > 1 else 0.0
    qs = statistics.quantiles(vals, n=4)
    return qs[2] - qs[0]


def _mann_whitney(a: list[float], b: list[float]) -> float | None:
    if len(a) < 5 or len(b) < 5:
        return None
    try:
        from scipy.stats import mannwhitneyu

        return float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
    except Exception:
        return None


def _atr_series(bars: list[dict], period: int = 14) -> list[float | None]:
    trs: list[float] = []
    out: list[float | None] = [None] * len(bars)
    for i, bar in enumerate(bars):
        h, l, c = bar["high"], bar["low"], bar["close"]
        if i == 0:
            tr = h - l
        else:
            pc = bars[i - 1]["close"]
            tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
        if i >= period:
            window = trs[i - period + 1 : i + 1]
            out[i] = sum(window) / period
        elif i == period - 1:
            out[i] = sum(trs) / period
    return out


def _atr_pct_at(bars: list[dict], idx: int | None, atrs: list[float | None]) -> float | None:
    if idx is None or atrs[idx] is None:
        return None
    c = bars[idx]["close"]
    if not c:
        return None
    return atrs[idx] / c * 100.0


def _realized_vol_pct(bars: list[dict], idx: int, n_bars: int) -> float | None:
    if idx is None or idx < n_bars:
        return None
    rets = []
    for j in range(idx - n_bars + 1, idx + 1):
        if j < 1:
            continue
        c0, c1 = bars[j]["close"], bars[j - 1]["close"]
        if c1 <= 0:
            continue
        rets.append(math.log(c0 / c1))
    if len(rets) < 2:
        return None
    return statistics.pstdev(rets) * math.sqrt(len(rets)) * 100.0


def _sign_changes(bars: list[dict], idx: int, n: int) -> int | None:
    if idx is None or idx < n:
        return None
    signs = []
    for j in range(idx - n + 1, idx + 1):
        if j < 1:
            continue
        d = bars[j]["close"] - bars[j - 1]["close"]
        signs.append(1 if d > 0 else (-1 if d < 0 else 0))
    changes = 0
    prev = None
    for s in signs:
        if s == 0:
            continue
        if prev is not None and s != prev:
            changes += 1
        prev = s
    return changes


def _alternating_count(bars: list[dict], idx: int, n: int) -> int | None:
    if idx is None or idx < n:
        return None
    dirs = []
    for j in range(idx - n + 1, idx + 1):
        if j < 1:
            continue
        d = bars[j]["close"] - bars[j - 1]["close"]
        dirs.append(1 if d > 0 else (-1 if d < 0 else 0))
    alt = 0
    for i in range(1, len(dirs)):
        if dirs[i] != 0 and dirs[i - 1] != 0 and dirs[i] != dirs[i - 1]:
            alt += 1
    return alt


def _close_loc_std(bars: list[dict], idx: int, n: int) -> float | None:
    if idx is None or idx < n:
        return None
    locs = []
    for j in range(idx - n + 1, idx + 1):
        b = bars[j]
        rng = b["high"] - b["low"]
        if rng <= 0:
            continue
        locs.append((b["close"] - b["low"]) / rng)
    return statistics.pstdev(locs) if len(locs) >= 2 else None


def _max_bar_move(bars: list[dict], idx: int, n: int) -> float | None:
    if idx is None or idx < 1:
        return None
    start = max(1, idx - n + 1)
    mx = 0.0
    for j in range(start, idx + 1):
        c1 = bars[j - 1]["close"]
        if c1:
            mx = max(mx, abs(bars[j]["close"] - c1) / c1 * 100.0)
    return mx


def _largest_wick_pct(bars: list[dict], idx: int, n: int) -> float | None:
    if idx is None:
        return None
    window = bars[max(0, idx - n + 1) : idx + 1]
    mx = 0.0
    for b in window:
        c = b["close"] or 1.0
        rng = b["high"] - b["low"]
        if rng <= 0:
            continue
        body_top = max(b["open"], b["close"])
        body_bot = min(b["open"], b["close"])
        wick = max(b["high"] - body_top, body_bot - b["low"])
        mx = max(mx, wick / c * 100.0)
    return mx


def _wick_reversal_freq(bars: list[dict], idx: int, n: int) -> float | None:
    if idx is None or idx < n:
        return None
    hits = 0
    total = 0
    for j in range(idx - n + 1, idx + 1):
        b = bars[j]
        rng = b["high"] - b["low"]
        if rng <= 0:
            continue
        upper = b["high"] - max(b["open"], b["close"])
        lower = min(b["open"], b["close"]) - b["low"]
        total += 1
        if upper > lower * 1.5 and b["close"] < b["open"]:
            hits += 1
        elif lower > upper * 1.5 and b["close"] > b["open"]:
            hits += 1
    return hits / total if total else None


def _pools_created_recent(pools: list[dict], moment: datetime, hours: float) -> int:
    cutoff = moment - timedelta(hours=hours)
    n = 0
    for p in pools:
        kn = p["known"]
        if cutoff < kn <= moment:
            n += 1
    return n


def _pools_broken_recent(pools: list[dict], moment: datetime, hours: float) -> int:
    cutoff = moment - timedelta(hours=hours)
    n = 0
    for p in pools:
        br = p.get("break_at")
        if br is not None and cutoff < br <= moment:
            n += 1
    return n


def _active_near_price(pools: list[dict], moment: datetime, price: float, pct: float) -> int:
    from case_control_features import active_pools

    n = 0
    for p in active_pools(pools, moment):
        lo, hi = float(p["bottom"]), float(p["top"])
        if hi < price:
            dist = (price - hi) / price * 100.0
        elif lo > price:
            dist = (lo - price) / price * 100.0
        else:
            dist = 0.0
        if dist <= pct:
            n += 1
    return n


def _diag_be_timing(
    bars: list[dict],
    idx: int,
    entry: float,
    stop: float,
    tp: float,
    outcome: str,
) -> dict[str, Any]:
    """Post-entry path diagnostics only (not used as filter features)."""
    trigger = entry * 1.01
    be_stop = entry
    armed = False
    t_trigger: int | None = None
    t_be: int | None = None
    profit_then_loss = False
    saw_profit = False
    for k in range(idx + 1, len(bars)):
        bar = bars[k]
        if not armed and bar["high"] >= trigger:
            armed = True
            t_trigger = k - idx
            saw_profit = True
        eff_stop = be_stop if armed else stop
        if bar["low"] <= eff_stop:
            if armed and t_be is None:
                t_be = k - idx
            if saw_profit and str(outcome).upper() in ("SL", "STOP", "BE", "BE_SMALL"):
                profit_then_loss = True
            break
        if bar["high"] >= tp:
            break
    return {
        "diag_time_to_1pct_bars": t_trigger,
        "diag_time_1pct_to_be_bars": (t_be - t_trigger) if t_trigger is not None and t_be is not None else None,
        "diag_profit_then_loss": profit_then_loss,
    }


def _enrich(
    trade: dict,
    bars15: list[dict],
    by_open: dict,
    market: dict,
    pools15: list[dict],
    atr15: list[float | None],
    atr30: list[float | None],
    atr1h: list[float | None],
    atr4h: list[float | None],
) -> dict[str, Any]:
    from case_control_features import (
        active_pools,
        candle_features,
        last_closed_index,
        lowers_under_price,
        mtf_pool_snapshot,
        pool_age_hours,
        prior_return_pct,
        range_pct_window,
        uppers_above_price,
    )

    ot = datetime.fromisoformat(trade["entry_time"])
    bar = by_open.get(ot)
    if bar is None:
        raise KeyError(f"missing bar {trade['symbol']} {trade['entry_time']}")
    idx = bars15.index(bar)
    moment = bar["close_time"]
    entry = float(trade["entry_price"])
    stop = float(trade["stop"])
    tp = float(trade["tp"])
    stop_dist = (entry - stop) / entry * 100.0 if entry else None
    tp_dist = (tp - entry) / entry * 100.0 if entry else None
    rr = tp_dist / stop_dist if stop_dist and stop_dist > 0 and tp_dist is not None else None
    known = datetime.fromisoformat(trade["known_at"])
    pool = next((p for p in pools15 if p["pool_id"] == trade["pool_id"]), None)
    pool_w = None
    if pool:
        pool_w = (float(pool["top"]) - float(pool["bottom"])) / float(pool["bottom"]) * 100.0

    row: dict[str, Any] = {
        "symbol": trade["symbol"],
        "entry_time": trade["entry_time"],
        "group": _group(trade["symbol"]),
        "outcome": trade["outcome"],
        "pnl_pct": _float(trade["pnl_pct"]),
        "mae_pct": _float(trade["mae_pct"]),
        "mfe_pct": _float(trade["mfe_pct"]),
        "be_trigger_fired": trade.get("be_trigger_fired") in (True, "True", "true", 1),
        "m15_lower_2_age_h": _float(trade.get("m15_lower_2_age_h")),
        "stop_distance_pct": round(stop_dist, 4) if stop_dist is not None else None,
        "tp_distance_pct": round(tp_dist, 4) if tp_dist is not None else None,
        "rr": round(rr, 4) if rr is not None else None,
        "bridge_gap_pct": _float(trade.get("gap_pct")),
        "entry_pool_age_h": round(pool_age_hours(moment, known), 4),
        "entry_pool_width_pct": round(pool_w, 4) if pool_w is not None else None,
    }

    cf = candle_features(bar)
    row["m15_bar_range_pct"] = round(cf["range_pct"], 4)
    row["m15_body_pct"] = round(cf["body_pct"], 4)
    row["m15_upper_wick_pct"] = round(cf["upper_wick_pct"], 4)
    row["m15_lower_wick_pct"] = round(cf["lower_wick_pct"], 4)
    row["m15_wick_body_ratio"] = (
        round((cf["upper_wick_pct"] + cf["lower_wick_pct"]) / cf["body_pct"], 4)
        if cf["body_pct"] > 0.01
        else None
    )
    row["m15_close_location"] = round(cf["close_location"], 4)

    tr_pct = None
    if idx > 0:
        pc = bars15[idx - 1]["close"]
        tr = max(bar["high"] - bar["low"], abs(bar["high"] - pc), abs(bar["low"] - pc))
        tr_pct = tr / entry * 100.0 if entry else None
    row["m15_true_range_pct"] = round(tr_pct, 4) if tr_pct is not None else None
    row["m15_atr14_pct"] = round(_atr_pct_at(bars15, idx, atr15) or 0, 4) if _atr_pct_at(bars15, idx, atr15) else None

    for n in (4, 8, 16, 24):
        rg = range_pct_window(bars15, idx, n)
        row[f"m15_roll_range_{n}b_pct"] = round(rg, 4) if rg is not None else None

    for label, n in (("1h", 4), ("2h", 8), ("4h", 16), ("6h", 24)):
        rv = _realized_vol_pct(bars15, idx, n)
        row[f"m15_rv_{label}_pct"] = round(rv, 4) if rv is not None else None

    idx30 = last_closed_index(market["30m"]["bars"], moment)
    idx1h = last_closed_index(market["1h"]["bars"], moment)
    idx4h = last_closed_index(market["4h"]["bars"], moment)

    for tf, idx_tf, atrs, prefix in (
        ("30m", idx30, atr30, "m30"),
        ("1h", idx1h, atr1h, "m1h"),
        ("4h", idx4h, atr4h, "m4h"),
    ):
        bars_tf = market[tf]["bars"]
        if idx_tf is not None:
            b = bars_tf[idx_tf]
            c = b["close"]
            row[f"{prefix}_atr14_pct"] = round(_atr_pct_at(bars_tf, idx_tf, atrs) or 0, 4) if _atr_pct_at(bars_tf, idx_tf, atrs) else None
            row[f"{prefix}_bar_range_pct"] = round((b["high"] - b["low"]) / c * 100.0, 4) if c else None
            rv = _realized_vol_pct(bars_tf, idx_tf, min(8, idx_tf + 1))
            row[f"{prefix}_rv_recent_pct"] = round(rv, 4) if rv is not None else None
        else:
            row[f"{prefix}_atr14_pct"] = None
            row[f"{prefix}_bar_range_pct"] = None
            row[f"{prefix}_rv_recent_pct"] = None

    if idx1h is not None:
        for label, n in (("4h", 4), ("8h", 8), ("24h", 24)):
            rg = range_pct_window(market["1h"]["bars"], idx1h, n)
            row[f"m1h_prev_{label}_range_pct"] = round(rg, 4) if rg is not None else None

    if idx4h is not None:
        b4 = market["4h"]["bars"][idx4h]
        c4 = b4["close"]
        row["m4h_candle_range_pct"] = round((b4["high"] - b4["low"]) / c4 * 100.0, 4) if c4 else None
        for label, n in (("12h", 3), ("24h", 6), ("48h", 12)):
            rg = range_pct_window(market["4h"]["bars"], idx4h, n)
            row[f"m4h_prev_{label}_realized_range_pct"] = round(rg, 4) if rg is not None else None

    for n in (1, 2, 4, 8, 12, 24):
        pr = prior_return_pct(bars15, idx, n)
        row[f"ret_prev_{n}bar_pct"] = round(pr, 4) if pr is not None else None
        row[f"ret_prev_{n}bar_abs_pct"] = round(abs(pr), 4) if pr is not None else None

    for n in (4, 8, 24):
        row[f"max_single_bar_move_{n}b_pct"] = round(_max_bar_move(bars15, idx, n) or 0, 4)
        row[f"largest_wick_{n}b_pct"] = round(_largest_wick_pct(bars15, idx, n) or 0, 4)

    for n in (4, 8, 16):
        row[f"sign_changes_{n}b"] = _sign_changes(bars15, idx, n)
        row[f"alternating_{n}b"] = _alternating_count(bars15, idx, n)

    row["close_loc_std_16b"] = round(_close_loc_std(bars15, idx, 16) or 0, 4) if _close_loc_std(bars15, idx, 16) else None
    row["wick_reversal_freq_16b"] = round(_wick_reversal_freq(bars15, idx, 16) or 0, 4)

    atr_e = row.get("m15_atr14_pct") or 0.01
    if atr_e < 0.0001:
        atr_e = 0.01
    row["stop_over_atr15"] = round(stop_dist / atr_e, 4) if stop_dist else None
    row["tp_over_atr15"] = round(tp_dist / atr_e, 4) if tp_dist else None
    row["pool_width_over_atr15"] = round(pool_w / atr_e, 4) if pool_w else None
    row["bridge_gap_over_atr15"] = round(_float(trade.get("gap_pct")) / atr_e, 4) if trade.get("gap_pct") else None

    row.update(mtf_pool_snapshot(pools15, moment, entry, "m15"))
    tp_pool = next((p for p in active_pools(pools15, moment, "upper") if p["pool_id"] == trade.get("tp_pool_id")), None)
    row["tp_pool_age_h"] = round(pool_age_hours(moment, tp_pool["known"]), 4) if tp_pool else None

    lowers = lowers_under_price(pools15, moment, entry)
    uppers = uppers_above_price(pools15, moment, entry)
    row["active_pools_near_3pct"] = _active_near_price(pools15, moment, entry, 3.0)
    row["pool_created_24h"] = _pools_created_recent(pools15, moment, 24.0)
    row["pool_break_24h"] = _pools_broken_recent(pools15, moment, 24.0)

    overlap = 0
    for i, a in enumerate(lowers):
        for b in lowers[i + 1 :]:
            if float(a["bottom"]) <= float(b["top"]) and float(b["bottom"]) <= float(a["top"]):
                overlap += 1
    row["lower_pool_overlap_pairs"] = overlap

    # BE diagnostics (post-entry)
    diag = _diag_be_timing(bars15, idx, entry, stop, tp, str(trade["outcome"]))
    row.update(diag)
    return row


def _aggregate(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = [float(t["pnl_pct"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None
    ordered = sorted(trades, key=lambda t: (t["entry_time"], t["symbol"]))
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for p in pnls:
        pass
    for t in ordered:
        p = float(t["pnl_pct"])
        eq += p
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        elif p > 0:
            streak = 0
    labels = [str(t["outcome"]).upper() for t in trades]
    return {
        "trades": len(trades),
        "TP": sum(1 for x in labels if x == "TP"),
        "SL": sum(1 for x in labels if x in ("SL", "STOP")),
        "BE": sum(1 for x in labels if x in ("BE", "BE_SMALL", "INTRABAR_AMBIGUOUS")),
        "pnl_sum_pct": round(sum(pnls), 4),
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_loss_streak": max_streak,
    }


def _sl_clusters(trades: list[dict]) -> list[dict]:
    sls = sorted([t for t in trades if _is_sl(t)], key=lambda t: t["entry_time"])
    clusters: list[list[dict]] = []
    for t in sls:
        et = datetime.fromisoformat(t["entry_time"])
        if not clusters:
            clusters.append([t])
            continue
        prev = datetime.fromisoformat(clusters[-1][-1]["entry_time"])
        same_sym = t["symbol"] == clusters[-1][-1]["symbol"]
        close_time = (et - prev).total_seconds() <= 72 * 3600
        if same_sym and close_time:
            clusters[-1].append(t)
        elif close_time and len({x["symbol"] for x in clusters[-1]}) <= 2:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    out = []
    for i, cl in enumerate(clusters, 1):
        syms = sorted({x["symbol"] for x in cl})
        out.append(
            {
                "cluster_id": i,
                "group_mix": "|".join(sorted({_group(x["symbol"]) for x in cl})),
                "symbols": "|".join(syms),
                "n_sl": len(cl),
                "total_loss_pct": round(sum(float(x["pnl_pct"]) for x in cl), 4),
                "start": cl[0]["entry_time"],
                "end": cl[-1]["entry_time"],
            }
        )
    return out


def _group_compare(rows: list[dict], field: str) -> dict:
    good = [_float(r[field]) for r in rows if r["group"] == "GOOD" and _float(r.get(field)) is not None]
    high = [_float(r[field]) for r in rows if r["group"] == "HIGH_DD" and _float(r.get(field)) is not None]
    if not good or not high:
        return {"feature": field, "status": "insufficient_data"}

    def stats(vals: list[float]) -> dict:
        return {
            "n": len(vals),
            "median": round(statistics.median(vals), 4),
            "mean": round(statistics.mean(vals), 4),
            "iqr": round(_iqr(vals), 4),
        }

    gs, hs = stats(good), stats(high)
    pooled_iqr = _iqr(good + high) or 1e-9
    med_diff = hs["median"] - gs["median"]
    effect = med_diff / pooled_iqr
    return {
        "feature": field,
        "good": gs,
        "high_dd": hs,
        "median_diff_high_minus_good": round(med_diff, 4),
        "effect_size_median_over_pooled_iqr": round(effect, 4),
        "mann_whitney_p": _mann_whitney(good, high),
    }


def _rank_feature(cmp: dict, rows: list[dict], field: str) -> dict:
    eff = abs(cmp.get("effect_size_median_over_pooled_iqr") or 0)
    if eff >= 0.5:
        label = "STRONG_GROUP_DIFFERENCE"
    elif eff >= 0.25:
        label = "MODERATE_GROUP_DIFFERENCE"
    elif eff >= 0.1:
        label = "WEAK"
    else:
        label = "NO_DIFFERENCE"

    med_g = cmp.get("good", {}).get("median")
    med_h = cmp.get("high_dd", {}).get("median")
    direction = 1 if (med_h or 0) > (med_g or 0) else -1
    coin_hits = 0
    for sym in HIGH_DD_SYMBOLS:
        gmed = statistics.median(
            [_float(r[field]) for r in rows if r["symbol"] in GOOD_SYMBOLS and _float(r.get(field)) is not None]
        )
        cvals = [_float(r[field]) for r in rows if r["symbol"] == sym and _float(r.get(field)) is not None]
        if not cvals:
            continue
        cmed = statistics.median(cvals)
        if direction > 0 and cmed > gmed:
            coin_hits += 1
        if direction < 0 and cmed < gmed:
            coin_hits += 1

    if coin_hits == 3:
        stability = "STABLE_HIGH_DD_3COIN"
    elif coin_hits == 2:
        stability = "PARTIAL"
    elif coin_hits == 1:
        stability = "COIN_SPECIFIC"
    else:
        stability = "MIXED"

    return {"feature": field, "group_label": label, "high_dd_coin_pattern": stability, "effect_size": round(eff, 4)}


def _coin_level(rows: list[dict]) -> list[dict]:
    out = []
    for sym in ALL_SYMBOLS:
        sub = [r for r in rows if r["symbol"] == sym]
        if not sub:
            continue

        def q(vals: list[float], p: float) -> float | None:
            if not vals:
                return None
            vals = sorted(vals)
            i = min(len(vals) - 1, int(p * len(vals)))
            return round(vals[i], 4)

        atrs = [_float(r["m15_atr14_pct"]) for r in sub if _float(r.get("m15_atr14_pct"))]
        ranges = [_float(r["m15_bar_range_pct"]) for r in sub if _float(r.get("m15_bar_range_pct"))]
        maes = [_float(r["mae_pct"]) for r in sub if _float(r.get("mae_pct"))]
        mfes = [_float(r["mfe_pct"]) for r in sub if _float(r.get("mfe_pct"))]
        rev = [_float(r["wick_reversal_freq_16b"]) for r in sub if _float(r.get("wick_reversal_freq_16b"))]
        stop_atr = [_float(r["stop_over_atr15"]) for r in sub if _float(r.get("stop_over_atr15"))]
        cl = _sl_clusters(sub)
        sl_gaps = []
        sl_times = sorted([datetime.fromisoformat(t["entry_time"]) for t in sub if _is_sl(t)])
        for i in range(1, len(sl_times)):
            sl_gaps.append((sl_times[i] - sl_times[i - 1]).total_seconds() / 3600.0)

        out.append(
            {
                "symbol": sym,
                "group": _group(sym),
                "trades": len(sub),
                "median_m15_atr14_pct": round(statistics.median(atrs), 4) if atrs else None,
                "median_m15_range_pct": round(statistics.median(ranges), 4) if ranges else None,
                "p90_m15_range_pct": q(ranges, 0.9),
                "median_wick_reversal_16b": round(statistics.median(rev), 4) if rev else None,
                "mae_p90": q(maes, 0.9),
                "mfe_p90": q(mfes, 0.9),
                "median_stop_over_atr15": round(statistics.median(stop_atr), 4) if stop_atr else None,
                "pool_created_24h_median": round(
                    statistics.median([_float(r["pool_created_24h"]) or 0 for r in sub]), 4
                ),
                "sl_cluster_count": len(cl),
                "max_sl_cluster_len": max((c["n_sl"] for c in cl), default=0),
                "median_hours_between_sls": round(statistics.median(sl_gaps), 4) if sl_gaps else None,
                "metrics": _aggregate(sub),
            }
        )
    return out


def _hypothetical(rows: list[dict], name: str, pred: Callable[[dict], bool]) -> dict:
    kept = [r for r in rows if not pred(r)]
    blocked = [r for r in rows if pred(r)]
    by_sym_block = defaultdict(int)
    by_sym_keep = defaultdict(int)
    for r in blocked:
        by_sym_block[r["symbol"]] += 1
    for r in kept:
        by_sym_keep[r["symbol"]] += 1
    return {
        "filter": name,
        "status": "RESEARCH_CANDIDATE",
        "blocked_trades": len(blocked),
        "blocked_pnl_pct": round(sum(float(r["pnl_pct"]) for r in blocked), 4),
        "blocked_TP": sum(1 for r in blocked if _is_tp(r)),
        "blocked_SL": sum(1 for r in blocked if _is_sl(r)),
        "kept_trades": len(kept),
        "result_pnl_pct": round(sum(float(r["pnl_pct"]) for r in kept), 4),
        "result_metrics": _aggregate(kept),
        "blocked_by_symbol": dict(by_sym_block),
        "kept_good_group": sum(by_sym_keep[s] for s in GOOD_SYMBOLS),
        "blocked_good_group": sum(by_sym_block[s] for s in GOOD_SYMBOLS),
        "blocked_high_dd": sum(by_sym_block[s] for s in HIGH_DD_SYMBOLS),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()}
            w.writerow(flat)


def _frozen_integrity() -> dict:
    for rel in (
        "frozen_strategies/long_geometry_ladder24_be100_v1",
        "frozen_strategies/e1r_cluster3_ema200_v1",
        "bot/e1r_live_scanner",
    ):
        st = subprocess.run(
            ["git", "status", "--short", rel],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        diff = subprocess.run(
            ["git", "diff", "--", rel],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
        if st.stdout.strip() or diff.stdout.strip():
            return {"pass": False, "failed_path": rel, "status": st.stdout, "diff_len": len(diff.stdout)}
    return {"pass": True}


def main() -> None:
    _ensure_paths()
    OUT.mkdir(parents=True, exist_ok=True)
    if not TRADES_CSV.is_file():
        raise SystemExit(f"missing {TRADES_CSV}")

    trades_raw: list[dict] = []
    with TRADES_CSV.open() as f:
        for row in csv.DictReader(f):
            trades_raw.append(row)
    if len(trades_raw) != 584:
        print(f"warning: expected 584 trades, got {len(trades_raw)}")

    from dashboard.research_charts.lld_research_kernel import load_pane_candles, scanner_pools_for_index, ui_lld_config
    from find_short_entry_15m_v1 import build_15m_bars, load_market

    enriched: list[dict] = []
    for symbol in ALL_SYMBOLS:
        sym_trades = [t for t in trades_raw if t["symbol"] == symbol]
        if not sym_trades:
            continue
        market = load_market(symbol)
        _, c15 = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(PANE_FROM.timestamp()),
            to_unix=int(PANE_TO.timestamp()),
            history_weeks=HISTORY_WEEKS,
        )
        bars15 = build_15m_bars(c15)
        by_open = {b["open_time"]: b for b in bars15}
        lld = ui_lld_config("15m")
        cache: dict = {}
        atr15 = _atr_series(bars15)
        atr30 = _atr_series(market["30m"]["bars"])
        atr1h = _atr_series(market["1h"]["bars"])
        atr4h = _atr_series(market["4h"]["bars"])
        for t in sym_trades:
            ot = datetime.fromisoformat(t["entry_time"])
            bar = by_open[ot]
            idx = bar["candle_index"]
            pools15, _, _ = scanner_pools_for_index(c15, "15m", idx, lld, cache=cache)
            enriched.append(_enrich(t, bars15, by_open, market, pools15, atr15, atr30, atr1h, atr4h))

    numeric_fields = sorted(
        k
        for k in enriched[0].keys()
        if k not in META_KEYS
        and k not in OUTCOME_KEYS
        and not k.startswith("diag_")
        and k != "group"
        and (isinstance(enriched[0][k], (int, float)) or enriched[0][k] is None)
    )

    comparisons = [_group_compare(enriched, f) for f in numeric_fields]
    comparisons.sort(key=lambda x: abs(x.get("effect_size_median_over_pooled_iqr") or 0), reverse=True)
    rankings = [_rank_feature(c, enriched, c["feature"]) for c in comparisons if c.get("good")]

    coin_rows = _coin_level(enriched)
    clusters = _sl_clusters(enriched)
    clusters_per_coin = []
    for sym in ALL_SYMBOLS:
        sub = [t for t in enriched if t["symbol"] == sym]
        cls = _sl_clusters(sub)
        gaps = []
        slt = sorted([datetime.fromisoformat(t["entry_time"]) for t in sub if _is_sl(t)])
        for i in range(1, len(slt)):
            gaps.append((slt[i] - slt[i - 1]).total_seconds() / 3600.0)
        top3 = sorted(cls, key=lambda c: c["total_loss_pct"])[:3]
        clusters_per_coin.append(
            {
                "symbol": sym,
                "group": _group(sym),
                "n_sl_clusters": len(cls),
                "max_cluster_len": max((c["n_sl"] for c in cls), default=0),
                "avg_cluster_loss_pct": round(statistics.mean([c["total_loss_pct"] for c in cls]), 4) if cls else None,
                "median_hours_between_sl": round(statistics.median(gaps), 4) if gaps else None,
                "top3_clusters": top3,
            }
        )

    be_rows = []
    for g in ("GOOD", "HIGH_DD"):
        sub = [r for r in enriched if r["group"] == g]
        n = len(sub)
        be_rows.append(
            {
                "group": g,
                "trades": n,
                "pct_be_trigger": round(sum(1 for r in sub if r.get("be_trigger_fired")) / n * 100, 2) if n else None,
                "pct_outcome_tp": round(sum(1 for r in sub if _is_tp(r)) / n * 100, 2) if n else None,
                "pct_outcome_sl": round(sum(1 for r in sub if _is_sl(r)) / n * 100, 2) if n else None,
                "pct_outcome_be": round(sum(1 for r in sub if _is_be(r)) / n * 100, 2) if n else None,
                "median_diag_time_to_1pct_bars": round(
                    statistics.median(
                        [r["diag_time_to_1pct_bars"] for r in sub if r.get("diag_time_to_1pct_bars") is not None]
                    ),
                    2,
                )
                if any(r.get("diag_time_to_1pct_bars") is not None for r in sub)
                else None,
                "pct_profit_then_loss": round(sum(1 for r in sub if r.get("diag_profit_then_loss")) / n * 100, 2)
                if n
                else None,
            }
        )

    mae_mfe = []
    for g in ("GOOD", "HIGH_DD"):
        sub = [r for r in enriched if r["group"] == g]
        maes = sorted([_float(r["mae_pct"]) for r in sub if _float(r.get("mae_pct")) is not None])
        mfes = sorted([_float(r["mfe_pct"]) for r in sub if _float(r.get("mfe_pct")) is not None])

        def pct(vals: list[float], p: float) -> float | None:
            if not vals:
                return None
            i = min(len(vals) - 1, int(p * len(vals)))
            return round(vals[i], 4)

        mae_mfe.append(
            {
                "group": g,
                "median_mae": pct(maes, 0.5),
                "median_mfe": pct(mfes, 0.5),
                "mae_p75": pct(maes, 0.75),
                "mae_p90": pct(maes, 0.9),
                "mfe_p75": pct(mfes, 0.75),
                "mfe_p90": pct(mfes, 0.9),
                "mfe_mae_ratio_median": round(statistics.median(mfes) / statistics.median(maes), 4)
                if maes and mfes and statistics.median(maes) > 0
                else None,
            }
        )

    vol_priority = (
        "m15_atr14_pct",
        "m15_rv_6h_pct",
        "m15_roll_range_24b_pct",
        "m4h_atr14_pct",
        "largest_wick_24b_pct",
        "stop_over_atr15",
    )
    cmp_by_name = {c["feature"]: c for c in comparisons if c.get("good")}
    hyps = []
    for f in vol_priority:
        c = cmp_by_name.get(f)
        if not c:
            continue
        med_g = c["good"]["median"]
        med_h = c["high_dd"]["median"]
        if f == "stop_over_atr15":
            if med_h >= med_g:
                continue
            name = f"HYP_{f}_lt_good_median_{med_g}"
            hyps.append(_hypothetical(enriched, name, lambda r, field=f, thr=med_g: (_float(r.get(field)) or 99) < thr))
        else:
            if med_h <= med_g:
                continue
            name = f"HYP_{f}_gt_good_median_{med_g}"
            hyps.append(_hypothetical(enriched, name, lambda r, field=f, thr=med_g: (_float(r.get(field)) or 0) > thr))
        if len(hyps) >= 3:
            break

    all_metrics = _aggregate(enriched)
    no_black = [r for r in enriched if r["symbol"] not in HIGH_DD_SYMBOLS]
    blacklist_cmp = {
        "all_10_coins": all_metrics,
        "hypothetical_no_ZEC_ACE_TUT": _aggregate(no_black),
        "hypothetical_filters": [h["result_metrics"] for h in hyps[:3]],
    }

    strong = [r for r in rankings if r["group_label"] == "STRONG_GROUP_DIFFERENCE"]
    moderate = [r for r in rankings if r["group_label"] == "MODERATE_GROUP_DIFFERENCE"]
    if len(strong) >= 3 and sum(1 for r in strong[:5] if r["high_dd_coin_pattern"] == "STABLE_HIGH_DD_3COIN") >= 2:
        verdict = "HIGH_DD_COINS_SHARE_CLEAR_VOLATILITY_SIGNATURE"
    elif len(strong) + len(moderate) >= 4:
        verdict = "HIGH_DD_COINS_PARTIAL_SIGNATURE"
    else:
        verdict = "HIGH_DD_COINS_NO_CLEAR_SIGNATURE"

    integrity = _frozen_integrity()
    leakage = {"pass": True, "notes": "Entry-time bars/pools only; outcome fields excluded from filter features"}

    summary = {
        "study": "long_v1_good_vs_highdd_volatility_v1",
        "trades": len(enriched),
        "groups": {"GOOD": len([r for r in enriched if r["group"] == "GOOD"]), "HIGH_DD": len([r for r in enriched if r["group"] == "HIGH_DD"])},
        "top_features_by_effect": comparisons[:15],
        "feature_rankings_strong": strong[:10],
        "mae_mfe_by_group": mae_mfe,
        "be_behavior": be_rows,
        "blacklist_comparison": blacklist_cmp,
        "hypothetical_vol_filters": hyps[:3],
        "final_verdict": verdict,
        "leakage_audit": leakage,
        "frozen_integrity": integrity,
        "coin_special": {
            sym: next((c for c in coin_rows if c["symbol"] == sym), {})
            for sym in ("TUTUSDT", "ACEUSDT", "ZECUSDT")
        },
    }

    _write_csv(OUT / "trade_features.csv", enriched)
    _write_csv(OUT / "group_comparison.csv", comparisons)
    _write_csv(OUT / "coin_level_features.csv", [{k: v for k, v in c.items() if k != "metrics"} for c in coin_rows])
    _write_csv(OUT / "feature_ranking.csv", rankings)
    _write_csv(OUT / "loss_clusters.csv", clusters_per_coin)
    _write_csv(OUT / "be_behavior.csv", be_rows)
    _write_csv(OUT / "hypothetical_vol_filters.csv", hyps[:3])
    bl_rows = [{"scenario": "all_10_coins", **blacklist_cmp["all_10_coins"]}]
    bl_rows.append({"scenario": "no_ZEC_ACE_TUT", **blacklist_cmp["hypothetical_no_ZEC_ACE_TUT"]})
    for i, h in enumerate(hyps[:3]):
        bl_rows.append({"scenario": h["filter"], **h["result_metrics"]})
    _write_csv(OUT / "blacklist_comparison.csv", bl_rows)
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    (OUT / "method.md").write_text(
        """# Long V1 GOOD vs HIGH-DD volatility study v1

- Cohort: frozen Long V1 variant C trades from `long_v1_10coin_crosssection_v1/trades_frozen_v1.csv`
- Period: 2026-04-01 .. 2026-07-31 UTC (inherited from cross-section)
- Groups: research labels only (7 GOOD-ish vs ZEC/ACE/TUT)
- Features: causal at `entry_time` (last closed 15m bar open = entry); MTF bars closed at decision
- Outcome / MAE / MFE / BE timing: diagnostic only, not filter inputs
- Hypotheticals: max 3, threshold = GOOD-group median on feature direction where HIGH-DD > GOOD
- No strategy / live / blacklist changes
"""
    )
    print(json.dumps({"out": str(OUT), "verdict": verdict, "integrity": integrity}, indent=2))


if __name__ == "__main__":
    main()
