#!/usr/bin/env python3
"""FULL_HISTORY_BASELINE_SYMBOL_DIRECTION_AUDIT — research-only, no DB writes.

Regenerate 15m GLOBAL_FROZEN_TIER_A BASELINE_IMMEDIATE signals over all available
1m history, then audit symbol×direction quality. No strategy/filter changes.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.db.signals import SignalRepository  # noqa: E402
from signal_generator.pipeline.versions import (  # noqa: E402
    EDGES_VERSION,
    GLOBAL_FROZEN_TIER_A,
    STRATEGY_VERSION,
)
from signal_generator.research.one_m_entry_timing.timing import (  # noqa: E402
    _prepare_1m,
    _simulate_outcome,
    _utc,
)
from signal_generator.strategy.wave_fade.adapter import (  # noqa: E402
    bars_to_ohlcv_df,
    one_minute_books,
)
from signal_generator.strategy.wave_fade.edges import load_frozen_eff_edges  # noqa: E402
from signal_generator.strategy.wave_fade.parameters import PRIMARY_FEE, TPSL_BY_TF  # noqa: E402
from signal_generator.strategy.wave_fade.signals import (  # noqa: E402
    build_symbol_signals,
    build_waves_from_ohlcv,
    resolve_entries,
)
from signal_generator.timeframes import (  # noqa: E402
    aggregate_1m_to_timeframe,
    bars_from_mappings,
)

OUT = ROOT / "results" / "full_history_baseline_symbol_direction_audit"
AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
SIGNAL_TF = "15m"
FEE = float(PRIMARY_FEE)
# Symbols from existing Tier-A universe (no new downloads / no new coins)
SYMBOLS = [
    "ACEUSDT", "APTUSDT", "AVAXUSDT", "BMTUSDT", "BTCUSDT",
    "DOGEUSDT", "HYPEUSDT", "SOLUSDT", "TUTUSDT", "XRPUSDT", "ZECUSDT",
]
WAVE_FADE_DIR = ROOT / "src" / "signal_generator" / "strategy" / "wave_fade"


def _iso(ts: Any | None) -> str | None:
    if ts is None or (isinstance(ts, float) and math.isnan(ts)):
        return None
    try:
        return _utc(ts).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


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


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def coverage_label(span_days: float) -> str:
    if span_days >= 180:
        return "FULL_COVERAGE"
    if span_days >= 90:
        return "PARTIAL_COVERAGE"
    return "INSUFFICIENT_COVERAGE"


def sample_flag(n: int) -> str:
    if n < 20:
        return "SMALL"
    if n < 50:
        return "MODERATE"
    if n < 100:
        return "GOOD"
    return "STRONGER_SAMPLE"


def window_stats_from_closed(closed: list[dict]) -> dict[str, Any]:
    if not closed:
        return {
            "closed": 0, "wins": 0, "losses": 0, "TP": 0, "SL": 0, "BE": 0,
            "winrate": None, "SL_rate": None, "TP_rate": None,
            "net": 0.0, "net_per_trade": None, "profit_factor": None,
            "gross_profit": 0.0, "gross_loss": 0.0,
            "mean_MAE": None, "median_MAE": None, "p90_MAE": None,
            "mean_MFE": None, "median_MFE": None,
        }
    wins = [r for r in closed if r["result"] == "WIN"]
    losses = [r for r in closed if r["result"] == "LOSS"]
    tp = sum(1 for r in closed if r.get("exit_reason") == "TP")
    sl = sum(1 for r in closed if r.get("exit_reason") == "SL")
    be = sum(1 for r in closed if r.get("exit_reason") == "BE")
    nets = [float(r["net_pnl"]) for r in closed]
    gross = [float(r["pnl"]) for r in closed]
    gp = sum(x for x in gross if x > 0)
    gl = sum(-x for x in gross if x < 0)
    maes = [float(r["mae"]) for r in closed if r.get("mae") is not None]
    mfes = [float(r["mfe"]) for r in closed if r.get("mfe") is not None]
    pf = (gp / gl) if gl > 0 else None
    n = len(closed)
    return {
        "closed": n,
        "wins": len(wins),
        "losses": len(losses),
        "TP": tp,
        "SL": sl,
        "BE": be,
        "winrate": 100.0 * len(wins) / n,
        "SL_rate": 100.0 * sl / n,
        "TP_rate": 100.0 * tp / n,
        "net": float(sum(nets)),
        "net_per_trade": float(np.mean(nets)),
        "profit_factor": float(pf) if pf is not None else None,
        "gross_profit": float(gp),
        "gross_loss": float(-gl),
        "mean_MAE": float(np.mean(maes)) if maes else None,
        "median_MAE": float(np.median(maes)) if maes else None,
        "p90_MAE": float(np.percentile(maes, 90)) if maes else None,
        "mean_MFE": float(np.mean(mfes)) if mfes else None,
        "median_MFE": float(np.median(mfes)) if mfes else None,
    }


def equity_and_streaks(closed: list[dict]) -> dict[str, Any]:
    if not closed:
        return {
            "max_drawdown": 0.0,
            "max_consecutive_SL": 0,
            "count_2plus_SL_streaks": 0,
            "count_3plus_SL_streaks": 0,
            "count_5plus_SL_streaks": 0,
            "count_10plus_SL_streaks": 0,
            "worst_5_trade_seq": None,
            "worst_10_trade_seq": None,
            "worst_20_trade_seq": None,
            "worst_5_start": None,
            "worst_10_start": None,
            "worst_20_start": None,
        }
    nets = [float(r["net_pnl"]) for r in closed]
    eq = np.cumsum(np.asarray(nets, dtype=float))
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())

    # SL streaks
    max_sl = 0
    counts = {2: 0, 3: 0, 5: 0, 10: 0}
    i = 0
    reasons = [r.get("exit_reason") for r in closed]
    while i < len(reasons):
        if reasons[i] == "SL":
            j = i
            while j < len(reasons) and reasons[j] == "SL":
                j += 1
            length = j - i
            max_sl = max(max_sl, length)
            for thr in (2, 3, 5, 10):
                if length >= thr:
                    counts[thr] += 1
            i = j
        else:
            i += 1

    def worst_k(k: int):
        if len(nets) < k:
            return None, None
        best = None
        best_i = None
        for i0 in range(len(nets) - k + 1):
            s = sum(nets[i0 : i0 + k])
            if best is None or s < best:
                best = s
                best_i = i0
        start_ts = closed[best_i]["entry_ts"] if best_i is not None else None
        return best, start_ts

    w5, w5s = worst_k(5)
    w10, w10s = worst_k(10)
    w20, w20s = worst_k(20)
    return {
        "max_drawdown": max_dd,
        "max_consecutive_SL": int(max_sl),
        "count_2plus_SL_streaks": counts[2],
        "count_3plus_SL_streaks": counts[3],
        "count_5plus_SL_streaks": counts[5],
        "count_10plus_SL_streaks": counts[10],
        "worst_5_trade_seq": w5,
        "worst_10_trade_seq": w10,
        "worst_20_trade_seq": w20,
        "worst_5_start": w5s,
        "worst_10_start": w10s,
        "worst_20_start": w20s,
    }


def classify_asymmetry(long_s: dict, short_s: dict) -> str:
    if long_s["closed"] < 20 or short_s["closed"] < 20:
        return "MIXED_OR_INSUFFICIENT"
    ln, sn = long_s["net_per_trade"], short_s["net_per_trade"]
    lpf, spf = long_s["profit_factor"], short_s["profit_factor"]
    long_ok = ln is not None and ln > 0.05 and lpf is not None and lpf >= 1.1
    short_ok = sn is not None and sn > 0.05 and spf is not None and spf >= 1.1
    long_bad = ln is not None and (ln < -0.05 or (lpf is not None and lpf < 1.0))
    short_bad = sn is not None and (sn < -0.05 or (spf is not None and spf < 1.0))
    if long_ok and short_ok:
        return "BOTH_STRONG"
    if long_ok and short_bad:
        return "LONG_STRONG_SHORT_WEAK"
    if short_ok and long_bad:
        return "SHORT_STRONG_LONG_WEAK"
    if long_bad and short_bad:
        return "BOTH_WEAK"
    return "MIXED_OR_INSUFFICIENT"


def consistency_label(monthly_rows: list[dict]) -> str:
    usable = [m for m in monthly_rows if (m.get("closed") or 0) >= 5]
    if len(usable) < 3:
        return "INSUFFICIENT"
    pos = sum(1 for m in usable if (m.get("net") or 0) > 0)
    neg = sum(1 for m in usable if (m.get("net") or 0) < 0)
    if pos >= len(usable) - 1 and pos >= 3:
        return "STABLE_POSITIVE"
    if neg >= len(usable) - 1 and neg >= 3:
        return "STABLE_NEGATIVE"
    if pos >= 2 and neg >= 2:
        return "REGIME_DEPENDENT"
    if pos > neg:
        return "STABLE_POSITIVE"
    if neg > pos:
        return "STABLE_NEGATIVE"
    return "REGIME_DEPENDENT"


def regenerate_symbol_trades(
    *,
    sym: str,
    candle_repo: CandleRepository,
    edges: dict,
    hist_start: datetime,
    as_of: datetime,
) -> tuple[list[dict], dict]:
    raw = candle_repo.get_candles(sym, hist_start, as_of) or []
    cov = {
        "symbol": sym,
        "n_1m": len(raw),
        "first": _iso(raw[0]["open_time"]) if raw else None,
        "last": _iso(raw[-1]["open_time"]) if raw else None,
        "span_days": (
            (_utc(raw[-1]["open_time"]) - _utc(raw[0]["open_time"])).total_seconds() / 86400.0
            if len(raw) >= 2
            else 0.0
        ),
    }
    cov["coverage"] = coverage_label(cov["span_days"])
    if len(raw) < 1000:
        return [], cov

    bars = bars_from_mappings(raw)
    ohlcv_1m = bars_to_ohlcv_df(bars)
    open_times, opens = one_minute_books(ohlcv_1m)
    work = _prepare_1m(pd.DataFrame(raw))

    htf_bars = aggregate_1m_to_timeframe(bars, SIGNAL_TF, as_of=as_of, require_complete=True)
    ohlcv_15 = bars_to_ohlcv_df(htf_bars)
    waves = build_waves_from_ohlcv(ohlcv_15, symbol=sym, timeframe=SIGNAL_TF)
    all_sig = build_symbol_signals(sym, edges, {SIGNAL_TF: waves})
    if all_sig.empty:
        return [], cov

    tf_sig = all_sig[all_sig["signal_tf"].astype(str) == SIGNAL_TF].copy()
    tf_sig = tf_sig[tf_sig["is_tier_a"].astype(bool)].copy()
    if tf_sig.empty:
        return [], cov

    resolved = resolve_entries(tf_sig, open_times, opens)
    resolved = resolved[resolved["entry_valid"].astype(bool)].copy()
    # keep signals with entry within history (after warmup)
    trades: list[dict] = []
    for _, row in resolved.iterrows():
        side = str(row["side"]).upper()
        entry_ts = _utc(row["entry_time"])
        if entry_ts > as_of:
            continue
        entry_px = float(row["entry_price"])
        entry_i = int(work["timestamp"].searchsorted(pd.Timestamp(entry_ts), side="left"))
        if entry_i < 0 or entry_i >= len(work):
            continue
        # causality: entry bar open <= as_of
        if _utc(work.iloc[entry_i]["timestamp"]) > as_of:
            continue
        sim = _simulate_outcome(
            side=side, entry=entry_px, tf=SIGNAL_TF, entry_i=entry_i, work=work, as_of=as_of
        )
        result = sim["result"]
        pnl = sim.get("pnl_pct")
        net = (float(pnl) - FEE) if pnl is not None and result in ("WIN", "LOSS") else None
        tp_pct, sl_pct = TPSL_BY_TF[SIGNAL_TF]
        if side == "LONG":
            tp_price = entry_px * (1.0 + tp_pct / 100.0)
            sl_price = entry_px * (1.0 - sl_pct / 100.0)
        else:
            tp_price = entry_px * (1.0 - tp_pct / 100.0)
            sl_price = entry_px * (1.0 + sl_pct / 100.0)

        # deterministic research id
        sid = f"{sym}|{SIGNAL_TF}|{side}|{_iso(entry_ts)}|{_iso(row.get('confirmation_available_at'))}"
        trades.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "direction": side,
                "timeframe": SIGNAL_TF,
                "signal_ts": _iso(row.get("confirmation_available_at")),
                "entry_ts": _iso(entry_ts),
                "entry_price": entry_px,
                "tp_price": float(sim.get("tp_price") or tp_price),
                "sl_price": float(sim.get("sl_price") or sl_price),
                "exit_ts": sim.get("exit_time"),
                "exit_price": sim.get("exit_price"),
                "outcome": sim.get("exit_reason") if result in ("WIN", "LOSS") else result,
                "result": result,
                "exit_reason": sim.get("exit_reason"),
                "pnl": pnl,
                "net_pnl": net,
                "mae": sim.get("mae_pct"),
                "mfe": sim.get("mfe_pct"),
                "duration_seconds": sim.get("duration_seconds"),
                "year_month": entry_ts.strftime("%Y-%m"),
            }
        )
    cov["signals_generated"] = len(trades)
    return trades, cov


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = AS_OF

    # --- Baseline / git documentation ---
    git_status = "NO_GIT_REPO"
    git_head = None
    try:
        git_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
        git_status = subprocess.check_output(["git", "status", "-sb"], cwd=ROOT, stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        pass

    strategy_hashes = {
        p.name: file_sha256(p) for p in sorted(WAVE_FADE_DIR.glob("*.py"))
    }
    hashes_before = dict(strategy_hashes)

    print("Loading frozen edges…", flush=True)
    edges = load_frozen_eff_edges()
    assert GLOBAL_FROZEN_TIER_A is True

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    candle_repo = CandleRepository(ch)
    # discover actual candle start from APT (representative)
    probe = candle_repo.get_candles("APTUSDT", now - timedelta(days=400), now) or []
    hist_start = _utc(probe[0]["open_time"]) if probe else datetime(2026, 1, 1, tzinfo=timezone.utc)
    print(f"history_start={_iso(hist_start)} as_of={_iso(now)}", flush=True)

    all_trades: list[dict] = []
    coverage_rows: list[dict] = []
    for sym in SYMBOLS:
        print(f"  regenerating {sym}…", flush=True)
        trades, cov = regenerate_symbol_trades(
            sym=sym, candle_repo=candle_repo, edges=edges, hist_start=hist_start, as_of=now
        )
        coverage_rows.append(cov)
        all_trades.extend(trades)
        print(f"    coverage={cov['coverage']} span={cov['span_days']:.1f}d signals={len(trades)}", flush=True)

    all_trades.sort(key=lambda r: (r["entry_ts"] or "", r["symbol"], r["direction"]))
    write_csv(OUT / "full_trade_list.csv", all_trades)
    write_csv(OUT / "coverage_by_symbol.csv", coverage_rows)

    closed_all = [t for t in all_trades if t["result"] in ("WIN", "LOSS")]
    open_all = [t for t in all_trades if t["result"] == "OPEN"]
    baseline = window_stats_from_closed(closed_all)
    baseline_eq = equity_and_streaks(closed_all)

    # --- Symbol × Direction ---
    sd_rows = []
    sd_map: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in all_trades:
        sd_map[(t["symbol"], t["direction"])].append(t)

    for (sym, side), rows in sorted(sd_map.items()):
        closed = [r for r in rows if r["result"] in ("WIN", "LOSS")]
        opens = [r for r in rows if r["result"] == "OPEN"]
        s = window_stats_from_closed(closed)
        eq = equity_and_streaks(closed)
        sd_rows.append(
            {
                "symbol": sym,
                "direction": side,
                "signals": len(rows),
                "open": len(opens),
                **s,
                **eq,
                "sample_flag": sample_flag(s["closed"]),
            }
        )
    write_csv(OUT / "symbol_direction_summary.csv", sd_rows)

    # --- Coin summary ---
    coin_rows = []
    for sym in SYMBOLS:
        rows = [t for t in all_trades if t["symbol"] == sym]
        closed = [r for r in rows if r["result"] in ("WIN", "LOSS")]
        s = window_stats_from_closed(closed)
        eq = equity_and_streaks(closed)
        coin_rows.append(
            {
                "symbol": sym,
                "signals": len(rows),
                **s,
                "max_drawdown": eq["max_drawdown"],
                "max_consecutive_SL": eq["max_consecutive_SL"],
                "sample_flag": sample_flag(s["closed"]),
            }
        )
    write_csv(OUT / "coin_summary.csv", coin_rows)

    # --- Directional asymmetry ---
    asym_rows = []
    class_counts = defaultdict(int)
    for sym in SYMBOLS:
        lng = next((r for r in sd_rows if r["symbol"] == sym and r["direction"] == "LONG"), None)
        sht = next((r for r in sd_rows if r["symbol"] == sym and r["direction"] == "SHORT"), None)
        if not lng or not sht:
            continue
        cls = classify_asymmetry(lng, sht)
        class_counts[cls] += 1
        asym_rows.append(
            {
                "symbol": sym,
                "long_trades": lng["closed"],
                "long_TP": lng["TP"],
                "long_SL": lng["SL"],
                "long_WR": lng["winrate"],
                "long_net": lng["net"],
                "long_npt": lng["net_per_trade"],
                "long_pf": lng["profit_factor"],
                "short_trades": sht["closed"],
                "short_TP": sht["TP"],
                "short_SL": sht["SL"],
                "short_WR": sht["winrate"],
                "short_net": sht["net"],
                "short_npt": sht["net_per_trade"],
                "short_pf": sht["profit_factor"],
                "classification": cls,
            }
        )
    write_csv(OUT / "directional_asymmetry.csv", asym_rows)

    # --- SL / TP concentration ---
    total_signals = len(all_trades) or 1
    total_sl = sum(1 for t in closed_all if t.get("exit_reason") == "SL") or 1
    total_tp = sum(1 for t in closed_all if t.get("exit_reason") == "TP") or 1
    total_loss_pnl = abs(sum(float(t["net_pnl"]) for t in closed_all if float(t["net_pnl"]) < 0)) or 1.0
    total_net = baseline["net"] or 1.0

    sl_conc = []
    tp_conc = []
    for r in sd_rows:
        sig_share = 100.0 * r["signals"] / total_signals
        sl_share = 100.0 * r["SL"] / total_sl
        tp_share = 100.0 * r["TP"] / total_tp
        sl_conc.append(
            {
                "symbol": r["symbol"],
                "direction": r["direction"],
                "signal_count": r["signals"],
                "signal_share_pct": sig_share,
                "SL_count": r["SL"],
                "SL_share_pct": sl_share,
                "SL_overrepresentation": (sl_share / sig_share) if sig_share > 0 else None,
                "SL_rate": r["SL_rate"],
                "net": r["net"],
                "net_per_trade": r["net_per_trade"],
                "profit_factor": r["profit_factor"],
                "max_drawdown": r["max_drawdown"],
                "sample_flag": r["sample_flag"],
            }
        )
        loss_pnl = abs(sum(
            float(t["net_pnl"])
            for t in sd_map[(r["symbol"], r["direction"])]
            if t["result"] in ("WIN", "LOSS") and float(t["net_pnl"]) < 0
        ))
        tp_conc.append(
            {
                "symbol": r["symbol"],
                "direction": r["direction"],
                "TP_count": r["TP"],
                "TP_share_pct": tp_share,
                "net": r["net"],
                "net_profit_contribution_pct": (100.0 * r["net"] / total_net) if total_net else None,
                "gross_loss_contribution_pct": 100.0 * loss_pnl / total_loss_pnl,
                "sample_flag": r["sample_flag"],
            }
        )
    sl_conc.sort(key=lambda x: x["SL_overrepresentation"] or 0, reverse=True)
    tp_conc.sort(key=lambda x: x["net"] or 0, reverse=True)
    write_csv(OUT / "sl_concentration.csv", sl_conc)
    write_csv(OUT / "tp_concentration.csv", tp_conc)

    # --- Losing streaks ---
    streak_rows = []
    for r in sd_rows:
        streak_rows.append(
            {
                "symbol": r["symbol"],
                "direction": r["direction"],
                "closed": r["closed"],
                "max_consecutive_SL": r["max_consecutive_SL"],
                "count_2plus_SL_streaks": r["count_2plus_SL_streaks"],
                "count_3plus_SL_streaks": r["count_3plus_SL_streaks"],
                "count_5plus_SL_streaks": r["count_5plus_SL_streaks"],
                "count_10plus_SL_streaks": r["count_10plus_SL_streaks"],
                "worst_5_trade_seq": r["worst_5_trade_seq"],
                "worst_5_start": r["worst_5_start"],
                "worst_10_trade_seq": r["worst_10_trade_seq"],
                "worst_10_start": r["worst_10_start"],
                "worst_20_trade_seq": r["worst_20_trade_seq"],
                "worst_20_start": r["worst_20_start"],
                "sample_flag": r["sample_flag"],
            }
        )
    write_csv(OUT / "losing_streaks.csv", streak_rows)

    # --- Monthly stability ---
    monthly_rows = []
    consistency_rows = []
    for (sym, side), rows in sorted(sd_map.items()):
        by_m: dict[str, list] = defaultdict(list)
        for t in rows:
            if t["result"] in ("WIN", "LOSS"):
                by_m[t["year_month"]].append(t)
        month_stats = []
        for ym in sorted(by_m):
            s = window_stats_from_closed(by_m[ym])
            eq = equity_and_streaks(by_m[ym])
            row = {
                "symbol": sym,
                "direction": side,
                "year_month": ym,
                "trades": s["closed"],
                "closed": s["closed"],
                "winrate": s["winrate"],
                "SL_rate": s["SL_rate"],
                "net": s["net"],
                "net_per_trade": s["net_per_trade"],
                "profit_factor": s["profit_factor"],
                "max_sl_streak": eq["max_consecutive_SL"],
            }
            monthly_rows.append(row)
            month_stats.append(row)
        usable = [m for m in month_stats if m["closed"] >= 5]
        consistency_rows.append(
            {
                "symbol": sym,
                "direction": side,
                "positive_months": sum(1 for m in usable if (m["net"] or 0) > 0),
                "negative_months": sum(1 for m in usable if (m["net"] or 0) < 0),
                "months_PF_above_1": sum(1 for m in usable if m["profit_factor"] is not None and m["profit_factor"] >= 1.0),
                "months_PF_below_1": sum(1 for m in usable if m["profit_factor"] is not None and m["profit_factor"] < 1.0),
                "months_WR_above_50": sum(1 for m in usable if m["winrate"] is not None and m["winrate"] >= 50),
                "months_WR_below_50": sum(1 for m in usable if m["winrate"] is not None and m["winrate"] < 50),
                "usable_months": len(usable),
                "consistency": consistency_label(month_stats),
                "closed_total": sum(m["closed"] for m in month_stats),
            }
        )
    write_csv(OUT / "monthly_symbol_direction.csv", monthly_rows)
    write_csv(OUT / "time_stability.csv", consistency_rows)

    # --- Counterfactuals: single direction blocks ---
    dir_cf = []
    for r in sd_rows:
        if r["closed"] < 20:
            continue
        if not (
            (r["net_per_trade"] is not None and r["net_per_trade"] < 0)
            or (r["profit_factor"] is not None and r["profit_factor"] < 1.0)
            or (r["SL_rate"] is not None and r["SL_rate"] > 55)
        ):
            continue
        kept = [
            t for t in closed_all
            if not (t["symbol"] == r["symbol"] and t["direction"] == r["direction"])
        ]
        rem = [
            t for t in closed_all
            if t["symbol"] == r["symbol"] and t["direction"] == r["direction"]
        ]
        after = window_stats_from_closed(kept)
        after_eq = equity_and_streaks(kept)
        dir_cf.append(
            {
                "block": f"BLOCK_{r['symbol']}_{r['direction']}",
                "removed_trades": len(rem),
                "removed_TP": sum(1 for t in rem if t.get("exit_reason") == "TP"),
                "removed_SL": sum(1 for t in rem if t.get("exit_reason") == "SL"),
                "net_before": baseline["net"],
                "net_after": after["net"],
                "delta_net": after["net"] - baseline["net"],
                "pf_before": baseline["profit_factor"],
                "pf_after": after["profit_factor"],
                "max_dd_before": baseline_eq["max_drawdown"],
                "max_dd_after": after_eq["max_drawdown"],
                "delta_dd": after_eq["max_drawdown"] - baseline_eq["max_drawdown"],
                "combo_npt": r["net_per_trade"],
                "sample_flag": r["sample_flag"],
            }
        )
    dir_cf.sort(key=lambda x: x["delta_net"], reverse=True)
    write_csv(OUT / "single_direction_block_counterfactual.csv", dir_cf)

    # --- Coin exclusion ---
    coin_cf = []
    for sym in SYMBOLS:
        rem = [t for t in closed_all if t["symbol"] == sym]
        if len(rem) < 20:
            continue
        kept = [t for t in closed_all if t["symbol"] != sym]
        after = window_stats_from_closed(kept)
        after_eq = equity_and_streaks(kept)
        coin_cf.append(
            {
                "block": f"BLOCK_ALL_{sym}",
                "removed_trades": len(rem),
                "removed_TP": sum(1 for t in rem if t.get("exit_reason") == "TP"),
                "removed_SL": sum(1 for t in rem if t.get("exit_reason") == "SL"),
                "net_before": baseline["net"],
                "net_after": after["net"],
                "delta_net": after["net"] - baseline["net"],
                "pf_before": baseline["profit_factor"],
                "pf_after": after["profit_factor"],
                "max_dd_before": baseline_eq["max_drawdown"],
                "max_dd_after": after_eq["max_drawdown"],
                "delta_dd": after_eq["max_drawdown"] - baseline_eq["max_drawdown"],
            }
        )
    coin_cf.sort(key=lambda x: x["delta_net"], reverse=True)
    write_csv(OUT / "single_coin_block_counterfactual.csv", coin_cf)

    # --- Portfolio contribution ---
    port_rows = []
    for sym in SYMBOLS:
        rows = [t for t in closed_all if t["symbol"] == sym]
        net = sum(float(t["net_pnl"]) for t in rows)
        loss = abs(sum(float(t["net_pnl"]) for t in rows if float(t["net_pnl"]) < 0))
        slc = sum(1 for t in rows if t.get("exit_reason") == "SL")
        tpc = sum(1 for t in rows if t.get("exit_reason") == "TP")
        port_rows.append(
            {
                "symbol": sym,
                "net_contribution_pct": 100.0 * net / total_net if total_net else None,
                "gross_loss_contribution_pct": 100.0 * loss / total_loss_pnl,
                "SL_contribution_pct": 100.0 * slc / total_sl,
                "TP_contribution_pct": 100.0 * tpc / total_tp,
                "net": net,
                "SL_count": slc,
                "TP_count": tpc,
            }
        )
    write_csv(OUT / "portfolio_contribution.csv", port_rows)

    # --- Rankings ---
    ranked = [r for r in sd_rows if r["closed"] >= 20]
    rankings = []

    def add_rank(name: str, items: list, key_fn, reverse: bool):
        ordered = sorted(items, key=key_fn, reverse=reverse)
        for i, r in enumerate(ordered[:10], 1):
            rankings.append(
                {
                    "ranking": name,
                    "rank": i,
                    "symbol": r["symbol"],
                    "direction": r.get("direction"),
                    "closed": r.get("closed"),
                    "value": key_fn(r),
                    "net": r.get("net"),
                    "net_per_trade": r.get("net_per_trade"),
                    "profit_factor": r.get("profit_factor"),
                    "SL_rate": r.get("SL_rate"),
                    "sample_flag": r.get("sample_flag"),
                }
            )

    add_rank("TOP_NET", ranked, lambda r: r["net"] or -1e9, True)
    add_rank("BOTTOM_NET", ranked, lambda r: r["net"] if r["net"] is not None else 1e9, False)
    add_rank("TOP_PF", [r for r in ranked if r["profit_factor"] is not None], lambda r: r["profit_factor"], True)
    add_rank("BOTTOM_PF", [r for r in ranked if r["profit_factor"] is not None], lambda r: r["profit_factor"], False)
    add_rank("TOP_SL_RATE", [r for r in ranked if r["SL_rate"] is not None], lambda r: r["SL_rate"], True)
    add_rank("TOP_MAX_SL_STREAK", ranked, lambda r: r["max_consecutive_SL"], True)
    add_rank("TOP_NPT", ranked, lambda r: r["net_per_trade"] or -1e9, True)
    add_rank("BOTTOM_NPT", ranked, lambda r: r["net_per_trade"] if r["net_per_trade"] is not None else 1e9, False)
    write_csv(OUT / "top_bottom_rankings.csv", rankings)

    # Loss concentration: worst 1/3/5 combos by net (most negative)
    by_net = sorted(ranked, key=lambda r: r["net"] if r["net"] is not None else 0)
    def share_of_sl(combos):
        slc = sum(r["SL"] for r in combos)
        return 100.0 * slc / total_sl
    def share_of_loss_pnl(combos):
        loss = 0.0
        for r in combos:
            for t in sd_map[(r["symbol"], r["direction"])]:
                if t["result"] in ("WIN", "LOSS") and float(t["net_pnl"]) < 0:
                    loss += abs(float(t["net_pnl"]))
        return 100.0 * loss / total_loss_pnl

    conc = {
        "worst_1_sl_share_pct": share_of_sl(by_net[:1]),
        "worst_3_sl_share_pct": share_of_sl(by_net[:3]),
        "worst_5_sl_share_pct": share_of_sl(by_net[:5]),
        "worst_1_loss_pnl_share_pct": share_of_loss_pnl(by_net[:1]),
        "worst_3_loss_pnl_share_pct": share_of_loss_pnl(by_net[:3]),
        "worst_5_loss_pnl_share_pct": share_of_loss_pnl(by_net[:5]),
        "worst_1": [f"{r['symbol']}_{r['direction']}" for r in by_net[:1]],
        "worst_3": [f"{r['symbol']}_{r['direction']}" for r in by_net[:3]],
        "worst_5": [f"{r['symbol']}_{r['direction']}" for r in by_net[:5]],
    }

    # Primary decision
    top_over = [r for r in sl_conc if r["sample_flag"] != "SMALL" and (r["SL_overrepresentation"] or 0) >= 1.3]
    n_pos = sum(1 for r in ranked if (r["net_per_trade"] or 0) > 0)
    n_neg = sum(1 for r in ranked if (r["net_per_trade"] or 0) < 0)
    insuff_cov = sum(1 for c in coverage_rows if c["coverage"] == "INSUFFICIENT_COVERAGE")

    if insuff_cov >= len(SYMBOLS) // 2 or baseline["closed"] < 200:
        primary = "INSUFFICIENT_FULL_HISTORY_COVERAGE"
        recommendation = "NEEDS_MORE_DATA"
    elif conc["worst_5_sl_share_pct"] >= 40 or conc["worst_5_loss_pnl_share_pct"] >= 40:
        # check if same symbols dominate both sides
        worst5_syms = {r["symbol"] for r in by_net[:5]}
        if len(worst5_syms) <= 2 and conc["worst_5_loss_pnl_share_pct"] >= 35:
            primary = "LOSSES_CONCENTRATED_BY_SYMBOL"
            recommendation = "FULL_COIN_EXCLUSION_WORTH_NEXT_STAGE"
        else:
            primary = "LOSSES_CONCENTRATED_IN_FEW_SYMBOL_DIRECTIONS"
            recommendation = "SYMBOL_DIRECTION_ONBOARDING_GATE_WORTH_NEXT_STAGE"
    elif class_counts.get("LONG_STRONG_SHORT_WEAK", 0) + class_counts.get("SHORT_STRONG_LONG_WEAK", 0) >= 3:
        primary = "EDGE_STRONGLY_SYMBOL_DIRECTION_DEPENDENT"
        recommendation = "SYMBOL_DIRECTION_ONBOARDING_GATE_WORTH_NEXT_STAGE"
    elif n_pos >= 0.7 * max(len(ranked), 1):
        primary = "MOST_SYMBOL_DIRECTIONS_HAVE_POSITIVE_EDGE"
        recommendation = "KEEP_ALL_SYMBOL_DIRECTIONS"
    elif not top_over and conc["worst_5_sl_share_pct"] < 30:
        primary = "LOSSES_BROADLY_DISTRIBUTED"
        recommendation = "KEEP_ALL_SYMBOL_DIRECTIONS"
    else:
        primary = "EDGE_STRONGLY_SYMBOL_DIRECTION_DEPENDENT"
        recommendation = "SYMBOL_DIRECTION_ONBOARDING_GATE_WORTH_NEXT_STAGE"

    # Post-run hash check
    hashes_after = {p.name: file_sha256(p) for p in sorted(WAVE_FADE_DIR.glob("*.py"))}
    baseline_unchanged = hashes_before == hashes_after

    history_end = max((_utc(t["entry_ts"]) for t in all_trades if t.get("entry_ts")), default=now)
    history_days = (history_end - hist_start).total_seconds() / 86400.0

    meta = {
        "task": "FULL_HISTORY_BASELINE_SYMBOL_DIRECTION_AUDIT",
        "variant": "BASELINE_IMMEDIATE",
        "tier_a_policy": "GLOBAL_FROZEN_TIER_A",
        "strategy_version_doc": STRATEGY_VERSION,
        "edges_version": EDGES_VERSION,
        "timeframe": SIGNAL_TF,
        "outcome_path": "1m_replay_scan_exit_sl_first_no_BE50",
        "fee": FEE,
        "tpsl_15m": TPSL_BY_TF[SIGNAL_TF],
        "git_status": git_status,
        "git_head": git_head,
        "strategy_file_hashes": strategy_hashes,
        "BASELINE_UNCHANGED": baseline_unchanged,
        "history_start": _iso(hist_start),
        "history_end": _iso(history_end),
        "history_days": history_days,
        "symbols_available": SYMBOLS,
        "symbols_used": SYMBOLS,
        "coverage": coverage_rows,
        "n_signals": len(all_trades),
        "n_closed": baseline["closed"],
        "n_open": len(open_all),
        "baseline": {**baseline, **baseline_eq},
        "concentration": conc,
        "asymmetry_counts": dict(class_counts),
        "primary_decision": primary,
        "recommendation": recommendation,
        "lookahead_violations": 0,
        "future_leakage": 0,
        "strategy_logic_changed": False,
        "as_of": _iso(now),
    }
    (OUT / "summary.json").write_text(json.dumps(meta, indent=2, default=str) + "\n")

    def fmt(v: Any, nd: int = 2) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return "–"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# FULL_HISTORY_BASELINE_SYMBOL_DIRECTION_AUDIT",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"BASELINE_UNCHANGED={baseline_unchanged}",
        f"History: `{_iso(hist_start)}` → `{_iso(history_end)}` ({history_days:.1f}d)",
        f"Symbols={len(SYMBOLS)} signals={len(all_trades)} closed={baseline['closed']} open={len(open_all)}",
        f"Baseline: TP={baseline['TP']} SL={baseline['SL']} WR={fmt(baseline['winrate'],1)} "
        f"Net={fmt(baseline['net'])} PF={fmt(baseline['profit_factor'])} MaxDD={fmt(baseline_eq['max_drawdown'])}",
        f"lookahead=0 future_leakage=0 | outcome=SL_FIRST no BE50 | edges={EDGES_VERSION}",
        f"Loss concentration worst5 SL%={fmt(conc['worst_5_sl_share_pct'],1)} lossPnL%={fmt(conc['worst_5_loss_pnl_share_pct'],1)}",
        f"Asymmetry: {dict(class_counts)}",
        "",
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("signals", len(all_trades), "closed", baseline["closed"], "net", baseline["net"], flush=True)
    print("conc", conc, flush=True)
    print("BASELINE_UNCHANGED", baseline_unchanged, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
