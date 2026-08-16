#!/usr/bin/env python3
"""AUDIT_SYMBOL_DIRECTION_EDGE_STABILITY — research-only, no DB writes.

15m BASELINE_IMMEDIATE GLOBAL_FROZEN_TIER_A across all available symbols.
No filters / size changes / threshold tuning.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.live.control_api import _signal_row_to_api  # noqa: E402
from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.research.one_m_entry_timing.timing import (  # noqa: E402
    _prepare_1m,
    _simulate_outcome,
    _utc,
)
from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE  # noqa: E402

OUT = ROOT / "results" / "symbol_direction_edge_audit"
AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
SIGNAL_TF = "15m"
FEE = float(PRIMARY_FEE)
LOOKBACK_DAYS = 60
LOOKAHEAD_VIOLATIONS = 0
# Counterfactual: only combos with closed >= this and clearly weak
BLOCK_MIN_CLOSED = 10


def _iso(ts: Any | None) -> str | None:
    if ts is None:
        return None
    return _utc(ts).isoformat().replace("+00:00", "Z")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def sample_flag(n: int) -> str:
    if n < 10:
        return "TINY_SAMPLE"
    if n < 30:
        return "SMALL_SAMPLE"
    if n < 100:
        return "MODERATE_SAMPLE"
    return "BETTER_SAMPLE"


def equity_dd_and_streaks(nets: list[float], results: list[str], exit_reasons: list[str | None]) -> dict[str, Any]:
    if not nets:
        return {
            "max_drawdown": 0.0,
            "longest_losing_streak": 0,
            "max_consecutive_SL": 0,
            "streaks_2plus_SL": 0,
            "streaks_3plus_SL": 0,
            "streaks_5plus_SL": 0,
            "worst_5_trade_seq": None,
            "worst_10_trade_seq": None,
        }
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_dd = float(dd.min())

    lose_streak = longest_lose = 0
    for r in results:
        if r == "LOSS":
            lose_streak += 1
            longest_lose = max(longest_lose, lose_streak)
        else:
            lose_streak = 0

    # SL streaks from exit_reason
    sl_streak = max_sl = 0
    counts = {2: 0, 3: 0, 5: 0}
    i = 0
    while i < len(exit_reasons):
        if exit_reasons[i] == "SL":
            j = i
            while j < len(exit_reasons) and exit_reasons[j] == "SL":
                j += 1
            length = j - i
            max_sl = max(max_sl, length)
            if length >= 2:
                counts[2] += 1
            if length >= 3:
                counts[3] += 1
            if length >= 5:
                counts[5] += 1
            i = j
        else:
            i += 1

    def worst_k(k: int) -> float | None:
        if len(nets) < k:
            return None
        return float(min(sum(nets[i : i + k]) for i in range(len(nets) - k + 1)))

    return {
        "max_drawdown": max_dd,
        "longest_losing_streak": int(longest_lose),
        "max_consecutive_SL": int(max_sl),
        "streaks_2plus_SL": counts[2],
        "streaks_3plus_SL": counts[3],
        "streaks_5plus_SL": counts[5],
        "worst_5_trade_seq": worst_k(5),
        "worst_10_trade_seq": worst_k(10),
    }


def summarize(rows: list[dict], *, label: str = "") -> dict[str, Any]:
    wins = [r for r in rows if r["result"] == "WIN"]
    losses = [r for r in rows if r["result"] == "LOSS"]
    opens = [r for r in rows if r["result"] == "OPEN"]
    closed = wins + losses
    n_c = len(closed)
    nets = [float(r["pnl"]) - FEE for r in closed]
    gross = [float(r["pnl"]) for r in closed]
    maes = [float(r["mae"]) for r in closed if r.get("mae") is not None]
    mfes = [float(r["mfe"]) for r in closed if r.get("mfe") is not None]
    tp = sum(1 for r in closed if r.get("exit_reason") == "TP")
    sl = sum(1 for r in closed if r.get("exit_reason") == "SL")
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    pf = (gp / gl) if gl > 0 else (None if gp == 0 else float("inf"))
    eq = equity_dd_and_streaks(
        nets,
        [r["result"] for r in closed],
        [r.get("exit_reason") for r in closed],
    )
    net = float(sum(nets)) if nets else 0.0
    return {
        "label": label,
        "signals": len(rows),
        "closed": n_c,
        "open": len(opens),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": (100.0 * len(wins) / n_c) if n_c else None,
        "TP_rate": (100.0 * tp / n_c) if n_c else None,
        "SL_rate": (100.0 * sl / n_c) if n_c else None,
        "SL_count": sl,
        "TP_count": tp,
        "gross_profit": float(gp),
        "gross_loss": float(-gl),
        "net": net,
        "net_per_trade": (net / n_c) if n_c else None,
        "profit_factor": None if pf is None or (isinstance(pf, float) and math.isinf(pf)) else float(pf),
        "mean_MAE": float(np.mean(maes)) if maes else None,
        "median_MAE": float(np.median(maes)) if maes else None,
        "p90_MAE": float(np.percentile(maes, 90)) if maes else None,
        "mean_MFE": float(np.mean(mfes)) if mfes else None,
        "median_MFE": float(np.median(mfes)) if mfes else None,
        "sample_flag": sample_flag(n_c),
        **eq,
    }


def descriptive_quality(s: dict) -> str:
    """Non-ML descriptive label from npt/PF/SL/DD."""
    if s["closed"] < 10:
        return "INSUFFICIENT"
    npt = s["net_per_trade"] if s["net_per_trade"] is not None else 0.0
    pf = s["profit_factor"] if s["profit_factor"] is not None else 0.0
    slr = s["SL_rate"] if s["SL_rate"] is not None else 100.0
    dd = s["max_drawdown"]
    score = 0
    if npt > 0.15:
        score += 2
    elif npt > 0:
        score += 1
    elif npt < -0.15:
        score -= 2
    else:
        score -= 1
    if pf is not None and pf >= 1.5:
        score += 2
    elif pf is not None and pf >= 1.0:
        score += 1
    elif pf is not None and pf < 0.8:
        score -= 2
    else:
        score -= 1
    if slr <= 35:
        score += 1
    elif slr >= 55:
        score -= 1
    if dd > -3:
        score += 1
    elif dd < -8:
        score -= 1
    if score >= 4:
        return "STRONG"
    if score >= 1:
        return "OK"
    if score >= -1:
        return "WEAK"
    return "POOR"


def classify_asymmetry(long_s: dict, short_s: dict) -> str:
    ln = long_s.get("net_per_trade")
    sn = short_s.get("net_per_trade")
    if long_s["closed"] < 5 and short_s["closed"] < 5:
        return "INSUFFICIENT"
    if long_s["closed"] < 5:
        return "SHORT_ONLY_SAMPLE"
    if short_s["closed"] < 5:
        return "LONG_ONLY_SAMPLE"
    assert ln is not None and sn is not None
    long_ok = ln > 0.05 and (long_s.get("profit_factor") or 0) >= 1.0
    short_ok = sn > 0.05 and (short_s.get("profit_factor") or 0) >= 1.0
    long_bad = ln < -0.05 or (long_s.get("profit_factor") or 99) < 0.9
    short_bad = sn < -0.05 or (short_s.get("profit_factor") or 99) < 0.9
    if long_ok and short_ok:
        return "BALANCED"
    if long_bad and short_bad:
        return "BOTH_WEAK"
    if long_ok and short_bad:
        return "LONG_BIASED"
    if short_ok and long_bad:
        return "SHORT_BIASED"
    if ln > sn + 0.15:
        return "LONG_BIASED"
    if sn > ln + 0.15:
        return "SHORT_BIASED"
    return "BALANCED"


def main() -> int:
    global LOOKAHEAD_VIOLATIONS
    OUT.mkdir(parents=True, exist_ok=True)
    now = AS_OF
    start = now - timedelta(days=LOOKBACK_DAYS)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading 15m Tier-A {start.date()} → {_iso(now)} …", flush=True)
    rows, _ = sig_repo.query_signals(
        start=start, end=now, tier_a=True, timeframe=SIGNAL_TF,
        time_field="candle_close_time", limit=20000, offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    api_rows = [r for r in api_rows if str(r.get("timeframe")) == SIGNAL_TF]
    api_rows.sort(key=lambda r: (_utc(r.get("entry_time") or r.get("candle_close_time")), str(r.get("symbol")), str(r["signal_id"])))
    print(f"signals={len(api_rows)}", flush=True)

    by_sym: dict[str, list] = defaultdict(list)
    for r in api_rows:
        by_sym[str(r["symbol"]).upper()].append(r)

    pad_before = timedelta(days=2)
    pad_after = timedelta(days=2)
    work_1m: dict[str, pd.DataFrame] = {}
    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, min(b, now + timedelta(days=1))) or []
        work_1m[sym] = _prepare_1m(pd.DataFrame(raw)) if raw else pd.DataFrame()
        print(f"  {sym}: signals={len(items)} 1m={len(work_1m[sym])}", flush=True)

    details: list[dict[str, Any]] = []
    for r in api_rows:
        sym = str(r["symbol"]).upper()
        side = str(r["direction"]).upper()
        sid = str(r["signal_id"])
        t0 = _utc(r.get("entry_time") or r.get("candle_close_time"))
        try:
            entry_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            entry_px = None
        work = work_1m[sym]
        entry_i = -1
        if not work.empty:
            entry_i = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))
            if entry_i >= len(work):
                entry_i = -1
            else:
                if _utc(work.iloc[entry_i]["timestamp"]) > now:
                    LOOKAHEAD_VIOLATIONS += 1
        if entry_i >= 0 and entry_px is None:
            entry_px = float(work.iloc[entry_i]["open"])

        result, pnl, mae, mfe, exit_reason = "OPEN", None, None, None, None
        if entry_i >= 0 and entry_px is not None and not work.empty:
            sim = _simulate_outcome(
                side=side, entry=float(entry_px), tf=SIGNAL_TF, entry_i=entry_i, work=work, as_of=now
            )
            result = sim["result"]
            pnl = sim.get("pnl_pct")
            mae = sim.get("mae_pct")
            mfe = sim.get("mfe_pct")
            exit_reason = sim.get("exit_reason")

        details.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "direction": side,
                "timeframe": SIGNAL_TF,
                "entry_ts": _iso(t0),
                "entry_price": entry_px,
                "result": result,
                "exit_reason": exit_reason,
                "pnl": pnl,
                "mae": mae,
                "mfe": mfe,
                "net": (float(pnl) - FEE) if pnl is not None and result in ("WIN", "LOSS") else None,
            }
        )

    # chronological order already
    write_csv(OUT / "trade_detail.csv", details)

    baseline = summarize(details, label="BASELINE")
    closed_all = [d for d in details if d["result"] in ("WIN", "LOSS")]
    total_signals = len(details)
    total_sl = sum(1 for d in closed_all if d.get("exit_reason") == "SL")

    symbols = sorted({d["symbol"] for d in details})
    sides = ("LONG", "SHORT")

    # §3 symbol × direction
    sd_rows = []
    sd_map: dict[tuple[str, str], dict] = {}
    for sym in symbols:
        for side in sides:
            sub = [d for d in details if d["symbol"] == sym and d["direction"] == side]
            s = summarize(sub, label=f"{sym}_{side}")
            row = {"symbol": sym, "direction": side, **s}
            sd_rows.append(row)
            sd_map[(sym, side)] = row
    write_csv(OUT / "symbol_direction_summary.csv", sd_rows)

    # §4 SL concentration
    sl_rows = []
    for sym in symbols:
        for side in sides:
            sub = [d for d in details if d["symbol"] == sym and d["direction"] == side]
            closed = [d for d in sub if d["result"] in ("WIN", "LOSS")]
            sl_c = sum(1 for d in closed if d.get("exit_reason") == "SL")
            sig_share = (100.0 * len(sub) / total_signals) if total_signals else 0.0
            sl_share = (100.0 * sl_c / total_sl) if total_sl else 0.0
            over = (sl_share / sig_share) if sig_share > 0 else None
            sl_rows.append(
                {
                    "symbol": sym,
                    "direction": side,
                    "signals": len(sub),
                    "closed": len(closed),
                    "SL_count": sl_c,
                    "signals_share_pct": sig_share,
                    "share_of_all_SLs_pct": sl_share,
                    "SL_OVERREPRESENTATION": over,
                    "sample_flag": sample_flag(len(closed)),
                }
            )
    sl_rows.sort(key=lambda r: (r["SL_OVERREPRESENTATION"] or 0), reverse=True)
    write_csv(OUT / "sl_concentration.csv", sl_rows)

    # §9 coin summary
    coin_rows = []
    for sym in symbols:
        lng = summarize([d for d in details if d["symbol"] == sym and d["direction"] == "LONG"], label=f"{sym}_LONG")
        sht = summarize([d for d in details if d["symbol"] == sym and d["direction"] == "SHORT"], label=f"{sym}_SHORT")
        tot = summarize([d for d in details if d["symbol"] == sym], label=sym)
        coin_rows.append(
            {
                "symbol": sym,
                "long_net": lng["net"],
                "short_net": sht["net"],
                "total_net": tot["net"],
                "winrate": tot["winrate"],
                "profit_factor": tot["profit_factor"],
                "SL_rate": tot["SL_rate"],
                "max_drawdown": tot["max_drawdown"],
                "closed": tot["closed"],
                "long_closed": lng["closed"],
                "short_closed": sht["closed"],
                "long_quality": descriptive_quality(lng),
                "short_quality": descriptive_quality(sht),
                "sample_flag": tot["sample_flag"],
            }
        )
    write_csv(OUT / "coin_summary.csv", coin_rows)

    # §10 directional asymmetry
    asym_rows = []
    for sym in symbols:
        lng = sd_map[(sym, "LONG")]
        sht = sd_map[(sym, "SHORT")]
        gap = None
        if lng["net_per_trade"] is not None and sht["net_per_trade"] is not None:
            gap = float(lng["net_per_trade"] - sht["net_per_trade"])
        asym_rows.append(
            {
                "symbol": sym,
                "long_npt": lng["net_per_trade"],
                "short_npt": sht["net_per_trade"],
                "long_wr": lng["winrate"],
                "short_wr": sht["winrate"],
                "long_pf": lng["profit_factor"],
                "short_pf": sht["profit_factor"],
                "long_sl_rate": lng["SL_rate"],
                "short_sl_rate": sht["SL_rate"],
                "direction_edge_gap": gap,
                "classification": classify_asymmetry(lng, sht),
                "long_closed": lng["closed"],
                "short_closed": sht["closed"],
            }
        )
    write_csv(OUT / "directional_asymmetry.csv", asym_rows)

    # §7 time stability
    time_rows = []
    for sym in symbols:
        for side in sides:
            sub = sorted(
                [d for d in details if d["symbol"] == sym and d["direction"] == side and d["result"] in ("WIN", "LOSS")],
                key=lambda x: x["entry_ts"],
            )
            # also include opens in chrono for blocks? use all signals chrono for stability of closed outcomes only
            all_sub = sorted(
                [d for d in details if d["symbol"] == sym and d["direction"] == side],
                key=lambda x: x["entry_ts"],
            )
            if not all_sub:
                continue
            n = len(all_sub)
            mid = n // 2
            blocks = {
                "first_half": all_sub[:mid],
                "second_half": all_sub[mid:],
                "block_1": all_sub[: n // 3],
                "block_2": all_sub[n // 3 : 2 * n // 3],
                "block_3": all_sub[2 * n // 3 :],
            }
            for bname, blk in blocks.items():
                s = summarize(blk, label=f"{sym}_{side}_{bname}")
                time_rows.append(
                    {
                        "symbol": sym,
                        "direction": side,
                        "block": bname,
                        "closed": s["closed"],
                        "winrate": s["winrate"],
                        "net": s["net"],
                        "net_per_trade": s["net_per_trade"],
                        "profit_factor": s["profit_factor"],
                        "SL_rate": s["SL_rate"],
                        "sample_flag": s["sample_flag"],
                    }
                )
    write_csv(OUT / "time_stability.csv", time_rows)

    # §8 losing streaks
    streak_rows = []
    for sym in symbols:
        for side in sides:
            sub = sorted(
                [d for d in details if d["symbol"] == sym and d["direction"] == side and d["result"] in ("WIN", "LOSS")],
                key=lambda x: x["entry_ts"],
            )
            s = summarize(sub, label=f"{sym}_{side}")
            streak_rows.append(
                {
                    "symbol": sym,
                    "direction": side,
                    "closed": s["closed"],
                    "max_consecutive_SL": s["max_consecutive_SL"],
                    "streaks_2plus_SL": s["streaks_2plus_SL"],
                    "streaks_3plus_SL": s["streaks_3plus_SL"],
                    "streaks_5plus_SL": s["streaks_5plus_SL"],
                    "longest_losing_streak": s["longest_losing_streak"],
                    "worst_5_trade_seq": s["worst_5_trade_seq"],
                    "worst_10_trade_seq": s["worst_10_trade_seq"],
                    "sample_flag": s["sample_flag"],
                }
            )
    write_csv(OUT / "losing_streaks.csv", streak_rows)

    # §11 counterfactual blocks — each weak combo individually
    def is_weak(row: dict) -> bool:
        if row["closed"] < BLOCK_MIN_CLOSED:
            return False
        npt = row["net_per_trade"]
        pf = row["profit_factor"]
        slr = row["SL_rate"] or 0
        if npt is not None and npt < -0.05:
            return True
        if pf is not None and pf < 0.95 and (npt or 0) <= 0:
            return True
        if slr >= 55 and (npt or 0) < 0.05:
            return True
        return False

    # also always include APT SHORT for continuity with prior audit
    block_candidates = []
    for row in sd_rows:
        if is_weak(row) or (row["symbol"] == "APTUSDT" and row["direction"] == "SHORT" and row["closed"] >= 5):
            block_candidates.append((row["symbol"], row["direction"]))
    # unique
    seen_b = set()
    block_list = []
    for c in block_candidates:
        if c not in seen_b:
            seen_b.add(c)
            block_list.append(c)

    cf_rows = []
    for sym, side in block_list:
        kept = [d for d in details if not (d["symbol"] == sym and d["direction"] == side)]
        removed = [d for d in details if d["symbol"] == sym and d["direction"] == side]
        rem_closed = [d for d in removed if d["result"] in ("WIN", "LOSS")]
        before = baseline
        after = summarize(kept, label=f"BLOCK_{sym}_{side}")
        cf_rows.append(
            {
                "block": f"BLOCK_{sym}_{side}",
                "symbol": sym,
                "direction": side,
                "trades_removed": len(rem_closed),
                "signals_removed": len(removed),
                "winners_removed": sum(1 for d in rem_closed if d["result"] == "WIN"),
                "losers_removed": sum(1 for d in rem_closed if d["result"] == "LOSS"),
                "net_before": before["net"],
                "net_after": after["net"],
                "delta_net": after["net"] - before["net"],
                "pf_before": before["profit_factor"],
                "pf_after": after["profit_factor"],
                "max_dd_before": before["max_drawdown"],
                "max_dd_after": after["max_drawdown"],
                "delta_max_dd": after["max_drawdown"] - before["max_drawdown"],
                "closed_after": after["closed"],
                "combo_npt": sd_map[(sym, side)]["net_per_trade"],
                "combo_sample_flag": sd_map[(sym, side)]["sample_flag"],
            }
        )
    # sort by delta_net desc (best blocks first)
    cf_rows.sort(key=lambda r: r["delta_net"], reverse=True)
    write_csv(OUT / "counterfactual_blocks.csv", cf_rows)

    # Rankings among non-tiny
    ranked = [r for r in sd_rows if r["closed"] >= 10]
    rankings = {
        "highest_SL_rate": sorted(ranked, key=lambda r: (r["SL_rate"] is None, -(r["SL_rate"] or -1)))[:5],
        "lowest_winrate": sorted(ranked, key=lambda r: (r["winrate"] is None, r["winrate"] or 999))[:5],
        "lowest_net_per_trade": sorted(ranked, key=lambda r: (r["net_per_trade"] is None, r["net_per_trade"] or 999))[:5],
        "lowest_PF": sorted(ranked, key=lambda r: (r["profit_factor"] is None, r["profit_factor"] or 999))[:5],
        "worst_max_drawdown": sorted(ranked, key=lambda r: (r["max_drawdown"]))[:5],
    }

    # Profitable SHORT coins
    profitable_shorts = [
        r for r in sd_rows
        if r["direction"] == "SHORT" and r["closed"] >= 5 and (r["net_per_trade"] or 0) > 0 and (r["profit_factor"] or 0) >= 1.0
    ]
    weak_shorts = [
        r for r in sd_rows
        if r["direction"] == "SHORT" and r["closed"] >= 10 and ((r["net_per_trade"] or 0) < 0 or (r["profit_factor"] or 99) < 1.0)
    ]
    strong_shorts = [
        r for r in sd_rows
        if r["direction"] == "SHORT" and r["closed"] >= 10 and (r["net_per_trade"] or 0) > 0.05 and (r["profit_factor"] or 0) >= 1.1
    ]

    # Generalization / primary
    short_all = summarize([d for d in details if d["direction"] == "SHORT"], label="ALL_SHORT")
    long_all = summarize([d for d in details if d["direction"] == "LONG"], label="ALL_LONG")

    n_long_biased = sum(1 for r in asym_rows if r["classification"] == "LONG_BIASED")
    n_short_biased = sum(1 for r in asym_rows if r["classification"] == "SHORT_BIASED")
    n_both_weak = sum(1 for r in asym_rows if r["classification"] == "BOTH_WEAK")
    n_balanced = sum(1 for r in asym_rows if r["classification"] == "BALANCED")

    # APT SHORT stability
    apt_s_fh = next((r for r in time_rows if r["symbol"] == "APTUSDT" and r["direction"] == "SHORT" and r["block"] == "first_half"), None)
    apt_s_sh = next((r for r in time_rows if r["symbol"] == "APTUSDT" and r["direction"] == "SHORT" and r["block"] == "second_half"), None)
    apt_short_stable_bad = False
    if apt_s_fh and apt_s_sh and apt_s_fh["closed"] >= 3 and apt_s_sh["closed"] >= 3:
        apt_short_stable_bad = (apt_s_fh.get("net_per_trade") or 0) < 0 and (apt_s_sh.get("net_per_trade") or 0) < 0

    if baseline["closed"] < 50:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif weak_shorts and not profitable_shorts and (short_all.get("net_per_trade") or 0) < -0.05:
        primary = "SHORT_IS_GLOBALLY_WEAK"
        recommendation = "SYMBOL_DIRECTION_FILTER_WORTH_FURTHER_TESTING"
    elif (
        n_long_biased >= 3
        and (strong_shorts or profitable_shorts)
        and weak_shorts
        and (long_all.get("net_per_trade") or 0) > 0.1
    ):
        # LONGs broadly stronger; shorts good on some coins, bad on others
        primary = "EDGE_IS_SYMBOL_AND_DIRECTION_DEPENDENT"
        recommendation = "SYMBOL_DIRECTION_FILTER_WORTH_FURTHER_TESTING"
    elif strong_shorts and weak_shorts:
        primary = "SHORT_WEAKNESS_IS_SYMBOL_SPECIFIC"
        recommendation = "SYMBOL_DIRECTION_FILTER_WORTH_FURTHER_TESTING"
    elif n_long_biased + n_short_biased >= 3:
        primary = "EDGE_IS_SYMBOL_AND_DIRECTION_DEPENDENT"
        recommendation = "SYMBOL_DIRECTION_FILTER_WORTH_FURTHER_TESTING"
    elif not weak_shorts and not strong_shorts:
        primary = "NO_STABLE_SYMBOL_EFFECT"
        recommendation = "KEEP_ALL_SYMBOL_DIRECTIONS"
    else:
        apt_s = sd_map[("APTUSDT", "SHORT")]
        doge_s = sd_map[("DOGEUSDT", "SHORT")]
        if (apt_s["net_per_trade"] or 0) < 0 and (doge_s["net_per_trade"] or 0) > 0:
            primary = "SHORT_WEAKNESS_IS_SYMBOL_SPECIFIC"
            recommendation = "SYMBOL_DIRECTION_FILTER_WORTH_FURTHER_TESTING"
        else:
            primary = "EDGE_IS_SYMBOL_AND_DIRECTION_DEPENDENT"
            recommendation = "SYMBOL_DIRECTION_FILTER_WORTH_FURTHER_TESTING"

    # If most combos tiny → insufficient nuance
    non_tiny = sum(1 for r in sd_rows if r["sample_flag"] not in ("TINY_SAMPLE",))
    if non_tiny < 6 and primary != "INSUFFICIENT_SAMPLE":
        # still have overall baseline large enough; keep but note
        pass

    most_sl = max(sl_rows, key=lambda r: r["SL_count"])
    most_over = max((r for r in sl_rows if r["closed"] >= 10), key=lambda r: r["SL_OVERREPRESENTATION"] or 0, default=None)

    meta = {
        "task": "AUDIT_SYMBOL_DIRECTION_EDGE_STABILITY",
        "variant": "BASELINE_IMMEDIATE",
        "timeframe": SIGNAL_TF,
        "as_of": _iso(now),
        "fee": FEE,
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "n_signals": len(details),
        "n_closed": baseline["closed"],
        "symbols": symbols,
        "baseline": baseline,
        "all_long": long_all,
        "all_short": short_all,
        "primary_decision": primary,
        "recommendation": recommendation,
        "generalization": primary,
        "asymmetry_counts": {
            "LONG_BIASED": n_long_biased,
            "SHORT_BIASED": n_short_biased,
            "BALANCED": n_balanced,
            "BOTH_WEAK": n_both_weak,
        },
        "profitable_shorts": [f"{r['symbol']}" for r in profitable_shorts],
        "strong_shorts": [f"{r['symbol']}" for r in strong_shorts],
        "weak_shorts": [f"{r['symbol']}" for r in weak_shorts],
        "apt_short_stable_bad_both_halves": apt_short_stable_bad,
        "most_sl_combo": f"{most_sl['symbol']}_{most_sl['direction']}",
        "most_overrepresented": (
            f"{most_over['symbol']}_{most_over['direction']}" if most_over else None
        ),
        "strategy_logic_changed": False,
    }

    # compact rankings for json
    rank_out = {
        k: [{"symbol": r["symbol"], "direction": r["direction"], "value": r.get({"highest_SL_rate": "SL_rate", "lowest_winrate": "winrate", "lowest_net_per_trade": "net_per_trade", "lowest_PF": "profit_factor", "worst_max_drawdown": "max_drawdown"}[k]), "closed": r["closed"], "sample_flag": r["sample_flag"]} for r in v]
        for k, v in rankings.items()
    }

    (OUT / "summary.json").write_text(
        json.dumps(
            {
                "meta": meta,
                "rankings": rank_out,
                "counterfactuals": cf_rows,
                "apt_doge": {
                    "APT_LONG": sd_map[("APTUSDT", "LONG")],
                    "APT_SHORT": sd_map[("APTUSDT", "SHORT")],
                    "DOGE_LONG": sd_map[("DOGEUSDT", "LONG")],
                    "DOGE_SHORT": sd_map[("DOGEUSDT", "SHORT")],
                },
            },
            indent=2,
            default=str,
        )
        + "\n",
        encoding="utf-8",
    )

    def fmt(v: Any, nd: int = 2) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return "–"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# AUDIT_SYMBOL_DIRECTION_EDGE_STABILITY",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"Baseline 15m BASELINE_IMMEDIATE | signals={len(details)} closed={baseline['closed']} "
        f"WR={fmt(baseline['winrate'],1)} Net={fmt(baseline['net'])} npt={fmt(baseline['net_per_trade'],3)} "
        f"PF={fmt(baseline['profit_factor'])} SL={fmt(baseline['SL_rate'],1)} DD={fmt(baseline['max_drawdown'])}",
        f"lookahead_violations={LOOKAHEAD_VIOLATIONS}",
        f"ALL LONG: npt={fmt(long_all['net_per_trade'],3)} PF={fmt(long_all['profit_factor'])} | "
        f"ALL SHORT: npt={fmt(short_all['net_per_trade'],3)} PF={fmt(short_all['profit_factor'])}",
        "",
        "## Symbol × Direction (closed>=5 or notable)",
        "",
        "| Symbol | Side | Trades | WR | SL Rate | Net | Net/Trade | PF | Max DD | Sample |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    show = sorted(sd_rows, key=lambda r: (r["net_per_trade"] is None, r["net_per_trade"] or 0))
    for r in show:
        if r["closed"] == 0:
            continue
        lines.append(
            f"| {r['symbol']} | {r['direction']} | {r['closed']} | {fmt(r['winrate'],1)} | {fmt(r['SL_rate'],1)} | "
            f"{fmt(r['net'])} | {fmt(r['net_per_trade'],3)} | {fmt(r['profit_factor'])} | {fmt(r['max_drawdown'])} | {r['sample_flag']} |"
        )
    lines += ["", "## Top SL overrepresentation (closed>=10)", ""]
    for r in sl_rows:
        if r["closed"] >= 10:
            lines.append(
                f"- {r['symbol']} {r['direction']}: SL_share={fmt(r['share_of_all_SLs_pct'],1)}% "
                f"sig_share={fmt(r['signals_share_pct'],1)}% over={fmt(r['SL_OVERREPRESENTATION'],2)}"
            )
    lines += ["", "## Counterfactuals (single blocks)", ""]
    for r in cf_rows[:12]:
        lines.append(
            f"- {r['block']}: ΔNet={fmt(r['delta_net'],2)} ΔDD={fmt(r['delta_max_dd'],2)} "
            f"removed W/L={r['winners_removed']}/{r['losers_removed']} [{r['combo_sample_flag']}]"
        )
    lines += ["", "## Strategy Logic Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("BASELINE", baseline["closed"], baseline["net"], baseline["winrate"], flush=True)
    print("SHORT ALL npt", short_all["net_per_trade"], "LONG ALL npt", long_all["net_per_trade"], flush=True)
    print("profitable_shorts", [r["symbol"] for r in profitable_shorts], flush=True)
    print("weak_shorts", [r["symbol"] for r in weak_shorts], flush=True)
    print("lookahead", LOOKAHEAD_VIOLATIONS, "wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
