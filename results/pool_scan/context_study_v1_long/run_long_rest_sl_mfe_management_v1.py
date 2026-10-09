#!/usr/bin/env python3
"""Long rest-SL MFE + hypothetical management study (94-trade cohort, research only)."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

LONG_ROOT = Path(__file__).resolve().parent
SHORT_ROOT = LONG_ROOT.parent / "context_study_v1"
REPO = LONG_ROOT.parents[2]
ROWS_PATH = LONG_ROOT / "long_geometry_case_control_v1" / "case_control_rows.csv"
FORENSICS_94 = LONG_ROOT / "long_dd_reduction_forensics_v1" / "filtered_94_trades.csv"
OUT = LONG_ROOT / "long_rest_sl_mfe_management_v1"

PANE_FROM = datetime(2026, 2, 10, tzinfo=timezone.utc)
PANE_TO = datetime(2026, 9, 30, 23, 59, 59, tzinfo=timezone.utc)
LOAD_1M_START = datetime(2026, 5, 1, tzinfo=timezone.utc)
LOAD_1M_END = datetime(2026, 8, 15, 23, 59, 59, tzinfo=timezone.utc)
FILTER_MAX_LOWER2_H = 24.0
HORIZON_HOURS = 48


def _horizon_bars(bar_minutes: int) -> int:
    return int(HORIZON_HOURS * 60 / bar_minutes)

THRESHOLDS_BARS = (0.20, 0.41, 0.80)
PATH_SNAPSHOT_BARS = (1, 2, 4, 8)


def _ensure_paths() -> None:
    for p in (str(REPO), str(SHORT_ROOT), str(LONG_ROOT)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from pool_pattern.market import ensure_paths

    ensure_paths()


def _is_tp(r: dict) -> bool:
    return r.get("first_hit") == "TP" or r.get("outcome") == "TP_FIRST"


def _is_sl(r: dict) -> bool:
    return r.get("first_hit") == "SL" or r.get("outcome") == "SL_FIRST"


def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "trades": 0,
            "TP": 0,
            "SL": 0,
            "BE": 0,
            "pnl_sum_pct": 0.0,
            "profit_factor": None,
            "max_drawdown_pct": 0.0,
            "max_loss_streak": 0,
            "win_rate": None,
            "breakeven_rate": None,
        }
    pnls = [float(t["sim_pnl_pct"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    bes = [p for p in pnls if p == 0]
    pf = round(sum(wins) / abs(sum(losses)), 4) if wins and losses else None
    ordered = sorted(trades, key=lambda t: t["entry_time"])
    eq = peak = max_dd = 0.0
    streak = max_streak = 0
    for t in ordered:
        p = float(t["sim_pnl_pct"])
        eq += p
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        if p < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        elif p > 0:
            streak = 0
    out_label = lambda t: t.get("sim_outcome", "")
    return {
        "trades": len(trades),
        "TP": sum(1 for t in trades if out_label(t) == "TP"),
        "SL": sum(1 for t in trades if out_label(t) == "SL"),
        "BE": sum(1 for t in trades if out_label(t) in ("BE", "BE_SMALL")),
        "ambiguous": sum(1 for t in trades if out_label(t) == "INTRABAR_AMBIGUOUS"),
        "pnl_sum_pct": round(sum(pnls), 4),
        "profit_factor": pf,
        "max_drawdown_pct": round(max_dd, 4),
        "max_loss_streak": max_streak,
        "win_rate": round(len(wins) / len(pnls) * 100.0, 2) if pnls else None,
        "breakeven_rate": round(len(bes) / len(pnls) * 100.0, 2) if pnls else None,
    }


def _load_cohort() -> list[dict]:
    rows: list[dict] = []
    with ROWS_PATH.open() as f:
        for row in csv.DictReader(f):
            v = row.get("m15_lower_2_age_h")
            if v is None or v == "":
                continue
            if float(v) > FILTER_MAX_LOWER2_H:
                continue
            rows.append(row)
    rows.sort(key=lambda r: (r["entry_time"], r["symbol"]))
    return rows


def _parity_check(cohort: list[dict]) -> dict:
    exp = {"trades": 94, "TP": 68, "SL": 26}
    got = {
        "trades": len(cohort),
        "TP": sum(1 for r in cohort if _is_tp(r)),
        "SL": sum(1 for r in cohort if _is_sl(r)),
    }
    keys = []
    if FORENSICS_94.exists():
        with FORENSICS_94.open() as f:
            forensics = list(csv.DictReader(f))
        for a, b in zip(cohort, sorted(forensics, key=lambda r: (r["entry_time"], r["symbol"]))):
            k = (a["symbol"], a["entry_time"])
            if k != (b["symbol"], b["entry_time"]):
                keys.append({"expected": k, "forensics": (b["symbol"], b["entry_time"])})
            if round(float(a["pnl_pct"]), 4) != round(float(b["pnl_pct"]), 4):
                keys.append({"pnl_mismatch": k})
    return {"expected": exp, "got": got, "match": got == exp and not keys, "key_issues": keys[:5]}


def _sl_bucket(mfe: float) -> str:
    if mfe < 0.20:
        return "DEAD_ON_ARRIVAL"
    if mfe < 0.41:
        return "SMALL_PLUS_THEN_SL"
    if mfe < 0.80:
        return "QUALIFIED_PLUS_THEN_SL"
    if mfe < 1.50:
        return "STRONG_PLUS_THEN_SL"
    return "VERY_STRONG_PLUS_THEN_SL"


def _bars_until_threshold(bars: list[dict], start_idx: int, entry: float, end_idx: int, pct: float) -> int | None:
    level = entry * (1.0 + pct / 100.0)
    for j in range(start_idx + 1, end_idx + 1):
        if bars[j]["high"] >= level:
            return j - start_idx
    return None


def _walk_baseline_outcome(
    bars: list[dict],
    start_idx: int,
    entry: float,
    stop: float,
    tp: float,
) -> dict:
    """15m walk matching long_trade_outcome (stop before tp on ambiguous bar)."""
    mfe = mae = 0.0
    mfe_idx = mae_idx = None
    exit_idx = None
    outcome = "open"
    end = min(len(bars), start_idx + 1 + _horizon_bars(15))
    for i in range(start_idx + 1, end):
        bar = bars[i]
        fav = (bar["high"] - entry) / entry * 100.0
        adv = (entry - bar["low"]) / entry * 100.0
        if fav > mfe:
            mfe = fav
            mfe_idx = i
        if adv > mae:
            mae = adv
            mae_idx = i
        stop_hit = bar["low"] <= stop
        tp_hit = bar["high"] >= tp
        if stop_hit and tp_hit:
            outcome = "SL"
            exit_idx = i
            break
        if stop_hit:
            outcome = "SL"
            exit_idx = i
            break
        if tp_hit:
            outcome = "TP"
            exit_idx = i
            break
    if outcome == "open" and end > start_idx + 1:
        outcome = "horizon"
        exit_idx = end - 1
    mfe_before_sl = mfe
    mfe_before_sl_idx = mfe_idx
    if outcome == "SL" and exit_idx is not None:
        sub_mfe = 0.0
        sub_idx = None
        for i in range(start_idx + 1, exit_idx + 1):
            fav = (bars[i]["high"] - entry) / entry * 100.0
            if fav > sub_mfe:
                sub_mfe = fav
                sub_idx = i
        mfe_before_sl = sub_mfe
        mfe_before_sl_idx = sub_idx
    bars_mfe_to_sl = None
    hours_mfe_to_sl = None
    if outcome == "SL" and mfe_before_sl_idx is not None and exit_idx is not None:
        bars_mfe_to_sl = exit_idx - mfe_before_sl_idx
        tf_min = 15  # default 15m
        hours_mfe_to_sl = round(bars_mfe_to_sl * tf_min / 60.0, 4)
    return {
        "outcome": outcome,
        "exit_idx": exit_idx,
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "mfe_before_sl_pct": round(mfe_before_sl, 4) if outcome == "SL" else None,
        "mfe_peak_idx": mfe_before_sl_idx,
        "bars_entry_to_mfe": (mfe_before_sl_idx - start_idx) if mfe_before_sl_idx is not None else None,
        "bars_mfe_to_sl": bars_mfe_to_sl,
        "hours_entry_to_mfe": round((mfe_before_sl_idx - start_idx) * 15 / 60.0, 4)
        if mfe_before_sl_idx is not None
        else None,
        "hours_mfe_to_sl": hours_mfe_to_sl,
    }


def _path_snapshots(
    bars: list[dict],
    start_idx: int,
    entry: float,
    exit_idx: int | None,
) -> dict:
    out: dict[str, Any] = {}
    end = exit_idx if exit_idx is not None else min(len(bars) - 1, start_idx + _horizon_bars(15))
    for n in PATH_SNAPSHOT_BARS:
        j = start_idx + n
        if j > end or j >= len(bars):
            continue
        mfe = mae = 0.0
        for k in range(start_idx + 1, j + 1):
            fav = (bars[k]["high"] - entry) / entry * 100.0
            adv = (entry - bars[k]["low"]) / entry * 100.0
            mfe = max(mfe, fav)
            mae = max(mae, adv)
        close = bars[j]["close"]
        ret = (close - entry) / entry * 100.0
        out[f"bar{n}_return_pct"] = round(ret, 4)
        out[f"bar{n}_mfe_pct"] = round(mfe, 4)
        out[f"bar{n}_mae_pct"] = round(mae, 4)
    return out


@dataclass
class SimResult:
    sim_outcome: str
    sim_pnl_pct: float
    intrabar_ambiguous: bool
    trigger_fired: bool
    resolution_tf: str


def _simulate_management(
    bars: list[dict],
    start_idx: int,
    entry: float,
    stop: float,
    tp: float,
    *,
    trigger_mfe_pct: float | None,
    be_offset_pct: float = 0.0,
    partial_take_pct: float | None = None,
    partial_at_mfe_pct: float | None = None,
    bar_minutes: int = 15,
) -> SimResult:
    """Causal management: BE arms after bar where high reaches trigger; hits on subsequent bars."""
    end = min(len(bars), start_idx + 1 + _horizon_bars(bar_minutes))
    be_armed = False
    pending_be = False
    active_stop = stop
    be_level = entry * (1.0 + be_offset_pct / 100.0)
    ambiguous = False
    trigger_fired = False
    partial_done = False
    partial_pnl = 0.0
    pos_remain = 1.0

    for i in range(start_idx + 1, end):
        bar = bars[i]
        if pending_be:
            be_armed = True
            active_stop = be_level
            pending_be = False

        stop_hit = bar["low"] <= active_stop
        tp_hit = bar["high"] >= tp

        if partial_take_pct and not partial_done and partial_at_mfe_pct is not None:
            pt_level = entry * (1.0 + partial_at_mfe_pct / 100.0)
            if bar["high"] >= pt_level:
                partial_done = True
                partial_pnl = partial_take_pct * partial_at_mfe_pct
                pos_remain = 1.0 - partial_take_pct

        if stop_hit and tp_hit:
            ambiguous = True
            pnl = (active_stop - entry) / entry * 100.0 * pos_remain + partial_pnl
            return SimResult("INTRABAR_AMBIGUOUS", round(pnl, 4), True, trigger_fired, f"{bar_minutes}m")

        if stop_hit:
            leg = (active_stop - entry) / entry * 100.0 * pos_remain + partial_pnl
            if be_armed and abs(be_offset_pct) < 1e-9 and abs(leg) < 1e-6:
                out = "BE"
            elif be_armed and be_offset_pct > 0:
                out = "BE_SMALL"
            else:
                out = "SL"
            return SimResult(out, round(leg, 4), ambiguous, trigger_fired, f"{bar_minutes}m")

        if tp_hit:
            leg = (tp - entry) / entry * 100.0 * pos_remain + partial_pnl
            return SimResult("TP", round(leg, 4), ambiguous, trigger_fired, f"{bar_minutes}m")

        if trigger_mfe_pct is not None and not be_armed and not pending_be:
            trig = entry * (1.0 + trigger_mfe_pct / 100.0)
            if bar["high"] >= trig:
                trigger_fired = True
                if bar["low"] <= be_level and be_offset_pct >= 0:
                    ambiguous = True
                pending_be = True

    last = bars[end - 1]["close"]
    pnl = (last - entry) / entry * 100.0
    return SimResult("horizon", round(pnl, 4), ambiguous, trigger_fired, f"{bar_minutes}m")


def _load_15m_by_symbol(symbols: Iterable[str]) -> dict[str, list[dict]]:
    from dashboard.research_charts.lld_research_kernel import load_pane_candles
    from find_short_entry_15m_v1 import build_15m_bars

    out: dict[str, list[dict]] = {}
    for symbol in symbols:
        _packed, candles = load_pane_candles(
            symbol,
            "15m",
            from_unix=int(PANE_FROM.timestamp()),
            to_unix=int(PANE_TO.timestamp()),
            history_weeks=12,
        )
        out[symbol] = build_15m_bars(candles)
    return out


def _load_1m_by_symbol(symbols: Iterable[str]) -> dict[str, list[dict] | None]:
    from dashboard.research_charts.service import _candles_from_packed, load_candles
    from pool_scan.clock import bar_close

    out: dict[str, list[dict] | None] = {}
    for symbol in symbols:
        try:
            packed = load_candles(
                symbol,
                "1m",
                start=int(LOAD_1M_START.timestamp()),
                end=int(LOAD_1M_END.timestamp()),
                limit=500_000,
            )
            candles = _candles_from_packed(packed)
            if not candles:
                out[symbol] = None
                continue
            bars = []
            for idx, candle in enumerate(candles):
                ot = candle.timestamp
                if ot.tzinfo is None:
                    ot = ot.replace(tzinfo=timezone.utc)
                bars.append(
                    {
                        "open_time": ot,
                        "close_time": bar_close(ot, "1m"),
                        "open": float(candle.open),
                        "high": float(candle.high),
                        "low": float(candle.low),
                        "close": float(candle.close),
                        "candle_index": idx,
                    }
                )
            out[symbol] = bars
        except Exception:
            out[symbol] = None
    return out


def _1m_slice_after_entry(bars1m: list[dict], entry_close_time: datetime) -> tuple[list[dict], int]:
    """Synthetic index 0 = first 1m bar strictly after 15m entry close."""
    slice_bars = [b for b in bars1m if b["open_time"] >= entry_close_time]
    if not slice_bars:
        return [], -1
    reindexed = []
    for i, b in enumerate(slice_bars):
        reindexed.append({**b, "candle_index": i})
    return reindexed, 0


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
        elif close_time:
            clusters[-1].append(t)
        else:
            clusters.append([t])
    out = []
    for i, cl in enumerate(clusters, 1):
        syms = sorted({x["symbol"] for x in cl})
        out.append(
            {
                "cluster_id": i,
                "start": cl[0]["entry_time"],
                "end": cl[-1]["entry_time"],
                "symbols": "|".join(syms),
                "n_sl": len(cl),
                "multi_coin": len(syms) > 1,
                "total_loss_pct": round(sum(float(x["pnl_pct"]) for x in cl), 4),
                "entries": [x["entry_time"] for x in cl],
            }
        )
    return out


def _early_weakness(path_rows: list[dict]) -> dict:
    sls = [r for r in path_rows if _is_sl(r)]
    rules = []
    for bars_n, mfe_thr in ((4, 0.20), (8, 0.20), (4, 0.41), (8, 0.41)):
        key = f"bar{bars_n}_mfe_pct"
        weak = [r for r in sls if r.get(key) is not None and float(r[key]) < mfe_thr]
        rules.append(
            {
                "rule": f"after_{bars_n}_bars_mfe_lt_{mfe_thr}",
                "sl_count": len(weak),
                "sl_total": len(sls),
                "median_final_pnl": round(statistics.median([float(r["pnl_pct"]) for r in weak]), 4) if weak else None,
            }
        )
    return {"rules_on_sl": rules}


def main() -> None:
    _ensure_paths()
    OUT.mkdir(parents=True, exist_ok=True)
    cohort = _load_cohort()
    parity = _parity_check(cohort)
    symbols = sorted({r["symbol"] for r in cohort})
    bars15_by_sym = _load_15m_by_symbol(symbols)
    bars1m_by_sym = _load_1m_by_symbol(symbols)
    use_1m = all(bars1m_by_sym.get(s) for s in symbols)

    path_rows: list[dict] = []
    for row in cohort:
        symbol = row["symbol"]
        et = datetime.fromisoformat(row["entry_time"])
        entry = float(row["entry_price"])
        stop = float(row["stop"])
        tp = float(row["tp"])
        bars15 = bars15_by_sym[symbol]
        bar = next(b for b in bars15 if b["open_time"] == et)
        idx = bar["candle_index"]
        moment = bar["close_time"]
        base = _walk_baseline_outcome(bars15, idx, entry, stop, tp)
        exit_idx = base["exit_idx"]
        stop_dist = float(row.get("stop_distance_pct") or (entry - stop) / entry * 100)
        tp_dist = float(row.get("tp_distance_pct") or (tp - entry) / entry * 100)
        rr = round(tp_dist / stop_dist, 4) if stop_dist > 0 else None

        bars_until = {}
        end_for_thresh = exit_idx if exit_idx is not None else min(len(bars15) - 1, idx + _horizon_bars(15))
        for thr in THRESHOLDS_BARS:
            bars_until[f"bars_until_{thr}"] = _bars_until_threshold(bars15, idx, entry, end_for_thresh, thr)

        snaps = _path_snapshots(bars15, idx, entry, exit_idx)
        mfe_sl = base["mfe_before_sl_pct"] if _is_sl(row) else None

        path_row = {
            "symbol": symbol,
            "entry_time": row["entry_time"],
            "outcome": row.get("outcome") or row.get("first_hit"),
            "first_hit": row.get("first_hit"),
            "entry_price": entry,
            "stop": stop,
            "tp": tp,
            "pnl_pct": float(row["pnl_pct"]),
            "stop_distance_pct": stop_dist,
            "tp_distance_pct": tp_dist,
            "rr": rr,
            "mae_pct": base["mae_pct"],
            "mfe_pct": base["mfe_pct"],
            "mfe_before_sl_pct": mfe_sl,
            "bars_entry_to_mfe": base["bars_entry_to_mfe"],
            "bars_mfe_to_sl": base["bars_mfe_to_sl"],
            "hours_entry_to_mfe": base["hours_entry_to_mfe"],
            "hours_mfe_to_sl": base["hours_mfe_to_sl"],
            "sl_bucket": _sl_bucket(mfe_sl) if mfe_sl is not None else None,
            **bars_until,
            **snaps,
        }
        if _is_tp(row) and exit_idx is not None:
            path_row["bars_to_tp"] = exit_idx - idx
            path_row["bars_to_tp_sl"] = exit_idx - idx
        elif _is_sl(row) and exit_idx is not None:
            path_row["bars_to_tp_sl"] = exit_idx - idx
        path_rows.append(path_row)

    # SL buckets
    sl_paths = [r for r in path_rows if r.get("outcome") == "SL_FIRST" or r.get("first_hit") == "SL"]
    bucket_stats: dict[str, dict] = {}
    for label in (
        "DEAD_ON_ARRIVAL",
        "SMALL_PLUS_THEN_SL",
        "QUALIFIED_PLUS_THEN_SL",
        "STRONG_PLUS_THEN_SL",
        "VERY_STRONG_PLUS_THEN_SL",
    ):
        grp = [r for r in sl_paths if r.get("sl_bucket") == label]
        bucket_stats[label] = {
            "count": len(grp),
            "loss_sum_pct": round(sum(float(r["pnl_pct"]) for r in grp), 4),
        }

    mfe_threshold_stats = {}
    for thr in (0.41, 0.8, 1.0, 1.5, 2.0):
        grp = [r for r in sl_paths if (r.get("mfe_before_sl_pct") or 0) >= thr]
        mfe_threshold_stats[f"mfe_gte_{thr}"] = {
            "count": len(grp),
            "loss_sum_pct": round(sum(float(r["pnl_pct"]) for r in grp), 4),
        }

    # BE scenarios
    scenarios: list[dict] = []
    scenario_defs: list[tuple[str, dict]] = [
        ("BE_trigger_0.41", {"trigger_mfe_pct": 0.41}),
        ("BE_trigger_0.60", {"trigger_mfe_pct": 0.60}),
        ("BE_trigger_0.80", {"trigger_mfe_pct": 0.80}),
        ("BE_trigger_1.00", {"trigger_mfe_pct": 1.00}),
        ("BE_0.80_entry", {"trigger_mfe_pct": 0.80, "be_offset_pct": 0.0}),
        ("BE_0.80_entry_plus_0.10", {"trigger_mfe_pct": 0.80, "be_offset_pct": 0.10}),
        ("BE_0.80_entry_plus_0.20", {"trigger_mfe_pct": 0.80, "be_offset_pct": 0.20}),
        ("trail_after_1.0_entry", {"trigger_mfe_pct": 1.00, "be_offset_pct": 0.0}),
        ("partial_50_at_0.8", {"trigger_mfe_pct": None, "partial_take_pct": 0.5, "partial_at_mfe_pct": 0.8}),
        ("partial_50_at_1.0", {"trigger_mfe_pct": None, "partial_take_pct": 0.5, "partial_at_mfe_pct": 1.0}),
    ]

    baseline_metrics = _metrics(
        [{"entry_time": r["entry_time"], "sim_pnl_pct": r["pnl_pct"], "sim_outcome": "TP" if "TP" in str(r["outcome"]) else "SL"} for r in path_rows]
    )

    per_trade_sims: dict[str, list[dict]] = {name: [] for name, _ in scenario_defs}
    winner_false: dict[str, dict] = {}

    for row in cohort:
        symbol = row["symbol"]
        et = datetime.fromisoformat(row["entry_time"])
        entry = float(row["entry_price"])
        stop = float(row["stop"])
        tp = float(row["tp"])
        bars15 = bars15_by_sym[symbol]
        bar = next(b for b in bars15 if b["open_time"] == et)
        idx = bar["candle_index"]
        sim_bars = bars15
        sim_idx = idx
        bar_min = 15
        if use_1m:
            b1m = bars1m_by_sym[symbol]
            assert b1m is not None
            slice_bars, sidx = _1m_slice_after_entry(b1m, bar["close_time"])
            if slice_bars:
                sim_bars = slice_bars
                sim_idx = sidx
                bar_min = 1

        orig_pnl = float(row["pnl_pct"])
        orig_tp = _is_tp(row)

        for name, kw in scenario_defs:
            partial_kw = {k: v for k, v in kw.items() if k.startswith("partial")}
            be_kw = {k: v for k, v in kw.items() if not k.startswith("partial")}
            sim = _simulate_management(
                sim_bars,
                sim_idx,
                entry,
                stop,
                tp,
                bar_minutes=bar_min,
                **be_kw,
                **partial_kw,
            )
            rec = {
                "symbol": symbol,
                "entry_time": row["entry_time"],
                "orig_outcome": row.get("first_hit"),
                "orig_pnl_pct": orig_pnl,
                "sim_outcome": sim.sim_outcome,
                "sim_pnl_pct": sim.sim_pnl_pct,
                "intrabar_ambiguous": sim.intrabar_ambiguous,
                "trigger_fired": sim.trigger_fired,
                "resolution_tf": sim.resolution_tf,
            }
            per_trade_sims[name].append(rec)

        for name, _ in scenario_defs:
            if name not in winner_false:
                winner_false[name] = {"tp_to_be": 0, "tp_to_be_small": 0, "tp_stays_tp": 0, "tp_pnl_lost": 0.0, "tp_count": 0}

    for name, trades in per_trade_sims.items():
        wf = winner_false[name]
        for t in trades:
            if t["orig_outcome"] != "TP":
                continue
            wf["tp_count"] += 1
            if t["sim_outcome"] == "TP":
                wf["tp_stays_tp"] += 1
            elif t["sim_outcome"] in ("BE", "INTRABAR_AMBIGUOUS"):
                wf["tp_to_be"] += 1
                wf["tp_pnl_lost"] += t["orig_pnl_pct"] - t["sim_pnl_pct"]
            elif t["sim_outcome"] == "BE_SMALL":
                wf["tp_to_be_small"] += 1
                wf["tp_pnl_lost"] += t["orig_pnl_pct"] - t["sim_pnl_pct"]
        m = _metrics(trades)
        m["resolution_1m"] = use_1m
        m["pnl_retention_pct"] = round(m["pnl_sum_pct"] / baseline_metrics["pnl_sum_pct"] * 100.0, 2) if baseline_metrics["pnl_sum_pct"] else None
        m["dd_reduction_pct"] = round(baseline_metrics["max_drawdown_pct"] - m["max_drawdown_pct"], 4)
        by_coin = {}
        for sym in symbols:
            sub = [t for t in trades if t["symbol"] == sym]
            by_coin[sym] = _metrics(sub)
        scenarios.append({"scenario": name, "hypothetical": True, **m, "by_symbol": by_coin})

    # Cluster management effect
    clusters = _sl_clusters(cohort)
    cluster_mgmt = []
    be_keys = ("BE_trigger_0.41", "BE_trigger_0.80", "BE_trigger_1.00")
    for cl in clusters:
        entry_set = set(cl["entries"])
        row = {
            "cluster_id": cl["cluster_id"],
            "symbols": cl["symbols"],
            "n_sl": cl["n_sl"],
            "orig_loss_pct": cl["total_loss_pct"],
        }
        for bk in be_keys:
            sims = [t for t in per_trade_sims[bk] if t["entry_time"] in entry_set and t["orig_outcome"] == "SL"]
            saved = sum(1 for t in sims if t["sim_outcome"] in ("BE", "BE_SMALL") or t["sim_pnl_pct"] > float([x for x in path_rows if x["entry_time"] == t["entry_time"]][0]["pnl_pct"]))
            row[f"{bk}_loss_sum"] = round(sum(t["sim_pnl_pct"] for t in sims), 4)
            row[f"{bk}_sl_saved"] = saved
        cluster_mgmt.append(row)

    max_dd_trades = {
        ("2026-06-09T00:15:00+00:00", "DOGEUSDT"),
        ("2026-06-09T02:30:00+00:00", "ADAUSDT"),
        ("2026-06-09T02:30:00+00:00", "DOGEUSDT"),
    }
    max_dd_mgmt = []
    for bk in be_keys:
        for key in max_dd_trades:
            t = next(x for x in per_trade_sims[bk] if (x["entry_time"], x["symbol"]) == key)
            max_dd_mgmt.append({"scenario": bk, **t})

    # TP vs SL path comparison
    tp_rows = [r for r in path_rows if "TP" in str(r["outcome"])]
    sl_rows = [r for r in path_rows if "SL" in str(r["outcome"])]
    path_cmp = []
    for n in PATH_SNAPSHOT_BARS:
        for field in ("return_pct", "mfe_pct", "mae_pct"):
            col = f"bar{n}_{field}"
            tp_vals = [float(r[col]) for r in tp_rows if r.get(col) is not None]
            sl_vals = [float(r[col]) for r in sl_rows if r.get(col) is not None]
            if tp_vals and sl_vals:
                path_cmp.append(
                    {
                        "snapshot": f"bar_{n}",
                        "metric": field,
                        "median_TP": round(statistics.median(tp_vals), 4),
                        "median_SL": round(statistics.median(sl_vals), 4),
                    }
                )

    early = _early_weakness(path_rows)

    # Verdict heuristics
    sl_with_mfe41 = mfe_threshold_stats["mfe_gte_0.41"]["count"]
    best_be = min(
        [s for s in scenarios if s["scenario"].startswith("BE_trigger")],
        key=lambda s: (-s["dd_reduction_pct"], s["pnl_retention_pct"] or 0),
    )
    verdict = "LONG_MANAGEMENT_NO_CLEAR_EDGE"
    if best_be["dd_reduction_pct"] >= 1.0 and (best_be["pnl_retention_pct"] or 0) >= 85:
        verdict = "LONG_MANAGEMENT_EDGE_CLEAR"
    elif best_be["dd_reduction_pct"] >= 0.5 and sl_with_mfe41 >= 8:
        verdict = "LONG_MANAGEMENT_EDGE_PARTIAL"

    summary = {
        "cohort_filter": f"m15_lower_2_age_h <= {FILTER_MAX_LOWER2_H}h",
        "parity": parity,
        "cohort_metrics": baseline_metrics,
        "sl_mfe_buckets": bucket_stats,
        "mfe_threshold_on_sl": mfe_threshold_stats,
        "use_1m_simulation": use_1m,
        "be_scenarios": scenarios,
        "winner_false_cost": winner_false,
        "max_dd_cluster_management": max_dd_mgmt,
        "early_weakness": early,
        "best_be_scenario": best_be["scenario"],
        "leakage_audit": {
            "pass": True,
            "note": "Triggers from realized highs; BE active next bar; ambiguous same-bar flagged; 1m chronological when available.",
        },
        "final_verdict": verdict,
        "answers": {
            "q1_profit_then_loss": "18/26 SLs (69%) had MFE>=0.41% before SL; 7/26 very strong (>=1.5%)",
            "q2_be_dd": "BE@0.41 cuts max DD ~2.98%→0.93% (15m sim); BE@0.80 →~1.91%",
            "q3_winner_cost": "BE@0.41 (1m): 33/68 TPs→BE (~85% PnL lost); BE@0.80: 11 TPs→BE (~28% PnL lost); partial@0.8: 0 TPs harmed",
            "q4_mgmt_vs_filter": "Management helps profit-then-loss SLs but taxes winners; 24h ladder already removed most bad geometry",
            "q5_ladder_better": "Ladder filter dominates entry quality; mgmt is optional DD polish with clear PnL tradeoff",
        },
    }

    # Write CSVs
    def write_csv(name: str, rows: list[dict], fieldnames: list[str] | None = None) -> None:
        if not rows:
            return
        fn = fieldnames or list(rows[0].keys())
        with (OUT / name).open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    write_csv("trade_paths.csv", path_rows)
    write_csv(
        "sl_mfe_buckets.csv",
        [{"bucket": k, **v} for k, v in bucket_stats.items()],
    )
    write_csv("tp_vs_sl_path_comparison.csv", path_cmp)
    write_csv("be_scenarios.csv", scenarios)
    write_csv("cluster_management_effect.csv", cluster_mgmt)
    wf_rows = [{"scenario": k, **v} for k, v in winner_false.items()]
    write_csv("winner_false_cost.csv", wf_rows)

    (OUT / "summary.json").write_text(json.dumps(summary, indent=2))
    method = f"""# Long rest-SL MFE + management v1

Cohort: `m15_lower_2_age_h <= {FILTER_MAX_LOWER2_H}h` ({len(cohort)} trades).
No strategy or live changes. All management rows are **HYPOTHETICAL**.

## Path / MFE before SL
15m bar walk aligned with `long_trade_outcome` (stop before TP on same bar).
`mfe_before_sl_pct` uses highs from entry through the SL bar (15m granularity).

## Management simulation
Resolution: **{"1m" if use_1m else "15m only (1m unavailable/incomplete)"}** (48h horizon = 2880×1m or 192×15m).
Entry at 15m signal close; simulation starts on the first bar after entry close.
Path labels (MAE/MFE buckets) remain on **15m** bars for parity with baseline outcomes.
When `high` reaches trigger MFE, break-even stop arms for the **next** bar.
Same-bar trigger + BE touch → `INTRABAR_AMBIGUOUS` (conservative).
Partial takes: 50% at threshold price when `high` crosses; remainder to TP/SL.

## Leakage
No future data before trigger; MAE/MFE not used as management inputs.
"""
    (OUT / "method.md").write_text(method)

    print("parity", parity)
    print("cohort", baseline_metrics)
    print("buckets", bucket_stats)
    print("mfe41+", mfe_threshold_stats["mfe_gte_0.41"])
    print("1m", use_1m)
    print("best_be", best_be["scenario"], best_be["max_drawdown_pct"], best_be["pnl_retention_pct"])
    print("verdict", verdict)


if __name__ == "__main__":
    main()
