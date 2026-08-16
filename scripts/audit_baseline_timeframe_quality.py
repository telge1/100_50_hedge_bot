#!/usr/bin/env python3
"""COMPARE_BASELINE_SIGNAL_QUALITY_BY_TIMEFRAME — research-only, no DB writes.

Compares BASELINE_IMMEDIATE Tier-A signal quality across 15m / 30m / 1h / 4h
for APTUSDT + DOGEUSDT. No filters, no threshold tuning, no strategy changes.
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

OUT = ROOT / "results" / "baseline_timeframe_quality_audit"
AS_OF = datetime(2026, 8, 11, 9, 30, 7, 105630, tzinfo=timezone.utc)
SYMBOLS = ("APTUSDT", "DOGEUSDT")
TFS = ("15m", "30m", "1h", "4h")
FEE_PCT = float(PRIMARY_FEE)
LOOKBACK_DAYS = 60
N_BOOT = 2000
RNG = np.random.default_rng(42)

LOOKAHEAD_VIOLATIONS = 0


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


def safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def sample_flag(n: int) -> str:
    if n < 30:
        return "VERY_SMALL_SAMPLE"
    if n < 100:
        return "SMALL_SAMPLE"
    return "BETTER_SAMPLE"


def net_pnls(rows: list[dict]) -> list[float]:
    out = []
    for r in rows:
        if r.get("result") in ("WIN", "LOSS") and r.get("pnl") is not None:
            out.append(float(r["pnl"]) - FEE_PCT)
    return out


def bootstrap_ci(values: list[float], *, stat: str = "mean") -> tuple[float | None, float | None]:
    if len(values) < 5:
        return None, None
    arr = np.asarray(values, dtype=float)
    stats = []
    n = len(arr)
    for _ in range(N_BOOT):
        sample = arr[RNG.integers(0, n, size=n)]
        if stat == "mean":
            stats.append(float(np.mean(sample)))
        elif stat == "winrate":
            # values are 1/0
            stats.append(100.0 * float(np.mean(sample)))
        else:
            stats.append(float(np.mean(sample)))
    lo, hi = np.percentile(stats, [2.5, 97.5])
    return float(lo), float(hi)


def summarize(rows: list[dict], *, label: str = "") -> dict[str, Any]:
    wins = [r for r in rows if r.get("result") == "WIN"]
    losses = [r for r in rows if r.get("result") == "LOSS"]
    opens = [r for r in rows if r.get("result") == "OPEN"]
    closed = wins + losses
    n_closed = len(closed)
    pnls_gross = [float(r["pnl"]) for r in closed if r.get("pnl") is not None]
    pnls_net = [float(r["pnl"]) - FEE_PCT for r in closed if r.get("pnl") is not None]
    maes = [float(r["mae"]) for r in closed if r.get("mae") is not None]
    mfes = [float(r["mfe"]) for r in closed if r.get("mfe") is not None]
    durs = [float(r["duration_seconds"]) for r in closed if r.get("duration_seconds") is not None]

    win_rets = [float(r["pnl"]) - FEE_PCT for r in wins if r.get("pnl") is not None]
    loss_rets = [float(r["pnl"]) - FEE_PCT for r in losses if r.get("pnl") is not None]

    gross_win = sum(max(0.0, x) for x in pnls_gross) if pnls_gross else 0.0
    gross_loss_abs = sum(abs(min(0.0, x)) for x in pnls_gross) if pnls_gross else 0.0
    pf = (gross_win / gross_loss_abs) if gross_loss_abs > 0 else (None if gross_win == 0 else float("inf"))

    early1 = sum(1 for m in maes if m <= -1.0)
    early2 = sum(1 for m in maes if m <= -2.0)
    early3 = sum(1 for m in maes if m <= -3.0)

    wr = (100.0 * len(wins) / n_closed) if n_closed else None
    net = float(sum(pnls_net)) if pnls_net else 0.0
    npt = float(np.mean(pnls_net)) if pnls_net else None
    med_mae = float(np.median(maes)) if maes else None
    med_mfe = float(np.median(mfes)) if mfes else None

    # risk-adjusted
    abs_gross_loss = gross_loss_abs
    net_over_abs_loss = (net / abs_gross_loss) if abs_gross_loss > 0 else None
    mean_w = float(np.mean(win_rets)) if win_rets else None
    mean_l = float(np.mean(loss_rets)) if loss_rets else None
    payoff = (abs(mean_w / mean_l) if mean_w is not None and mean_l is not None and mean_l != 0 else None)
    expectancy = npt
    ret_over_mae = (npt / abs(med_mae)) if npt is not None and med_mae is not None and med_mae != 0 else None

    # outcome counts (BASELINE_IMMEDIATE research path has no BE50)
    tp = sum(1 for r in closed if r.get("exit_reason") == "TP")
    sl = sum(1 for r in closed if r.get("exit_reason") == "SL")
    be = sum(1 for r in closed if r.get("exit_reason") == "BE")

    wr_ci = bootstrap_ci([1.0 if r["result"] == "WIN" else 0.0 for r in closed], stat="winrate") if closed else (None, None)
    npt_ci = bootstrap_ci(pnls_net, stat="mean") if pnls_net else (None, None)

    return {
        "label": label,
        "signals": len(rows),
        "closed_trades": n_closed,
        "open_trades": len(opens),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": wr,
        "gross_return": float(sum(pnls_gross)) if pnls_gross else 0.0,
        "net_return": net,
        "net_per_trade": npt,
        "profit_factor": (None if pf is None or math.isinf(pf) else float(pf)),
        "profit_factor_raw": pf,
        "TP_rate": (100.0 * tp / n_closed) if n_closed else None,
        "SL_rate": (100.0 * sl / n_closed) if n_closed else None,
        "TP_count": tp,
        "SL_count": sl,
        "BE_count": be,
        "OPEN_count": len(opens),
        "mean_trade_return": float(np.mean(pnls_net)) if pnls_net else None,
        "median_trade_return": float(np.median(pnls_net)) if pnls_net else None,
        "mean_MAE": float(np.mean(maes)) if maes else None,
        "median_MAE": med_mae,
        "p90_MAE": float(np.percentile(maes, 90)) if maes else None,
        "p10_MAE_adverse": float(np.percentile(maes, 10)) if maes else None,
        "mean_MFE": float(np.mean(mfes)) if mfes else None,
        "median_MFE": med_mfe,
        "p90_MFE": float(np.percentile(mfes, 90)) if mfes else None,
        "mean_duration_min": (float(np.mean(durs)) / 60.0) if durs else None,
        "median_duration_min": (float(np.median(durs)) / 60.0) if durs else None,
        "EARLY_DD_1PCT_rate": (100.0 * early1 / len(maes)) if maes else None,
        "EARLY_DD_2PCT_rate": (100.0 * early2 / len(maes)) if maes else None,
        "EARLY_DD_3PCT_rate": (100.0 * early3 / len(maes)) if maes else None,
        "net_over_abs_gross_loss": net_over_abs_loss,
        "mean_winner": mean_w,
        "mean_loser": mean_l,
        "winner_loser_payoff_ratio": payoff,
        "expectancy_per_trade": expectancy,
        "return_over_median_MAE": ret_over_mae,
        "wr_ci_lo": wr_ci[0],
        "wr_ci_hi": wr_ci[1],
        "npt_ci_lo": npt_ci[0],
        "npt_ci_hi": npt_ci[1],
        "sample_flag": sample_flag(n_closed),
    }


def signals_per_day(rows: list[dict], span_days: float) -> float | None:
    if span_days <= 0:
        return None
    return len(rows) / span_days


def median_hours_between(rows: list[dict]) -> float | None:
    if len(rows) < 2:
        return None
    ts = sorted(_utc(r["entry_ts"]) for r in rows)
    gaps = [(ts[i + 1] - ts[i]).total_seconds() / 3600.0 for i in range(len(ts) - 1)]
    return float(np.median(gaps)) if gaps else None


def main() -> int:
    global LOOKAHEAD_VIOLATIONS
    OUT.mkdir(parents=True, exist_ok=True)
    now = AS_OF
    start = now - timedelta(days=LOOKBACK_DAYS)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading Tier-A {start.date()} → {_iso(now)} for {SYMBOLS} …", flush=True)
    rows, _ = sig_repo.query_signals(
        start=start,
        end=now,
        tier_a=True,
        time_field="candle_close_time",
        limit=10000,
        offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    api_rows = [
        r
        for r in api_rows
        if str(r.get("symbol", "")).upper() in SYMBOLS and str(r.get("timeframe")) in TFS
    ]
    api_rows.sort(key=lambda r: (_utc(r.get("entry_time") or r.get("candle_close_time")), str(r.get("symbol")), str(r.get("timeframe"))))
    print(f"signals raw={len(api_rows)}", flush=True)

    # Common calendar window = min/max entry across all selected signals
    all_ts = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in api_rows]
    win_start, win_end = min(all_ts), max(all_ts)
    span_days = max((win_end - win_start).total_seconds() / 86400.0, 1e-9)
    print(f"common window {_iso(win_start)} → {_iso(win_end)} ({span_days:.2f}d)", flush=True)

    # Load 1m candles once per symbol with padding for max hold (4h = 10d)
    pad_before = timedelta(days=2)
    pad_after = timedelta(days=12)
    work_1m: dict[str, pd.DataFrame] = {}
    for sym in SYMBOLS:
        raw = candle_repo.get_candles(sym, win_start - pad_before, now + pad_after) or []
        work_1m[sym] = _prepare_1m(pd.DataFrame(raw)) if raw else pd.DataFrame()
        print(f"  {sym}: 1m={len(work_1m[sym])}", flush=True)

    details: list[dict[str, Any]] = []
    for r in api_rows:
        sym = str(r["symbol"]).upper()
        tf = str(r["timeframe"])
        side = str(r["direction"]).upper()
        sid = str(r["signal_id"])
        t0 = _utc(r.get("entry_time") or r.get("candle_close_time"))
        # enforce common window (already from these signals)
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
                # causality: entry bar open must be <= as_of; signal candle closed
                bar_ts = _utc(work.iloc[entry_i]["timestamp"])
                if bar_ts > now:
                    LOOKAHEAD_VIOLATIONS += 1
        if entry_i >= 0 and entry_px is None:
            entry_px = float(work.iloc[entry_i]["open"])

        result, pnl, mae, mfe = "OPEN", None, None, None
        exit_reason, duration = None, None
        if entry_i >= 0 and entry_px is not None and not work.empty:
            sim = _simulate_outcome(
                side=side, entry=float(entry_px), tf=tf, entry_i=entry_i, work=work, as_of=now
            )
            result = sim["result"]
            pnl = sim.get("pnl_pct")
            mae = sim.get("mae_pct")
            mfe = sim.get("mfe_pct")
            exit_reason = sim.get("exit_reason")
            duration = sim.get("duration_seconds")

        details.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "timeframe": tf,
                "direction": side,
                "entry_ts": t0,
                "entry_ts_iso": _iso(t0),
                "entry_price": entry_px,
                "result": result,
                "exit_reason": exit_reason,
                "pnl": pnl,
                "mae": mae,
                "mfe": mfe,
                "duration_seconds": duration,
                "candle_close_time": _iso(r.get("candle_close_time")),
            }
        )

    # Coverage report per TF
    coverage = []
    for tf in TFS:
        sub = [d for d in details if d["timeframe"] == tf]
        if not sub:
            coverage.append({"timeframe": tf, "n": 0, "first": None, "last": None, "span_days": 0})
            continue
        ts = sorted(d["entry_ts"] for d in sub)
        coverage.append(
            {
                "timeframe": tf,
                "n": len(sub),
                "first": _iso(ts[0]),
                "last": _iso(ts[-1]),
                "span_days": (ts[-1] - ts[0]).total_seconds() / 86400.0,
            }
        )

    # --- Primary TF summaries ---
    tf_summaries = []
    for tf in TFS:
        sub = [d for d in details if d["timeframe"] == tf]
        s = summarize(sub, label=tf)
        s["timeframe"] = tf
        s["signals_per_day"] = signals_per_day(sub, span_days)
        s["median_hours_between_signals"] = median_hours_between(sub)
        s["coverage_first"] = next(c["first"] for c in coverage if c["timeframe"] == tf)
        s["coverage_last"] = next(c["last"] for c in coverage if c["timeframe"] == tf)
        s["coverage_span_days"] = next(c["span_days"] for c in coverage if c["timeframe"] == tf)
        tf_summaries.append(s)

    write_csv(OUT / "timeframe_summary.csv", tf_summaries)

    # --- Direction split ---
    dir_rows = []
    for tf in TFS:
        for side in ("ALL", "LONG", "SHORT"):
            sub = [d for d in details if d["timeframe"] == tf]
            if side != "ALL":
                sub = [d for d in sub if d["direction"] == side]
            s = summarize(sub, label=f"{tf}_{side}")
            dir_rows.append(
                {
                    "timeframe": tf,
                    "side": side,
                    "trades": s["closed_trades"],
                    "signals": s["signals"],
                    "winrate": s["winrate"],
                    "net": s["net_return"],
                    "net_per_trade": s["net_per_trade"],
                    "profit_factor": s["profit_factor"],
                    "SL_rate": s["SL_rate"],
                    "median_MAE": s["median_MAE"],
                    "sample_flag": s["sample_flag"],
                }
            )
    write_csv(OUT / "direction_summary.csv", dir_rows)

    # --- Symbol split ---
    sym_rows = []
    for tf in TFS:
        for sym in SYMBOLS:
            sub = [d for d in details if d["timeframe"] == tf and d["symbol"] == sym]
            s = summarize(sub, label=f"{tf}_{sym}")
            sym_rows.append(
                {
                    "timeframe": tf,
                    "symbol": sym,
                    "trades": s["closed_trades"],
                    "signals": s["signals"],
                    "winrate": s["winrate"],
                    "net": s["net_return"],
                    "net_per_trade": s["net_per_trade"],
                    "profit_factor": s["profit_factor"],
                    "SL_rate": s["SL_rate"],
                    "median_MAE": s["median_MAE"],
                    "sample_flag": s["sample_flag"],
                }
            )
    write_csv(OUT / "symbol_summary.csv", sym_rows)

    # --- Time stability: first/second half + thirds ---
    stab_rows = []
    for tf in TFS:
        sub = sorted([d for d in details if d["timeframe"] == tf], key=lambda x: x["entry_ts"])
        if not sub:
            continue
        mid = len(sub) // 2
        blocks = {
            "first_half": sub[:mid] if mid else sub[:1],
            "second_half": sub[mid:] if mid else [],
        }
        # thirds
        n = len(sub)
        t1, t2 = n // 3, 2 * n // 3
        blocks["block_1"] = sub[:t1]
        blocks["block_2"] = sub[t1:t2]
        blocks["block_3"] = sub[t2:]
        for name, blk in blocks.items():
            s = summarize(blk, label=f"{tf}_{name}")
            stab_rows.append(
                {
                    "timeframe": tf,
                    "block": name,
                    "trades": s["closed_trades"],
                    "signals": s["signals"],
                    "winrate": s["winrate"],
                    "net": s["net_return"],
                    "net_per_trade": s["net_per_trade"],
                    "profit_factor": s["profit_factor"],
                    "sample_flag": s["sample_flag"],
                }
            )
    write_csv(OUT / "time_stability.csv", stab_rows)

    # --- Frequency ---
    freq_rows = []
    for tf in TFS:
        all_tf = [d for d in details if d["timeframe"] == tf]
        freq_rows.append(
            {
                "timeframe": tf,
                "scope": "ALL",
                "symbol": "ALL",
                "signals": len(all_tf),
                "signals_per_day": signals_per_day(all_tf, span_days),
                "median_hours_between_signals": median_hours_between(all_tf),
            }
        )
        for sym in SYMBOLS:
            sub = [d for d in all_tf if d["symbol"] == sym]
            freq_rows.append(
                {
                    "timeframe": tf,
                    "scope": "SYMBOL",
                    "symbol": sym,
                    "signals": len(sub),
                    "signals_per_day": signals_per_day(sub, span_days),
                    "median_hours_between_signals": median_hours_between(sub),
                }
            )
    write_csv(OUT / "signal_frequency.csv", freq_rows)

    # --- Drawdown ---
    dd_rows = []
    for tf in TFS:
        sub = [d for d in details if d["timeframe"] == tf]
        s = summarize(sub, label=tf)
        dd_rows.append(
            {
                "timeframe": tf,
                "closed_trades": s["closed_trades"],
                "median_MAE": s["median_MAE"],
                "mean_MAE": s["mean_MAE"],
                "EARLY_DD_1PCT_rate": s["EARLY_DD_1PCT_rate"],
                "EARLY_DD_2PCT_rate": s["EARLY_DD_2PCT_rate"],
                "EARLY_DD_3PCT_rate": s["EARLY_DD_3PCT_rate"],
                "sample_flag": s["sample_flag"],
            }
        )
    write_csv(OUT / "drawdown_summary.csv", dd_rows)

    # --- Detail ---
    detail_out = []
    for d in details:
        detail_out.append(
            {
                **{k: v for k, v in d.items() if k != "entry_ts"},
                "entry_ts": d["entry_ts_iso"],
            }
        )
    write_csv(OUT / "signal_detail.csv", detail_out)

    # --- Comparisons vs 15m ---
    by_tf = {s["timeframe"]: s for s in tf_summaries}
    base = by_tf["15m"]

    def delta(a: float | None, b: float | None) -> float | None:
        if a is None or b is None:
            return None
        return float(a) - float(b)

    comps = {}
    for tf in ("30m", "1h", "4h"):
        s = by_tf[tf]
        comps[tf] = {
            "wr_delta": delta(s["winrate"], base["winrate"]),
            "npt_delta": delta(s["net_per_trade"], base["net_per_trade"]),
            "pf_delta": delta(s["profit_factor"], base["profit_factor"]),
            "sl_delta": delta(s["SL_rate"], base["SL_rate"]),
            "mae_delta": delta(s["median_MAE"], base["median_MAE"]),  # less negative = better
        }

    # Rankings (closed trades only quality)
    def rank_key(metric: str, *, higher_better: bool = True):
        scored = []
        for s in tf_summaries:
            v = s.get(metric)
            if v is None or s["closed_trades"] == 0:
                continue
            scored.append((s["timeframe"], float(v)))
        scored.sort(key=lambda x: x[1], reverse=higher_better)
        return scored[0][0] if scored else None

    rankings = {
        "BEST_WINRATE_TF": rank_key("winrate", higher_better=True),
        "BEST_NET_PER_TRADE_TF": rank_key("net_per_trade", higher_better=True),
        "BEST_PROFIT_FACTOR_TF": rank_key("profit_factor", higher_better=True),
        "LOWEST_SL_RATE_TF": rank_key("SL_rate", higher_better=False),
        "LOWEST_MAE_TF": rank_key("median_MAE", higher_better=True),  # MAE less negative better
        "BEST_FREQUENCY_TF": rank_key("signals_per_day", higher_better=True),
    }

    # Stability: npt same sign in first/second half
    def half_stable(tf: str) -> bool | None:
        fh = next((r for r in stab_rows if r["timeframe"] == tf and r["block"] == "first_half"), None)
        sh = next((r for r in stab_rows if r["timeframe"] == tf and r["block"] == "second_half"), None)
        if not fh or not sh or fh["net_per_trade"] is None or sh["net_per_trade"] is None:
            return None
        if fh["trades"] < 5 or sh["trades"] < 5:
            return None
        return (fh["net_per_trade"] > 0 and sh["net_per_trade"] > 0) or (
            fh["net_per_trade"] < 0 and sh["net_per_trade"] < 0
        )

    # Direction/symbol mixed?
    def mixed_direction(tf: str) -> bool:
        lng = next(r for r in dir_rows if r["timeframe"] == tf and r["side"] == "LONG")
        sht = next(r for r in dir_rows if r["timeframe"] == tf and r["side"] == "SHORT")
        if lng["net_per_trade"] is None or sht["net_per_trade"] is None:
            return False
        if lng["trades"] < 3 or sht["trades"] < 3:
            return False
        return (lng["net_per_trade"] > 0) != (sht["net_per_trade"] > 0)

    def mixed_symbol(tf: str) -> bool:
        a = next(r for r in sym_rows if r["timeframe"] == tf and r["symbol"] == "APTUSDT")
        d = next(r for r in sym_rows if r["timeframe"] == tf and r["symbol"] == "DOGEUSDT")
        if a["net_per_trade"] is None or d["net_per_trade"] is None:
            return False
        if a["trades"] < 3 or d["trades"] < 3:
            return False
        return (a["net_per_trade"] > 0) != (d["net_per_trade"] > 0)

    # Quality score relative to 15m for 30m/1h
    def clearly_better(tf: str) -> bool:
        c = comps[tf]
        s = by_tf[tf]
        if s["closed_trades"] < 20:
            return False
        # need better npt AND (better WR or better PF) AND not worse MAE by much
        if c["npt_delta"] is None or c["npt_delta"] <= 0.05:
            return False
        wr_ok = c["wr_delta"] is not None and c["wr_delta"] >= 3.0
        pf_ok = c["pf_delta"] is not None and c["pf_delta"] >= 0.15
        mae_ok = c["mae_delta"] is None or c["mae_delta"] >= -0.15  # not much worse (MAE more neg = worse)
        return (wr_ok or pf_ok) and mae_ok

    def slightly_better(tf: str) -> bool:
        c = comps[tf]
        if by_tf[tf]["closed_trades"] < 10:
            return False
        if c["npt_delta"] is None:
            return False
        return c["npt_delta"] > 0 and (
            (c["wr_delta"] or 0) > 0 or (c["pf_delta"] or 0) > 0 or (c["mae_delta"] or 0) > 0
        )

    any_insufficient = all(by_tf[tf]["closed_trades"] < 30 for tf in ("30m", "1h", "4h"))
    m30_clear = clearly_better("30m")
    h1_clear = clearly_better("1h")
    m30_slight = slightly_better("30m")
    h1_slight = slightly_better("1h")
    mixed = any(mixed_direction(tf) or mixed_symbol(tf) for tf in ("30m", "1h") if by_tf[tf]["closed_trades"] >= 10)

    # Is 15m best on net_per_trade?
    npt_rank = sorted(
        [(s["timeframe"], s["net_per_trade"]) for s in tf_summaries if s["net_per_trade"] is not None],
        key=lambda x: x[1],
        reverse=True,
    )
    best_quality_tf = npt_rank[0][0] if npt_rank else "15m"
    best_freq_tf = rankings["BEST_FREQUENCY_TF"] or "15m"

    # Prefer overall TF quality over subgroup mixed flags when 15m dominates.
    fifteen_dominates = (
        best_quality_tf == "15m"
        and rankings["BEST_WINRATE_TF"] == "15m"
        and rankings["BEST_PROFIT_FACTOR_TF"] == "15m"
        and (comps["30m"]["npt_delta"] or 0) < 0
        and (comps["1h"]["npt_delta"] or 0) <= 0
    )

    if m30_clear and h1_clear:
        primary = "30M_AND_1H_BOTH_HIGHER_QUALITY"
    elif m30_clear:
        primary = "30M_SIGNALS_CLEARLY_BETTER_THAN_15M"
    elif h1_clear:
        primary = "1H_SIGNALS_CLEARLY_BETTER_THAN_15M"
    elif fifteen_dominates:
        primary = "15M_REMAINS_BEST"
    elif mixed and (m30_slight or h1_slight):
        # higher TF only looks ok in some sides/symbols
        primary = "QUALITY_MIXED_BY_DIRECTION_OR_SYMBOL"
    elif m30_slight or h1_slight:
        primary = "HIGHER_TF_ONLY_SLIGHTLY_BETTER"
    elif any_insufficient:
        primary = "INSUFFICIENT_SAMPLE"
    elif best_quality_tf == "15m":
        primary = "15M_REMAINS_BEST"
    else:
        primary = "INSUFFICIENT_SAMPLE"

    # Recommendation
    if primary in ("30M_AND_1H_BOTH_HIGHER_QUALITY",) and (
        by_tf["30m"]["sample_flag"] != "VERY_SMALL_SAMPLE" or by_tf["1h"]["sample_flag"] != "VERY_SMALL_SAMPLE"
    ):
        recommendation = "30M_1H_WORTH_MULTI_COIN_EXPANSION_TEST"
    elif primary == "30M_SIGNALS_CLEARLY_BETTER_THAN_15M":
        recommendation = "FOCUS_MORE_ON_30M"
    elif primary == "1H_SIGNALS_CLEARLY_BETTER_THAN_15M":
        recommendation = "FOCUS_MORE_ON_1H"
    elif primary == "HIGHER_TF_ONLY_SLIGHTLY_BETTER":
        # slight edge but small sample → expansion test only if npt clearly higher
        if (comps["30m"]["npt_delta"] or 0) > 0.1 or (comps["1h"]["npt_delta"] or 0) > 0.1:
            recommendation = "30M_1H_WORTH_MULTI_COIN_EXPANSION_TEST"
        else:
            recommendation = "KEEP_15M_PRIMARY"
    elif primary == "INSUFFICIENT_SAMPLE":
        # If higher TF look better on npt despite small n, still flag expansion test cautiously
        if (comps["30m"]["npt_delta"] or 0) > 0.15 or (comps["1h"]["npt_delta"] or 0) > 0.15:
            recommendation = "30M_1H_WORTH_MULTI_COIN_EXPANSION_TEST"
        else:
            recommendation = "KEEP_15M_PRIMARY"
    elif primary == "QUALITY_MIXED_BY_DIRECTION_OR_SYMBOL":
        recommendation = "KEEP_15M_PRIMARY"
    else:
        recommendation = "KEEP_15M_PRIMARY"

    meta = {
        "task": "COMPARE_BASELINE_SIGNAL_QUALITY_BY_TIMEFRAME",
        "variant": "BASELINE_IMMEDIATE",
        "symbols": list(SYMBOLS),
        "timeframes": list(TFS),
        "as_of": _iso(now),
        "window_start": _iso(win_start),
        "window_end": _iso(win_end),
        "span_days": span_days,
        "fee_pct": FEE_PCT,
        "outcome_path": "1m_replay_scan_exit_sl_first_no_BE50",
        "lookahead_violations": LOOKAHEAD_VIOLATIONS,
        "n_signals": len(details),
        "coverage": coverage,
        "primary_decision": primary,
        "recommendation": recommendation,
        "rankings": rankings,
        "comparisons_vs_15m": comps,
        "half_stability": {tf: half_stable(tf) for tf in TFS},
        "strategy_logic_changed": False,
    }
    (OUT / "summary.json").write_text(json.dumps({"meta": meta, "timeframe_summary": tf_summaries, "comparisons_vs_15m": comps, "rankings": rankings, "direction": dir_rows, "symbol": sym_rows}, indent=2, default=str) + "\n")

    def fmt(v: Any, nd: int = 2) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if math.isnan(v) or math.isinf(v):
                return "–"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# COMPARE_BASELINE_SIGNAL_QUALITY_BY_TIMEFRAME",
        "",
        f"Primary: `{primary}`",
        f"Recommendation: `{recommendation}`",
        f"Variant: `BASELINE_IMMEDIATE` | symbols={','.join(SYMBOLS)} | fee={FEE_PCT}",
        f"Window: `{_iso(win_start)}` → `{_iso(win_end)}` ({span_days:.2f}d)",
        f"lookahead_violations = `{LOOKAHEAD_VIOLATIONS}`",
        f"Outcome: 1m replay, SL-first, no BE50 (research BASELINE_IMMEDIATE)",
        "",
        "## Haupttabelle",
        "",
        "| TF | Trades | WR | Net | Net/Trade | PF | SL Rate | Median MAE | Signals/Day | Sample |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for s in tf_summaries:
        lines.append(
            f"| {s['timeframe']} | {s['closed_trades']} | {fmt(s['winrate'],1)} | {fmt(s['net_return'],2)} | "
            f"{fmt(s['net_per_trade'],3)} | {fmt(s['profit_factor'],2)} | {fmt(s['SL_rate'],1)} | "
            f"{fmt(s['median_MAE'],3)} | {fmt(s['signals_per_day'],2)} | {s['sample_flag']} |"
        )
    lines += ["", "## 30m / 1h vs 15m", ""]
    for tf in ("30m", "1h"):
        c = comps[tf]
        lines.append(
            f"- **{tf} vs 15m**: WRΔ={fmt(c['wr_delta'],1)} nptΔ={fmt(c['npt_delta'],3)} "
            f"PFΔ={fmt(c['pf_delta'],2)} SLΔ={fmt(c['sl_delta'],1)} MAEΔ={fmt(c['mae_delta'],3)}"
        )
    lines += ["", "## Rankings", ""]
    for k, v in rankings.items():
        lines.append(f"- {k}: `{v}`")
    lines += ["", "## Coverage by TF", ""]
    for c in coverage:
        lines.append(f"- {c['timeframe']}: n={c['n']} first={c['first']} last={c['last']} span={fmt(c['span_days'],2)}d")
    lines += ["", "## Strategy Logic Changed", "", "`NO`", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    for s in tf_summaries:
        print(
            f"  {s['timeframe']}: closed={s['closed_trades']} WR={s['winrate']} npt={s['net_per_trade']} "
            f"PF={s['profit_factor']} SL={s['SL_rate']} MAE={s['median_MAE']} spd={s['signals_per_day']}",
            flush=True,
        )
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
