#!/usr/bin/env python3
"""COMPARE_15M_ENTRY_WINDOW_30M_VS_60M — research-only, no DB writes.

Same 168h Tier-A dataset as timeout audit; only signal_tf=15m.
Rule unchanged: WAIT_1M_EXTREME_TURN_CROSS (20/80 + K/D cross).
"""

from __future__ import annotations

import csv
import json
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
from signal_generator.research.one_m_entry_timing.constants import (  # noqa: E402
    VARIANT_WAIT_1M_EXTREME_TURN_CROSS,
)
from signal_generator.research.one_m_entry_timing.timing import (  # noqa: E402
    _prepare_1m,
    _simulate_outcome,
    _utc,
)
from signal_generator.strategy.wave_fade.parameters import STOCH_HIGH_K, STOCH_LOW_K  # noqa: E402

# Reuse helpers from prior audit script
sys.path.insert(0, str(ROOT / "scripts"))
from audit_no_entry_timeout_root_cause import (  # noqa: E402
    HOURS,
    find_extreme_and_turn,
    _iso,
    _median,
    _pctile,
)

OUT = ROOT / "results" / "1m_stoch_entry_timing_audit" / "30m_vs_60m"
VARIANT = VARIANT_WAIT_1M_EXTREME_TURN_CROSS
WINDOWS = (30, 60)
FEE_PCT = 0.11  # same primary fee as freeze (for net reporting only)


def _mean(xs: list[float]) -> float | None:
    if not xs:
        return None
    return float(np.mean(np.asarray(xs, dtype=float)))


def profit_factor(pnls: list[float]) -> float | None:
    gp = sum(p for p in pnls if p > 0)
    gl = sum(-p for p in pnls if p < 0)
    if gl == 0:
        return None if gp == 0 else float("inf")
    return float(gp / gl)


def age_bucket(mins: float) -> str:
    if mins <= 15:
        return "0-15m"
    if mins <= 30:
        return "15-30m"
    if mins <= 45:
        return "30-45m"
    if mins <= 60:
        return "45-60m"
    return ">60m"


def delay_bucket(mins: float) -> str:
    return age_bucket(mins)  # same edges for signal→entry


def summarize_trades(trades: list[dict[str, Any]], *, n_signals: int) -> dict[str, Any]:
    triggered = [t for t in trades if t.get("triggered")]
    timeouts = [t for t in trades if not t.get("triggered")]
    wins = [t for t in triggered if t.get("result") == "WIN"]
    losses = [t for t in triggered if t.get("result") == "LOSS"]
    opens = [t for t in triggered if t.get("result") == "OPEN"]
    pnls = [float(t["pnl"]) for t in triggered if t.get("pnl") is not None]
    nets = [float(t["pnl"]) - FEE_PCT for t in triggered if t.get("pnl") is not None]
    maes = [float(t["mae"]) for t in triggered if t.get("mae") is not None]
    mfes = [float(t["mfe"]) for t in triggered if t.get("mfe") is not None]
    waits = [float(t["wait_minutes"]) for t in triggered if t.get("wait_minutes") is not None]
    closed = len(wins) + len(losses)
    return {
        "signals": n_signals,
        "triggered": len(triggered),
        "timeouts": len(timeouts),
        "coverage_pct": (100.0 * len(triggered) / n_signals) if n_signals else None,
        "wins": len(wins),
        "losses": len(losses),
        "open": len(opens),
        "winrate": (100.0 * len(wins) / closed) if closed else None,
        "gross_return": float(sum(pnls)) if pnls else 0.0,
        "net_return": float(sum(nets)) if nets else 0.0,
        "profit_factor": profit_factor(pnls) if pnls else None,
        "sl_rate": (100.0 * len(losses) / closed) if closed else None,
        "tp_rate": (100.0 * len(wins) / closed) if closed else None,
        "median_mae": _median(maes),
        "mean_mae": _mean(maes),
        "p90_mae": _pctile(maes, 90),
        "median_mfe": _median(mfes),
        "mean_mfe": _mean(mfes),
        "median_wait": _median(waits),
        "mean_wait": _mean(waits),
        "p90_wait": _pctile(waits, 90),
    }


def eval_window(
    work: pd.DataFrame,
    *,
    side: str,
    tf: str,
    t0: datetime,
    start_i: int,
    window_m: int,
    now: datetime,
    baseline_px: float | None,
) -> dict[str, Any]:
    deadline = t0 + timedelta(minutes=window_m)
    if work.empty or start_i >= len(work):
        return {
            "triggered": False,
            "result": "NO_ENTRY_TIMEOUT",
            "entry_ts": None,
            "entry_price": None,
            "entry_i": None,
            "wait_minutes": None,
            "pnl": None,
            "mae": None,
            "mfe": None,
            "exit_reason": None,
        }
    scan = find_extreme_and_turn(work, side=side, start_i=start_i, scan_end_ts=deadline)
    if scan["entry_i"] is None:
        return {
            "triggered": False,
            "result": "NO_ENTRY_TIMEOUT",
            "entry_ts": None,
            "entry_price": None,
            "entry_i": None,
            "wait_minutes": None,
            "pnl": None,
            "mae": None,
            "mfe": None,
            "exit_reason": None,
        }
    ei = scan["entry_i"]
    entry_px = float(work.iloc[ei]["open"])
    entry_ts = _utc(work.iloc[ei]["timestamp"])
    sim = _simulate_outcome(
        side=side, entry=entry_px, tf=tf, entry_i=ei, work=work, as_of=now
    )
    return {
        "triggered": True,
        "result": sim["result"],
        "entry_ts": entry_ts,
        "entry_price": entry_px,
        "entry_i": ei,
        "wait_minutes": (entry_ts - t0).total_seconds() / 60.0,
        "pnl": sim.get("pnl_pct"),
        "mae": sim.get("mae_pct"),
        "mfe": sim.get("mfe_pct"),
        "exit_reason": sim.get("exit_reason"),
        "extreme_i": scan["extreme_i"],
        "turn_i": scan["turn_i"],
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=HOURS)

    ch = setup_clickhouse(settings=get_clickhouse_settings())
    sig_repo = SignalRepository(ch)
    candle_repo = CandleRepository(ch)

    print(f"Loading Tier-A {start.isoformat()} → {now.isoformat()} …", flush=True)
    rows, total = sig_repo.query_signals(
        start=start,
        end=now,
        tier_a=True,
        timeframe="15m",
        time_field="candle_close_time",
        limit=5000,
        offset=0,
    )
    api_rows = [_signal_row_to_api(r) for r in rows]
    api_rows = [r for r in api_rows if str(r.get("timeframe")) == "15m"]
    print(f"15m Tier-A: {len(api_rows)} (query total={total})", flush=True)

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for r in api_rows:
        by_sym[str(r["symbol"]).upper()].append(r)

    candle_cache: dict[str, pd.DataFrame] = {}
    pad_before = timedelta(hours=6)
    pad_after = timedelta(minutes=60 + 24 * 60)
    for sym, items in by_sym.items():
        t0s = [_utc(r.get("entry_time") or r.get("candle_close_time")) for r in items]
        a, b = min(t0s) - pad_before, max(t0s) + pad_after
        raw = candle_repo.get_candles(sym, a, b)
        df = pd.DataFrame(raw) if raw else pd.DataFrame()
        candle_cache[sym] = _prepare_1m(df) if not df.empty else df
        print(f"  candles {sym}: {len(candle_cache[sym])}", flush=True)

    trades_30: list[dict[str, Any]] = []
    trades_60: list[dict[str, Any]] = []
    trades_base: list[dict[str, Any]] = []
    pairwise_rows: list[dict[str, Any]] = []
    late_rows: list[dict[str, Any]] = []  # timeout@30 but trigger@60
    age_bucket_trades: dict[str, list[dict]] = defaultdict(list)
    delay_bucket_trades: dict[str, list[dict]] = defaultdict(list)

    winners_missed_30 = losers_avoided_30 = 0
    winners_missed_60 = losers_avoided_60 = 0
    sl_to_tp = tp_to_sl = 0  # 30 vs 60 outcome flips among both-triggered
    mismatches = 0

    for r in api_rows:
        sym = str(r["symbol"]).upper()
        side = str(r["direction"]).upper()
        sid = str(r["signal_id"])
        signal_ts = _utc(r.get("candle_close_time") or r.get("generated_at"))
        t0 = _utc(r.get("entry_time") or signal_ts)
        try:
            baseline_px = float(r["entry_price"]) if r.get("entry_price") is not None else None
        except (TypeError, ValueError):
            baseline_px = None
        work = candle_cache.get(sym, pd.DataFrame())
        start_i = 0
        if not work.empty:
            start_i = int(work["timestamp"].searchsorted(pd.Timestamp(t0), side="left"))

        # Baseline immediate
        base = {
            "triggered": False,
            "result": "OPEN",
            "pnl": None,
            "mae": None,
            "mfe": None,
            "wait_minutes": 0.0,
            "entry_ts": None,
            "entry_price": None,
            "exit_reason": None,
        }
        if not work.empty and start_i < len(work):
            bpx = baseline_px if baseline_px else float(work.iloc[start_i]["open"])
            sim = _simulate_outcome(
                side=side, entry=bpx, tf="15m", entry_i=start_i, work=work, as_of=now
            )
            base = {
                "triggered": True,
                "result": sim["result"],
                "pnl": sim.get("pnl_pct"),
                "mae": sim.get("mae_pct"),
                "mfe": sim.get("mfe_pct"),
                "wait_minutes": 0.0,
                "entry_ts": _utc(work.iloc[start_i]["timestamp"]),
                "entry_price": bpx,
                "exit_reason": sim.get("exit_reason"),
            }
        trades_base.append({**base, "signal_id": sid})

        e30 = eval_window(
            work, side=side, tf="15m", t0=t0, start_i=start_i, window_m=30, now=now, baseline_px=baseline_px
        )
        e60 = eval_window(
            work, side=side, tf="15m", t0=t0, start_i=start_i, window_m=60, now=now, baseline_px=baseline_px
        )
        trades_30.append({**e30, "signal_id": sid, "symbol": sym, "direction": side})
        trades_60.append({**e60, "signal_id": sid, "symbol": sym, "direction": side})

        b_res = base["result"]
        if not e30["triggered"]:
            if b_res == "WIN":
                winners_missed_30 += 1
            elif b_res == "LOSS":
                losers_avoided_30 += 1
        if not e60["triggered"]:
            if b_res == "WIN":
                winners_missed_60 += 1
            elif b_res == "LOSS":
                losers_avoided_60 += 1

        # Pairwise when both trigger
        if e30["triggered"] and e60["triggered"]:
            same_ts = e30["entry_ts"] == e60["entry_ts"]
            same_px = (
                e30["entry_price"] is not None
                and e60["entry_price"] is not None
                and abs(float(e30["entry_price"]) - float(e60["entry_price"])) < 1e-12
            )
            mismatch = not (same_ts and same_px)
            if mismatch:
                mismatches += 1
            if e30["result"] == "LOSS" and e60["result"] == "WIN":
                sl_to_tp += 1
            if e30["result"] == "WIN" and e60["result"] == "LOSS":
                tp_to_sl += 1
            pairwise_rows.append(
                {
                    "signal_id": sid,
                    "symbol": sym,
                    "direction": side,
                    "entry_ts_30m": _iso(e30["entry_ts"]),
                    "entry_price_30m": e30["entry_price"],
                    "entry_ts_60m": _iso(e60["entry_ts"]),
                    "entry_price_60m": e60["entry_price"],
                    "same_entry": (not mismatch),
                    "result_30m": e30["result"],
                    "result_60m": e60["result"],
                    "pnl_30m": e30["pnl"],
                    "pnl_60m": e60["pnl"],
                }
            )

        # Additional 60m-only entries
        if (not e30["triggered"]) and e60["triggered"]:
            delay = e60["wait_minutes"]
            late_rows.append(
                {
                    "signal_id": sid,
                    "symbol": sym,
                    "direction": side,
                    "signal_ts": _iso(signal_ts),
                    "baseline_entry_ts": _iso(t0),
                    "entry_ts_60m": _iso(e60["entry_ts"]),
                    "entry_price_60m": e60["entry_price"],
                    "entry_delay_minutes": delay,
                    "signal_age_minutes": (e60["entry_ts"] - signal_ts).total_seconds() / 60.0
                    if e60["entry_ts"]
                    else None,
                    "result_60m": e60["result"],
                    "exit_reason_60m": e60.get("exit_reason"),
                    "pnl_60m": e60["pnl"],
                    "mae_60m": e60["mae"],
                    "mfe_60m": e60["mfe"],
                    "baseline_outcome": b_res,
                    "baseline_pnl": base["pnl"],
                    "baseline_mae": base["mae"],
                }
            )

        # Signal age / delay buckets for ALL 60m triggered
        if e60["triggered"] and e60["entry_ts"] is not None:
            age = (e60["entry_ts"] - signal_ts).total_seconds() / 60.0
            wait = e60["wait_minutes"] or 0.0
            rec = {
                "result": e60["result"],
                "pnl": e60["pnl"],
                "mae": e60["mae"],
                "mfe": e60["mfe"],
                "exit_reason": e60.get("exit_reason"),
                "wait_minutes": wait,
            }
            age_bucket_trades[age_bucket(age)].append(rec)
            delay_bucket_trades[delay_bucket(wait)].append(rec)

    n = len(api_rows)
    s30 = summarize_trades(trades_30, n_signals=n)
    s60 = summarize_trades(trades_60, n_signals=n)
    sbase = summarize_trades(trades_base, n_signals=n)
    s30["winners_missed"] = winners_missed_30
    s30["losers_avoided"] = losers_avoided_30
    s30["sl_to_tp"] = sl_to_tp  # only meaningful pairwise; attach to both
    s30["tp_to_sl"] = tp_to_sl
    s60["winners_missed"] = winners_missed_60
    s60["losers_avoided"] = losers_avoided_60
    s60["sl_to_tp"] = sl_to_tp
    s60["tp_to_sl"] = tp_to_sl
    sbase["winners_missed"] = None
    sbase["losers_avoided"] = None
    sbase["sl_to_tp"] = None
    sbase["tp_to_sl"] = None

    # Late group quality
    late_pnls = [float(r["pnl_60m"]) for r in late_rows if r.get("pnl_60m") is not None]
    late_wins = sum(1 for r in late_rows if r["result_60m"] == "WIN")
    late_losses = sum(1 for r in late_rows if r["result_60m"] == "LOSS")
    late_closed = late_wins + late_losses
    late_delays = [float(r["entry_delay_minutes"]) for r in late_rows if r.get("entry_delay_minutes") is not None]
    late_maes = [float(r["mae_60m"]) for r in late_rows if r.get("mae_60m") is not None]
    late_mfes = [float(r["mfe_60m"]) for r in late_rows if r.get("mfe_60m") is not None]
    late_base_w = sum(1 for r in late_rows if r["baseline_outcome"] == "WIN")
    late_base_l = sum(1 for r in late_rows if r["baseline_outcome"] == "LOSS")
    late_tp = sum(1 for r in late_rows if r.get("exit_reason_60m") == "TP" or r["result_60m"] == "WIN")
    late_sl = sum(1 for r in late_rows if r.get("exit_reason_60m") == "SL" or r["result_60m"] == "LOSS")
    late_open = sum(1 for r in late_rows if r["result_60m"] == "OPEN")

    late_summary = {
        "count": len(late_rows),
        "wins": late_wins,
        "losses": late_losses,
        "open": late_open,
        "winrate": (100.0 * late_wins / late_closed) if late_closed else None,
        "net": float(sum(late_pnls) - FEE_PCT * len(late_pnls)) if late_pnls else 0.0,
        "gross": float(sum(late_pnls)) if late_pnls else 0.0,
        "profit_factor": profit_factor(late_pnls) if late_pnls else None,
        "median_entry_delay": _median(late_delays),
        "mean_entry_delay": _mean(late_delays),
        "median_MAE": _median(late_maes),
        "median_MFE": _median(late_mfes),
        "baseline_winners": late_base_w,
        "baseline_losers": late_base_l,
        "timed_TP": late_tp,
        "timed_SL": late_sl,
        "timed_OPEN": late_open,
    }

    def bucket_table(src: dict[str, list[dict]]) -> list[dict]:
        out = []
        for b in ("0-15m", "15-30m", "30-45m", "45-60m", ">60m"):
            items = src.get(b, [])
            if not items and b == ">60m":
                continue
            wins = sum(1 for t in items if t["result"] == "WIN")
            losses = sum(1 for t in items if t["result"] == "LOSS")
            closed = wins + losses
            pnls = [float(t["pnl"]) for t in items if t.get("pnl") is not None]
            maes = [float(t["mae"]) for t in items if t.get("mae") is not None]
            mfes = [float(t["mfe"]) for t in items if t.get("mfe") is not None]
            out.append(
                {
                    "bucket": b,
                    "entries": len(items),
                    "winrate": (100.0 * wins / closed) if closed else None,
                    "net": float(sum(pnls) - FEE_PCT * len(pnls)) if pnls else 0.0,
                    "net_per_trade": (float(sum(pnls) / len(pnls)) if pnls else None),
                    "sl_rate": (100.0 * losses / closed) if closed else None,
                    "median_MAE": _median(maes),
                    "median_MFE": _median(mfes),
                }
            )
        return out

    age_rows = bucket_table(age_bucket_trades)
    delay_rows = bucket_table(delay_bucket_trades)

    # Decision
    n_late = len(late_rows)
    wr30 = s30["winrate"]
    wr60 = s60["winrate"]
    net30 = s30["net_return"]
    net60 = s60["net_return"]
    cov30 = s30["coverage_pct"] or 0
    cov60 = s60["coverage_pct"] or 0

    b_3045 = next((x for x in delay_rows if x["bucket"] == "30-45m"), None)
    b_4560 = next((x for x in delay_rows if x["bucket"] == "45-60m"), None)

    late_edge_ok = True
    if b_3045 and b_3045["entries"] >= 5 and b_3045["winrate"] is not None:
        if wr30 is not None and b_3045["winrate"] < wr30 - 10:
            late_edge_ok = False
    if b_4560 and b_4560["entries"] >= 5 and b_4560["winrate"] is not None:
        if wr30 is not None and b_4560["winrate"] < wr30 - 10:
            late_edge_ok = False
        if b_4560["sl_rate"] is not None and (s30["sl_rate"] or 0) and b_4560["sl_rate"] > (s30["sl_rate"] or 0) + 15:
            late_edge_ok = False

    if n < 50:
        primary = "INSUFFICIENT_SAMPLE"
        recommendation = "NEEDS_MORE_HISTORY"
    elif mismatches > 0:
        # structural bug — still report metrics but flag
        primary = "INSUFFICIENT_SAMPLE" if n < 80 else "30M_AND_60M_EFFECTIVELY_EQUAL"
        recommendation = "NEEDS_MORE_HISTORY"
    elif abs(cov60 - cov30) < 3 and abs((net60 or 0) - (net30 or 0)) < 2 and n_late <= 3:
        primary = "30M_AND_60M_EFFECTIVELY_EQUAL"
        recommendation = "KEEP_30M"
    elif n_late >= 5 and not late_edge_ok:
        primary = "60M_LATE_ENTRIES_LOSE_EDGE"
        recommendation = "KEEP_30M"
    elif n_late >= 5 and late_summary["winrate"] is not None and wr30 is not None:
        if late_summary["gross"] > 0 and late_summary["winrate"] >= (wr30 - 5) and (net60 or 0) > (net30 or 0) + 1:
            if (net60 or 0) > (net30 or 0) + 5 and (cov60 - cov30) >= 10:
                primary = "60M_CLEARLY_BETTER"
                recommendation = "USE_60M_FOR_15M_RESEARCH"
            else:
                primary = "60M_ADDS_GOOD_LATE_ENTRIES"
                recommendation = "USE_60M_FOR_15M_RESEARCH"
        elif (net60 or 0) + 1 < (net30 or 0) and (s60["sl_rate"] or 0) > (s30["sl_rate"] or 0):
            primary = "30M_BETTER_FILTER"
            recommendation = "KEEP_30M"
        elif late_summary["gross"] <= 0 or (
            late_summary["winrate"] is not None and late_summary["winrate"] < (wr30 or 50) - 8
        ):
            primary = "30M_BETTER_FILTER"
            recommendation = "KEEP_30M"
        else:
            primary = "60M_ADDS_GOOD_LATE_ENTRIES"
            recommendation = "USE_60M_FOR_15M_RESEARCH"
    elif (net60 or 0) > (net30 or 0) + 3 and (cov60 - cov30) >= 8:
        primary = "60M_CLEARLY_BETTER"
        recommendation = "USE_60M_FOR_15M_RESEARCH"
    elif (net30 or 0) > (net60 or 0) + 1:
        primary = "30M_BETTER_FILTER"
        recommendation = "KEEP_30M"
    else:
        primary = "30M_AND_60M_EFFECTIVELY_EQUAL"
        recommendation = "KEEP_30M"

    # Override if late buckets clearly lose edge
    if primary in ("60M_CLEARLY_BETTER", "60M_ADDS_GOOD_LATE_ENTRIES") and not late_edge_ok:
        primary = "60M_LATE_ENTRIES_LOSE_EDGE"
        recommendation = "KEEP_30M"

    # Artifacts
    summary_rows = [
        {"variant": "BASELINE_IMMEDIATE", **{k: sbase[k] for k in sbase}},
        {"variant": "WAIT_CROSS_30M", **{k: s30[k] for k in s30}},
        {"variant": "WAIT_CROSS_60M", **{k: s60[k] for k in s60}},
    ]
    write_csv(OUT / "summary.csv", summary_rows)
    write_csv(OUT / "late_30_to_60_trades.csv", late_rows)
    write_csv(OUT / "signal_age_buckets.csv", age_rows)
    write_csv(OUT / "delay_edge_buckets.csv", delay_rows)
    write_csv(OUT / "pairwise_comparison.csv", pairwise_rows)

    payload = {
        "primary_decision": primary,
        "recommendation": recommendation,
        "hours": HOURS,
        "n_15m_signals": n,
        "variant": VARIANT,
        "fee_pct_for_net": FEE_PCT,
        "baseline": sbase,
        "window_30m": s30,
        "window_60m": s60,
        "late_30_to_60": late_summary,
        "pairwise_entry_mismatches": mismatches,
        "pairwise_both_triggered": len(pairwise_rows),
        "signal_age_buckets": age_rows,
        "delay_edge_buckets": delay_rows,
        "as_of": _iso(now),
    }
    (OUT / "summary.json").write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")

    def fmt(v: Any, nd: int = 1) -> str:
        if v is None:
            return "–"
        if isinstance(v, float):
            if abs(v) == float("inf"):
                return "inf"
            return f"{v:.{nd}f}"
        return str(v)

    lines = [
        "# 15m Entry Window: 30m vs 60m",
        "",
        f"Variant rule: `{VARIANT}` (unchanged 20/80 + K/D cross)",
        f"Dataset: last **{HOURS}h** Tier-A, **15m only** (n={n})",
        "",
        "## Primary Decision",
        "",
        f"`{primary}`",
        "",
        f"Recommendation: `{recommendation}`",
        "",
        "## Main table",
        "",
        "| Variant | Coverage | Winrate | Net | PF | SL Rate | Median MAE | Median Wait |",
        "| ------- | -------: | ------: | --: | -: | ------: | ---------: | ----------: |",
        f"| BASELINE | {fmt(sbase['coverage_pct'])} | {fmt(sbase['winrate'])} | {fmt(sbase['net_return'])} | {fmt(sbase['profit_factor'])} | {fmt(sbase['sl_rate'])} | {fmt(sbase['median_mae'], 3)} | {fmt(sbase['median_wait'])} |",
        f"| 30m | {fmt(s30['coverage_pct'])} | {fmt(s30['winrate'])} | {fmt(s30['net_return'])} | {fmt(s30['profit_factor'])} | {fmt(s30['sl_rate'])} | {fmt(s30['median_mae'], 3)} | {fmt(s30['median_wait'])} |",
        f"| 60m | {fmt(s60['coverage_pct'])} | {fmt(s60['winrate'])} | {fmt(s60['net_return'])} | {fmt(s60['profit_factor'])} | {fmt(s60['sl_rate'])} | {fmt(s60['median_mae'], 3)} | {fmt(s60['median_wait'])} |",
        "",
        "## 30m detail",
        "",
        f"- triggered/timeouts: {s30['triggered']}/{s30['timeouts']}",
        f"- winners_missed / losers_avoided: {winners_missed_30} / {losers_avoided_30}",
        "",
        "## 60m detail",
        "",
        f"- triggered/timeouts: {s60['triggered']}/{s60['timeouts']}",
        f"- winners_missed / losers_avoided: {winners_missed_60} / {losers_avoided_60}",
        "",
        "## Additional trades (timeout@30 → trigger@60)",
        "",
        f"- count: **{late_summary['count']}**",
        f"- winrate: {fmt(late_summary['winrate'])}",
        f"- gross / net: {fmt(late_summary['gross'])} / {fmt(late_summary['net'])}",
        f"- PF: {fmt(late_summary['profit_factor'])}",
        f"- median/mean entry delay: {fmt(late_summary['median_entry_delay'])} / {fmt(late_summary['mean_entry_delay'])}",
        f"- median MAE / MFE: {fmt(late_summary['median_MAE'], 3)} / {fmt(late_summary['median_MFE'], 3)}",
        f"- baseline winners / losers in this set: {late_base_w} / {late_base_l}",
        f"- timed TP / SL / OPEN: {late_tp} / {late_sl} / {late_open}",
        "",
        "## Edge by signal→entry delay (60m triggers)",
        "",
        "| Bucket | Entries | Winrate | Net/trade | SL rate | Med MAE | Med MFE |",
        "| ------ | ------: | ------: | --------: | ------: | ------: | ------: |",
    ]
    for row in delay_rows:
        lines.append(
            f"| {row['bucket']} | {row['entries']} | {fmt(row['winrate'])} | {fmt(row['net_per_trade'], 3)} | "
            f"{fmt(row['sl_rate'])} | {fmt(row['median_MAE'], 3)} | {fmt(row['median_MFE'], 3)} |"
        )
    lines += [
        "",
        "## Signal age buckets (entry_ts − tier_a_signal_ts)",
        "",
        "| Bucket | Entries | Winrate | Net | SL rate | Med MAE | Med MFE |",
        "| ------ | ------: | ------: | --: | ------: | ------: | ------: |",
    ]
    for row in age_rows:
        lines.append(
            f"| {row['bucket']} | {row['entries']} | {fmt(row['winrate'])} | {fmt(row['net'])} | "
            f"{fmt(row['sl_rate'])} | {fmt(row['median_MAE'], 3)} | {fmt(row['median_MFE'], 3)} |"
        )
    lines += [
        "",
        "## Pairwise (both triggered)",
        "",
        f"- both triggered: **{len(pairwise_rows)}**",
        f"- pairwise_entry_mismatches: **{mismatches}** (expected 0)",
        f"- SL→TP / TP→SL flips: {sl_to_tp} / {tp_to_sl}",
        "",
        "## Strategy Logic Changed",
        "",
        "`NO`",
        "",
        "## DB Changed",
        "",
        "`NO`",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print("PRIMARY", primary, flush=True)
    print("REC", recommendation, flush=True)
    print("late_count", late_summary["count"], "mismatches", mismatches, flush=True)
    print("wrote", OUT, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
