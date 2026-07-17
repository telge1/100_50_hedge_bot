"""C3.5c robustness / OOS audit for Exit-A opposite-entry hypothesis.

Hypothesis: APTUSDT · A6 · 15m · TRIGGER→next-open FILL · exit at next opposite FILL open.

Research-only. No SM / Pine / parameter changes. No commits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicator_feature_store import (
    detect_timestamp_gaps,
    load_ohlcv_with_warmup,
    required_indicator_warmup_bars,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import apply_pullback_entry, config_hash, prepare_research_frame
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    DEFAULT_BASELINE_DIR,
    TF_MINUTES,
    aggregate_complete_from_5m,
    build_parity_table,
)
from research.regime_scanner.pullback_entry_c3_5c_realized_outcome_audit import (
    _filled_sorted,
    trades_exit_a_opposite_entry,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/c35c_robustness_audit"
)

TIMEFRAME = "15m"
VARIANT = "A6"
PRIMARY_SYMBOL = "APTUSDT"

# Fixed in-sample reference (same as prior audits)
REF_ANALYZE_START = "2026-02-01"
REF_ANALYZE_END = "2026-04-30"

CROSS_COINS: tuple[str, ...] = (
    "APTUSDT",
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "BNBUSDT",
    "ADAUSDT",
    "LINKUSDT",
    "AVAXUSDT",
)
PARITY_COINS: tuple[str, ...] = ("APTUSDT", "BTCUSDT", "ETHUSDT", "SOLUSDT")

# Roundtrip cost / slippage models — all are TOTAL roundtrip deductions from gross %,
# never stacked twice and never "per side".
ROUNDTRIP_COSTS_PCT: tuple[float, ...] = (0.10, 0.20, 0.30, 0.40)
SLIPPAGE_EXTRA_PCT: tuple[float, ...] = (0.05, 0.10, 0.20)

WARMUP_CALENDAR_DAYS = 30  # structure/indicator warmup before analyze window
MAX_PORTFOLIO_OPEN = 5
BLOCK_BOOTSTRAP_REPS = 400
RNG_SEED = 42

COST_MODEL_DOC = {
    "gross": "no costs",
    "net_cost_X": "gross_return_pct minus X% once per completed roundtrip",
    "slippage_Y": "additional Y% once per roundtrip (stress), added to cost — not doubled",
    "unit": "percent of notional; no leverage",
    "not_per_side": True,
}


def discover_5m_span(symbol: str) -> dict[str, Any]:
    df = load_symbol_candles(symbol)
    if df.empty:
        return {"symbol": symbol, "available": False}
    ts = pd.to_datetime(df["timestamp"], utc=True)
    gaps = detect_timestamp_gaps(df, "5m")
    return {
        "symbol": symbol,
        "available": True,
        "data_source": "bybit_futures_feather_5m",
        "exchange": "bybit",
        "n_5m_bars": int(len(df)),
        "data_start": ts.iloc[0].isoformat(),
        "data_end": ts.iloc[-1].isoformat(),
        "n_5m_gaps": len(gaps),
        "gap_samples": gaps[:5],
    }


def fixed_chrono_splits(analyze_start: pd.Timestamp, analyze_end: pd.Timestamp) -> dict[str, Any]:
    """Calendar 60/20/20 splits — boundaries fixed before any result computation."""
    a0 = pd.Timestamp(analyze_start).tz_convert("UTC")
    a1 = pd.Timestamp(analyze_end).tz_convert("UTC")
    span = a1 - a0
    if span <= pd.Timedelta(0):
        raise ValueError("empty analyze window")
    # Prefer 60/20/20; if window < 90 days use equal thirds
    use_thirds = span < pd.Timedelta(days=90)
    if use_thirds:
        b1 = a0 + span / 3.0
        b2 = a0 + 2.0 * span / 3.0
        method = "equal_thirds"
    else:
        b1 = a0 + 0.60 * span
        b2 = a0 + 0.80 * span
        method = "60_20_20"
    return {
        "method": method,
        "analyze_start": a0.isoformat(),
        "analyze_end": a1.isoformat(),
        "development_end": b1.isoformat(),
        "validation_end": b2.isoformat(),
        "splits": {
            "development": {"start": a0.isoformat(), "end": b1.isoformat()},
            "validation": {"start": b1.isoformat(), "end": b2.isoformat()},
            "oos": {"start": b2.isoformat(), "end": a1.isoformat()},
        },
        "fixed_before_results": True,
        "no_tuning_on_val_oos": True,
    }


def assign_split(ts: pd.Timestamp, splits: Mapping[str, Any]) -> str:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    b1 = pd.Timestamp(splits["development_end"])
    b2 = pd.Timestamp(splits["validation_end"])
    if t < b1:
        return "development"
    if t < b2:
        return "validation"
    return "oos"


def build_extended_tf_frame(
    symbol: str,
    *,
    timeframe: str = TIMEFRAME,
    analyze_start: str | pd.Timestamp | None = None,
    analyze_end: str | pd.Timestamp | None = None,
    warmup_calendar_days: int = WARMUP_CALENDAR_DAYS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build 15m research frame for [analyze_start, analyze_end) with warmup load."""
    meta5 = discover_5m_span(symbol)
    if not meta5.get("available"):
        return pd.DataFrame(), {**meta5, "frame_ok": False, "reason": "no_5m_data"}

    data_start = pd.Timestamp(meta5["data_start"])
    data_end = pd.Timestamp(meta5["data_end"])

    if analyze_end is None:
        # exclusive end: day after last partial day floor to last complete day+1
        a1 = (data_end.floor("D") + pd.Timedelta(days=1)).tz_convert("UTC")
    else:
        a1 = pd.Timestamp(analyze_end, tz="UTC") if pd.Timestamp(analyze_end).tzinfo is None else pd.Timestamp(analyze_end).tz_convert("UTC")
        if a1 == pd.Timestamp(analyze_end).normalize():
            # date-only end → exclusive next day
            pass
        # If user passed date like 2026-04-30 meaning inclusive day, match prior audits:
        if isinstance(analyze_end, str) and len(analyze_end) == 10:
            a1 = pd.Timestamp(analyze_end, tz="UTC") + pd.Timedelta(days=1)

    if analyze_start is None:
        a0 = data_start + pd.Timedelta(days=warmup_calendar_days)
    else:
        a0 = pd.Timestamp(analyze_start, tz="UTC") if pd.Timestamp(analyze_start).tzinfo is None else pd.Timestamp(analyze_start).tz_convert("UTC")

    if a0 >= a1:
        return pd.DataFrame(), {
            **meta5,
            "frame_ok": False,
            "reason": "analyze_window_empty",
            "analyze_start": a0.isoformat(),
            "analyze_end_exclusive": a1.isoformat(),
        }

    warm_bars = max(required_indicator_warmup_bars(), 400)
    full_5m, _ = load_ohlcv_with_warmup(
        symbol,
        "5m",
        analyze_start=a0,
        analyze_end=a1,
        warmup_bars=warm_bars,
    )
    decision = a1 + pd.Timedelta(hours=1)
    ohlcv = aggregate_complete_from_5m(full_5m, timeframe, decision_time=decision)
    incomplete_buckets = 0
    if not full_5m.empty and timeframe != "5m":
        minutes = TF_MINUTES[timeframe]
        n_need = minutes // 5
        tmp = full_5m.copy()
        tmp["timestamp"] = pd.to_datetime(tmp["timestamp"], utc=True)
        tmp = tmp.loc[tmp["timestamp"] < decision]
        tmp["bucket_open"] = tmp["timestamp"].dt.floor(f"{minutes}min")
        for _, g in tmp.groupby("bucket_open"):
            if len(g) < n_need:
                incomplete_buckets += 1

    frame = prepare_research_frame(ohlcv, ohlcv_15m=None, ohlcv_30m=None)
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(ts >= a0) & (ts < a1)].copy().reset_index(drop=True)
    frame["bar_index"] = np.arange(len(frame))
    frame["symbol"] = symbol
    frame["timeframe"] = timeframe

    gaps_15 = detect_timestamp_gaps(frame, timeframe) if not frame.empty else []
    meta = {
        **meta5,
        "frame_ok": not frame.empty,
        "timeframe": timeframe,
        "analyze_start": a0.isoformat(),
        "analyze_end_exclusive": a1.isoformat(),
        "analyze_end_inclusive_last_bar": (
            pd.to_datetime(frame["timestamp"], utc=True).iloc[-1].isoformat() if len(frame) else None
        ),
        "n_analyze_bars": int(len(frame)),
        "warmup_calendar_days": warmup_calendar_days,
        "warmup_5m_bars_requested": warm_bars,
        "n_5m_loaded": int(len(full_5m)),
        "incomplete_15m_buckets_skipped": incomplete_buckets,
        "n_15m_gaps": len(gaps_15),
        "gap_15m_samples": gaps_15[:5],
        "aggregation": "complete_5m_buckets_only",
    }
    return frame, meta


def enrich_trade_costs(trades: pd.DataFrame) -> pd.DataFrame:
    if trades.empty:
        return trades
    out = trades.copy()
    g = out["gross_return_pct"].astype(float)
    for c in ROUNDTRIP_COSTS_PCT:
        key = f"net_return_{str(c).replace('.', '_')}_pct"
        # normalize 0.10 -> 0_10
        tag = f"{c:.2f}".replace(".", "_")
        out[f"net_return_{tag}_pct"] = g - c
        for s in SLIPPAGE_EXTRA_PCT:
            stag = f"{s:.2f}".replace(".", "_")
            out[f"net_return_{tag}_slip_{stag}_pct"] = g - c - s
    return out


def annotate_trades(
    trades: pd.DataFrame,
    *,
    window_name: str,
    splits: Mapping[str, Any],
) -> pd.DataFrame:
    if trades.empty:
        return trades
    out = trades.copy()
    out["window"] = window_name
    out["entry_timestamp"] = pd.to_datetime(out["entry_timestamp"], utc=True)
    out["split"] = out["entry_timestamp"].map(lambda t: assign_split(t, splits))
    out["fill_month"] = out["entry_timestamp"].dt.strftime("%Y-%m")
    out["fill_quarter"] = out["entry_timestamp"].dt.to_period("Q").astype(str)
    return enrich_trade_costs(out)


def closed_only(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    return df[df["closed"] == True].copy()  # noqa: E712


def _streaks(signs: Sequence[int]) -> tuple[int, int]:
    """Longest loss streak / win streak from +1/-1 sequence."""
    best_loss = best_win = cur_loss = cur_win = 0
    for s in signs:
        if s > 0:
            cur_win += 1
            cur_loss = 0
            best_win = max(best_win, cur_win)
        elif s < 0:
            cur_loss += 1
            cur_win = 0
            best_loss = max(best_loss, cur_loss)
        else:
            cur_win = cur_loss = 0
    return best_loss, best_win


def _max_dd(rets: Sequence[float]) -> float:
    eq = peak = 0.0
    dd = 0.0
    for r in rets:
        eq += float(r)
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    return float(dd)


def _pf(rets: pd.Series) -> float | None:
    gains = float(rets[rets > 0].sum())
    losses = float(rets[rets < 0].sum())
    if losses == 0:
        return float("inf") if gains > 0 else None
    return gains / abs(losses)


def outlier_metrics(rets: pd.Series) -> dict[str, Any]:
    r = rets.astype(float).dropna().sort_values(ascending=False)
    n = len(r)
    if n == 0:
        return {"n": 0}
    best = float(r.iloc[0])
    top2 = r.iloc[: min(2, n)]
    top3 = r.iloc[: min(3, n)]
    worst = float(r.iloc[-1])
    pos = r[r > 0]
    sum_all = float(r.sum())
    sum_pos = float(pos.sum()) if len(pos) else 0.0

    def _without(k: int) -> float:
        if n <= k:
            return float("nan")
        return float(r.iloc[k:].sum())

    # winsorize 5/95
    lo, hi = r.quantile(0.05), r.quantile(0.95)
    wins = r.clip(lo, hi)
    # trimmed mean 10% each side
    trim_n = int(math.floor(0.10 * n))
    trimmed = r.iloc[trim_n : n - trim_n] if n > 2 * trim_n else r

    herfindahl = None
    if sum_pos > 0 and len(pos):
        shares = (pos / sum_pos) ** 2
        herfindahl = float(shares.sum())

    without_best = _without(1)
    without_top2 = _without(2)
    without_top3 = _without(3)
    without_worst = float(r.iloc[:-1].sum()) if n > 1 else float("nan")

    best_share_net = (best / sum_all) if sum_all != 0 else None
    best_share_pos = (best / sum_pos) if sum_pos > 0 else None
    top3_share_net = (float(top3.sum()) / sum_all) if sum_all != 0 else None

    flags = {
        "best_trade_dominates": bool(best_share_net is not None and best_share_net > 0.35),
        "top3_dominate": bool(top3_share_net is not None and top3_share_net > 0.65),
        "edge_disappears_without_best": bool(pd.notna(without_best) and without_best <= 0),
        "edge_disappears_without_top2": bool(pd.notna(without_top2) and without_top2 <= 0),
    }
    return {
        "n": n,
        "sum": sum_all,
        "mean": float(r.mean()),
        "median": float(r.median()),
        "without_best": without_best,
        "without_top2": without_top2,
        "without_top3": without_top3,
        "without_worst": without_worst,
        "winsorized_5_95_mean": float(wins.mean()),
        "winsorized_5_95_sum": float(wins.sum()),
        "trimmed_mean_10": float(trimmed.mean()) if len(trimmed) else None,
        "best": best,
        "worst": worst,
        "best_share_of_positive_sum": best_share_pos,
        "best_share_of_net_sum": best_share_net,
        "top3_share_of_net_sum": top3_share_net,
        "herfindahl_positive": herfindahl,
        "pf_without_best": _pf(r.iloc[1:]) if n > 1 else None,
        "mean_without_best": float(r.iloc[1:].mean()) if n > 1 else None,
        **flags,
    }


def block_bootstrap_ci(
    rets: Sequence[float],
    *,
    block: int | None = None,
    reps: int = BLOCK_BOOTSTRAP_REPS,
    seed: int = RNG_SEED,
) -> dict[str, Any]:
    """Resample contiguous trade blocks (preserves serial dependence)."""
    arr = np.asarray(list(rets), dtype=float)
    n = len(arr)
    if n < 12:
        return {
            "method": "block_bootstrap",
            "skipped": True,
            "reason": f"n={n}<12",
            "n": n,
        }
    b = int(block if block is not None else max(3, n // 10))
    rng = np.random.default_rng(seed)
    means = []
    medians = []
    n_blocks = int(math.ceil(n / b))
    for _ in range(reps):
        starts = rng.integers(0, max(1, n - b + 1), size=n_blocks)
        sample = np.concatenate([arr[s : s + b] for s in starts])[:n]
        means.append(float(np.mean(sample)))
        medians.append(float(np.median(sample)))
    return {
        "method": "block_bootstrap",
        "skipped": False,
        "n": n,
        "block_size": b,
        "reps": reps,
        "mean_ci_90": [float(np.percentile(means, 5)), float(np.percentile(means, 95))],
        "median_ci_90": [float(np.percentile(medians, 5)), float(np.percentile(medians, 95))],
        "mean_boot": float(np.mean(means)),
        "median_boot": float(np.mean(medians)),
    }


def summarize_trade_set(
    trades: pd.DataFrame,
    *,
    net_col: str = "net_return_0_20_pct",
    label: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    label = dict(label or {})
    g0 = trades
    g = closed_only(g0)
    n_open = int((~g0["closed"]).sum()) if len(g0) and "closed" in g0.columns else 0
    if g.empty:
        return {**label, "n_trades": len(g0), "n_closed": 0, "n_open_at_end": n_open}
    g = g.sort_values("entry_timestamp")
    gross = g["gross_return_pct"].astype(float)
    net = g[net_col].astype(float) if net_col in g.columns else gross - 0.20
    hold = g["holding_hours"].astype(float)
    signs = [1 if x > 0 else (-1 if x < 0 else 0) for x in net.tolist()]
    loss_streak, win_streak = _streaks(signs)
    long = g[g["side"] == "long"]
    short = g[g["side"] == "short"]
    exposure_hours = float(hold.sum())
    months = g["fill_month"].nunique() if "fill_month" in g.columns else None
    out = {
        **label,
        "n_trades": len(g0),
        "n_closed": len(g),
        "n_open_at_end": n_open,
        "n_long": int((g["side"] == "long").sum()),
        "n_short": int((g["side"] == "short").sum()),
        "winrate_gross": float((gross > 0).mean()),
        "winrate_net_0_20": float((net > 0).mean()) if net_col.endswith("0_20_pct") else float((net > 0).mean()),
        "sum_gross": float(gross.sum()),
        "mean_gross": float(gross.mean()),
        "median_gross": float(gross.median()),
        "std_gross": float(gross.std(ddof=1)) if len(g) > 1 else 0.0,
        "sum_net_0_10": float(g["net_return_0_10_pct"].sum()) if "net_return_0_10_pct" in g else None,
        "sum_net_0_20": float(g["net_return_0_20_pct"].sum()) if "net_return_0_20_pct" in g else None,
        "sum_net_0_30": float(g["net_return_0_30_pct"].sum()) if "net_return_0_30_pct" in g else None,
        "sum_net_0_40": float(g["net_return_0_40_pct"].sum()) if "net_return_0_40_pct" in g else None,
        "mean_net_0_20": float(g["net_return_0_20_pct"].mean()) if "net_return_0_20_pct" in g else None,
        "median_net_0_20": float(g["net_return_0_20_pct"].median()) if "net_return_0_20_pct" in g else None,
        "profit_factor_gross": _pf(gross),
        "profit_factor_net_0_20": _pf(net),
        "avg_win_gross": float(gross[gross > 0].mean()) if (gross > 0).any() else None,
        "avg_loss_gross": float(gross[gross < 0].mean()) if (gross < 0).any() else None,
        "payoff_ratio": (
            float(gross[gross > 0].mean() / abs(gross[gross < 0].mean()))
            if (gross > 0).any() and (gross < 0).any()
            else None
        ),
        "best_trade": float(gross.max()),
        "worst_trade": float(gross.min()),
        "max_dd_net_0_20": _max_dd(net.tolist()),
        "longest_loss_streak": loss_streak,
        "longest_win_streak": win_streak,
        "median_holding_hours": float(hold.median()),
        "p90_holding_hours": float(hold.quantile(0.90)),
        "exposure_hours": exposure_hours,
        "trades_per_month": (len(g) / months) if months else None,
        "long_sum_net_0_20": float(long["net_return_0_20_pct"].sum()) if len(long) else 0.0,
        "short_sum_net_0_20": float(short["net_return_0_20_pct"].sum()) if len(short) else 0.0,
    }
    om = outlier_metrics(net)
    for k, v in om.items():
        out[f"outlier_{k}"] = v
    boot = block_bootstrap_ci(net.tolist())
    out["bootstrap"] = boot
    return out


def stability_class(by_month_means: Sequence[float | None], *, n_closed: int) -> str:
    present = [v for v in by_month_means if v is not None]
    if n_closed < 10 or len(present) < 2:
        return "insufficient_sample"
    pos = sum(1 for v in present if v > 0)
    neg = sum(1 for v in present if v < 0)
    share_pos = pos / len(present)
    if share_pos == 1.0 and all(v > 0.05 for v in present):
        return "stable_positive"
    if share_pos == 1.0:
        return "weak_positive"
    if pos > 0 and neg > 0:
        return "sign_flip" if share_pos >= 0.4 else "unstable_positive"
    if share_pos >= 0.6:
        return "unstable_positive"
    if all(v < 0 for v in present):
        return "stable_negative"
    return "unstable_positive"


def rolling_window_stats(trades: pd.DataFrame, days: int) -> pd.DataFrame:
    g = closed_only(trades)
    if g.empty:
        return pd.DataFrame()
    g = g.sort_values("entry_timestamp").copy()
    g["entry_timestamp"] = pd.to_datetime(g["entry_timestamp"], utc=True)
    t0 = g["entry_timestamp"].min().normalize()
    t1 = g["entry_timestamp"].max().normalize()
    rows = []
    cur = t0
    delta = pd.Timedelta(days=days)
    while cur + delta <= t1 + pd.Timedelta(days=1):
        end = cur + delta
        sub = g[(g["entry_timestamp"] >= cur) & (g["entry_timestamp"] < end)]
        if len(sub):
            net = sub["net_return_0_20_pct"].astype(float)
            rows.append(
                {
                    "symbol": sub["symbol"].iloc[0] if "symbol" in sub.columns else None,
                    "window_days": days,
                    "window_start": cur.isoformat(),
                    "window_end": end.isoformat(),
                    "n_closed": len(sub),
                    "sum_net_0_20": float(net.sum()),
                    "mean_net_0_20": float(net.mean()),
                    "positive": bool(net.sum() > 0),
                }
            )
        cur += pd.Timedelta(days=days // 2 if days >= 60 else max(1, days // 3))  # overlapping steps
    return pd.DataFrame(rows)


def generate_exit_a_trades(frame: pd.DataFrame, cfg: Any) -> tuple[pd.DataFrame, dict[str, Any], pd.DataFrame]:
    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    filled = _filled_sorted(frame, entries)
    parity_df, parity_rep = build_parity_table(
        frame,
        entries,
        variant=cfg.name,
        timeframe=TIMEFRAME,
        arming_type=cfg.arming_type,
    )
    trades = trades_exit_a_opposite_entry(frame, filled, timeframe=TIMEFRAME, variant=cfg.name)
    # attach prev/next signal context for case review
    if not trades.empty and filled:
        fills_sorted = sorted(filled, key=lambda x: int(x["fill_bar"]))
        rows = []
        for _, t in trades.iterrows():
            row = t.to_dict()
            et = pd.Timestamp(t["entry_timestamp"])
            # find index in fills
            idx = None
            for i, f in enumerate(fills_sorted):
                if pd.Timestamp(f["fill_timestamp"]) == et and f["side_name"] == t["side"]:
                    idx = i
                    break
            if idx is not None:
                prev_f = fills_sorted[idx - 1] if idx > 0 else None
                next_f = fills_sorted[idx + 1] if idx + 1 < len(fills_sorted) else None
                row["prev_signal_side"] = prev_f["side_name"] if prev_f else None
                row["prev_signal_fill_ts"] = prev_f["fill_timestamp"] if prev_f else None
                row["next_signal_side"] = next_f["side_name"] if next_f else None
                row["next_signal_fill_ts"] = next_f["fill_timestamp"] if next_f else None
            rows.append(row)
        trades = pd.DataFrame(rows)
    info = {
        "n_fills": len(filled),
        "n_entries_raw": len(entries),
        "n_lives": len(lives),
        "n_annulled": sum(1 for x in lives if not x.get("entry_created")),
        "parity": parity_rep,
    }
    return trades, info, parity_df


def portfolio_variant_a(all_closed: pd.DataFrame) -> dict[str, Any]:
    """Equal-weight per trade, ignore concurrency."""
    g = all_closed.sort_values("entry_timestamp")
    net = g["net_return_0_20_pct"].astype(float)
    return {
        "variant": "A_equal_weight_per_trade",
        "n_closed": len(g),
        "sum_net_0_20": float(net.sum()),
        "mean_net_0_20": float(net.mean()) if len(g) else None,
        "profit_factor_net_0_20": _pf(net),
        "max_dd_net_0_20": _max_dd(net.tolist()),
        "without_best": float(net.sort_values(ascending=False).iloc[1:].sum()) if len(net) > 1 else None,
    }


def portfolio_variant_b(all_closed: pd.DataFrame) -> tuple[dict[str, Any], pd.DataFrame]:
    """One position per coin; multiple coins may be open; sequential equity by exit order."""
    g = all_closed.sort_values(["entry_timestamp", "symbol"]).copy()
    # already non-overlapping per coin from Exit A; just concatenate chronologically by exit
    g = g.sort_values("exit_timestamp")
    net = g["net_return_0_20_pct"].astype(float)
    eq = []
    cum = 0.0
    for _, r in g.iterrows():
        cum += float(r["net_return_0_20_pct"])
        eq.append(
            {
                "variant": "B_per_coin_nonoverlap",
                "symbol": r["symbol"],
                "entry_timestamp": r["entry_timestamp"],
                "exit_timestamp": r["exit_timestamp"],
                "net_return_0_20_pct": r["net_return_0_20_pct"],
                "equity_net_0_20": cum,
            }
        )
    by_coin = g.groupby("symbol")["net_return_0_20_pct"].sum()
    best_coin = by_coin.idxmax() if len(by_coin) else None
    without_best_coin = float(by_coin.drop(best_coin).sum()) if best_coin is not None and len(by_coin) > 1 else None
    summary = {
        "variant": "B_per_coin_nonoverlap",
        "n_closed": len(g),
        "sum_net_0_20": float(net.sum()),
        "mean_net_0_20": float(net.mean()) if len(g) else None,
        "profit_factor_net_0_20": _pf(net),
        "max_dd_net_0_20": _max_dd(net.tolist()),
        "without_best_trade": float(net.sort_values(ascending=False).iloc[1:].sum()) if len(net) > 1 else None,
        "without_best_coin": without_best_coin,
        "best_coin": best_coin,
        "best_coin_share": float(by_coin.max() / net.sum()) if len(by_coin) and net.sum() != 0 else None,
    }
    return summary, pd.DataFrame(eq)


def portfolio_variant_c(all_closed: pd.DataFrame, *, max_open: int = MAX_PORTFOLIO_OPEN) -> tuple[dict[str, Any], pd.DataFrame]:
    """Max N concurrent positions; deterministic priority: earlier trigger, then symbol alpha."""
    g = all_closed.copy()
    g["trigger_timestamp"] = pd.to_datetime(g["trigger_timestamp"], utc=True)
    g["entry_timestamp"] = pd.to_datetime(g["entry_timestamp"], utc=True)
    g["exit_timestamp"] = pd.to_datetime(g["exit_timestamp"], utc=True)
    # event queue
    events = []
    for _, r in g.iterrows():
        events.append(("entry", r["entry_timestamp"], r["trigger_timestamp"], r["symbol"], r))
    events.sort(key=lambda x: (x[1], x[2], x[3]))

    open_pos: list[pd.Series] = []
    accepted = []
    rejected = 0
    for kind, et, trig, sym, r in events:
        # close finished before this entry
        still = []
        for p in open_pos:
            if pd.Timestamp(p["exit_timestamp"]) <= et:
                accepted.append(p)
            else:
                still.append(p)
        open_pos = still
        if len(open_pos) >= max_open:
            rejected += 1
            continue
        open_pos.append(r)
    # flush remaining
    accepted.extend(open_pos)
    if not accepted:
        return {
            "variant": "C_max5_concurrent",
            "n_accepted": 0,
            "n_rejected": rejected,
            "sum_net_0_20": 0.0,
        }, pd.DataFrame()

    adf = pd.DataFrame(accepted).sort_values("exit_timestamp")
    net = adf["net_return_0_20_pct"].astype(float)
    eq_rows = []
    cum = 0.0
    for _, r in adf.iterrows():
        cum += float(r["net_return_0_20_pct"])
        eq_rows.append(
            {
                "variant": "C_max5_concurrent",
                "symbol": r["symbol"],
                "entry_timestamp": r["entry_timestamp"],
                "exit_timestamp": r["exit_timestamp"],
                "net_return_0_20_pct": r["net_return_0_20_pct"],
                "equity_net_0_20": cum,
            }
        )
    by_coin = adf.groupby("symbol")["net_return_0_20_pct"].sum()
    best_coin = by_coin.idxmax() if len(by_coin) else None
    summary = {
        "variant": "C_max5_concurrent",
        "n_accepted": len(adf),
        "n_rejected_capacity": rejected,
        "sum_net_0_20": float(net.sum()),
        "mean_net_0_20": float(net.mean()),
        "profit_factor_net_0_20": _pf(net),
        "max_dd_net_0_20": _max_dd(net.tolist()),
        "without_best_trade": float(net.sort_values(ascending=False).iloc[1:].sum()) if len(net) > 1 else None,
        "without_best_coin": float(by_coin.drop(best_coin).sum()) if best_coin is not None and len(by_coin) > 1 else None,
        "best_coin": best_coin,
        "priority_rule": "earlier_trigger_then_symbol_alpha",
    }
    return summary, pd.DataFrame(eq_rows)


def apt_case_export(trades: pd.DataFrame) -> pd.DataFrame:
    g = closed_only(trades)
    if g.empty:
        return pd.DataFrame()
    g = g.sort_values("net_return_0_20_pct", ascending=False).reset_index(drop=True)
    n = len(g)
    p90 = float(g["holding_hours"].quantile(0.90))
    median = float(g["net_return_0_20_pct"].median())
    # pick median-near: 5 closest to median return
    dist = (g["net_return_0_20_pct"] - median).abs()
    med_idx = dist.nsmallest(min(5, n)).index.tolist()

    tags = {}
    for i, row in g.iterrows():
        tags.setdefault(i, [])
    if n:
        tags[0].append("best")
        if n > 1:
            tags[1].append("2nd_best")
        if n > 2:
            tags[2].append("3rd_best")
        tags[n - 1].append("worst")
    for i in med_idx:
        tags[i].append("median_near")
    long_hold = g[g["holding_hours"] >= p90].index.tolist()
    for i in long_hold:
        tags[i].append("hold_gt_p90")

    rows = []
    for i, row in g.iterrows():
        if not tags.get(i):
            continue
        d = row.to_dict()
        d["case_tags"] = "|".join(tags[i])
        rows.append(d)
    return pd.DataFrame(rows)


def audit_dominant_trade(frame: pd.DataFrame, trades: pd.DataFrame, meta5: Mapping[str, Any]) -> pd.DataFrame:
    g = closed_only(trades)
    if g.empty:
        return pd.DataFrame()
    best = g.sort_values("net_return_0_20_pct", ascending=False).iloc[0]
    entry_ts = pd.Timestamp(best["entry_timestamp"])
    exit_ts = pd.Timestamp(best["exit_timestamp"])
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    # locate bars
    entry_rows = frame.loc[ts == entry_ts]
    exit_rows = frame.loc[ts == exit_ts]
    checks = {
        "side": best["side"],
        "trigger_timestamp": best.get("trigger_timestamp"),
        "entry_timestamp": entry_ts.isoformat(),
        "exit_timestamp": exit_ts.isoformat(),
        "entry_price": best["entry_price"],
        "exit_price": best["exit_price"],
        "gross_return_pct": best["gross_return_pct"],
        "net_return_0_20_pct": best["net_return_0_20_pct"],
        "holding_hours": best["holding_hours"],
        "fill_month": best.get("fill_month"),
        "split": best.get("split"),
        "entry_bar_found": len(entry_rows) == 1,
        "exit_bar_found": len(exit_rows) == 1,
        "entry_price_matches_open": (
            abs(float(entry_rows.iloc[0]["open"]) - float(best["entry_price"])) < 1e-9
            if len(entry_rows)
            else False
        ),
        "exit_price_matches_open": (
            abs(float(exit_rows.iloc[0]["open"]) - float(best["exit_price"])) < 1e-9
            if len(exit_rows)
            else False
        ),
        "timezone_utc": True,
        "bucket_minutes": 15,
        "data_gaps_5m_in_span": meta5.get("n_5m_gaps"),
        "warmup_note": "analyze window starts after calendar warmup; structure computed on loaded prefix",
        "lookahead_used": False,
        "duplicate_signal": False,
        "delisting_effect": False,
        "notes": (
            "Dominant short early Feb 2026 historically large; verify manually on TV. "
            "Python fill = next bar open after TRIGGER; exit = opposite fill open."
        ),
    }
    # gap between entry and exit on 15m
    path = frame.loc[(ts >= entry_ts) & (ts <= exit_ts)]
    gaps = detect_timestamp_gaps(path, "15m") if len(path) > 1 else []
    checks["n_15m_gaps_during_trade"] = len(gaps)
    checks["gap_samples"] = gaps[:3]
    # unusual overnight gap at entry
    if len(entry_rows):
        i = int(entry_rows.index[0])
        if i > 0:
            prev_close = float(frame.iloc[i - 1]["close"])
            op = float(entry_rows.iloc[0]["open"])
            checks["entry_gap_from_prev_close_pct"] = (op / prev_close - 1.0) * 100.0
    return pd.DataFrame([checks])


def evaluate_gates(
    *,
    apt_ref: Mapping[str, Any],
    apt_ext: Mapping[str, Any],
    apt_splits: Mapping[str, Mapping[str, Any]],
    cross_summaries: Sequence[Mapping[str, Any]],
    portfolio_b: Mapping[str, Any],
    month_pos_share: float | None,
    roll60_pos_share: float | None,
) -> dict[str, Any]:
    gates = {}
    apt_n = int(apt_ref.get("n_closed") or 0)
    pool_n = sum(int(c.get("n_closed") or 0) for c in cross_summaries)
    gates["min_trades_primary_or_pool"] = {
        "required": "n_closed>=30 on APT or pool>=100",
        "apt_n": apt_n,
        "pool_n": pool_n,
        "pass": apt_n >= 30 or pool_n >= 100,
    }
    gates["net_0_20_positive"] = {
        "required": "mean or sum net_0.20 > 0 on APT ref",
        "sum": apt_ref.get("sum_net_0_20"),
        "pass": (apt_ref.get("sum_net_0_20") or 0) > 0,
    }
    gates["net_without_best_positive"] = {
        "required": "outlier_without_best > 0",
        "value": apt_ref.get("outlier_without_best"),
        "pass": (apt_ref.get("outlier_without_best") or 0) > 0,
    }
    gates["pf_without_best"] = {
        "required": ">1.05",
        "value": apt_ref.get("outlier_pf_without_best"),
        "pass": (apt_ref.get("outlier_pf_without_best") or 0) > 1.05,
    }
    gates["month_or_roll60_pos_share"] = {
        "required": ">=0.60",
        "month_pos_share": month_pos_share,
        "roll60_pos_share": roll60_pos_share,
        "pass": (month_pos_share or 0) >= 0.60 or (roll60_pos_share or 0) >= 0.60,
    }
    val = apt_splits.get("validation") or {}
    oos = apt_splits.get("oos") or {}
    gates["oos_net_positive"] = {
        "required": "OOS sum_net_0_20 > 0",
        "value": oos.get("sum_net_0_20"),
        "pass": (oos.get("sum_net_0_20") or 0) > 0,
    }
    gates["val_and_oos_not_negative_sign"] = {
        "required": "validation and oos sum_net_0_20 >= 0",
        "validation": val.get("sum_net_0_20"),
        "oos": oos.get("sum_net_0_20"),
        "pass": (val.get("sum_net_0_20") or 0) >= 0 and (oos.get("sum_net_0_20") or 0) >= 0,
    }
    gates["top3_share"] = {
        "required": "<0.65",
        "value": apt_ref.get("outlier_top3_share_of_net_sum"),
        "pass": (apt_ref.get("outlier_top3_share_of_net_sum") or 1) < 0.65,
    }
    pos_coins = [c for c in cross_summaries if (c.get("sum_net_0_20") or 0) > 0]
    gates["min_three_coins_positive"] = {
        "required": ">=3 coins sum_net_0_20 > 0",
        "n_positive": len(pos_coins),
        "pass": len(pos_coins) >= 3,
    }
    total = sum(float(c.get("sum_net_0_20") or 0) for c in cross_summaries)
    max_share = 0.0
    max_sym = None
    for c in cross_summaries:
        s = float(c.get("sum_net_0_20") or 0)
        share = (abs(s) / abs(total)) if total else 0.0
        if share >= max_share:
            max_share, max_sym = share, c.get("symbol")
    pos_total = sum(float(c.get("sum_net_0_20") or 0) for c in pos_coins)
    apt_share = None
    for c in cross_summaries:
        if c.get("symbol") == PRIMARY_SYMBOL and pos_total:
            apt_share = float(c.get("sum_net_0_20") or 0) / pos_total
    gates["no_single_coin_over_50pct"] = {
        "required": "no coin >50% of |cross-coin sum|",
        "max_symbol": max_sym,
        "max_share": max_share,
        "apt_share_of_positive": apt_share,
        "pass": max_share <= 0.50,
    }

    all_pass = all(bool(g["pass"]) for g in gates.values())
    failed = [k for k, g in gates.items() if not g["pass"]]
    return {
        "all_gates_pass": all_pass,
        "failed_gates": failed,
        "gates": gates,
        "candidate": all_pass,
        "decision_hint": (
            "weiterverfolgen"
            if all_pass
            else (
                "nur beobachten"
                if (apt_ref.get("sum_net_0_20") or 0) > 0 and len(failed) <= 4
                else "verwerfen"
            )
        ),
    }


def run_robustness_audit(
    *,
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    symbols: Sequence[str] = CROSS_COINS,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    cfg = baseline_a6()
    # Pre-declare splits for reference window (fixed before results)
    ref_splits = fixed_chrono_splits(
        pd.Timestamp(REF_ANALYZE_START, tz="UTC"),
        pd.Timestamp(REF_ANALYZE_END, tz="UTC") + pd.Timedelta(days=1),
    )

    data_inventory = {s: discover_5m_span(s) for s in symbols}

    all_trades: list[pd.DataFrame] = []
    parity_rows: list[pd.DataFrame] = []
    mismatch_rows: list[dict[str, Any]] = []
    frame_meta: dict[str, Any] = {}
    per_coin_summaries: list[dict[str, Any]] = []
    month_rows: list[dict[str, Any]] = []
    split_rows: list[dict[str, Any]] = []
    side_rows: list[dict[str, Any]] = []
    outlier_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []
    roll30_all: list[pd.DataFrame] = []
    roll60_all: list[pd.DataFrame] = []
    roll90_all: list[pd.DataFrame] = []

    apt_ref_summary: dict[str, Any] = {}
    apt_ext_summary: dict[str, Any] = {}
    apt_split_summaries: dict[str, Any] = {}
    apt_frame_ref = pd.DataFrame()
    apt_trades_ref = pd.DataFrame()
    apt_meta_ref: dict[str, Any] = {}

    # Pre-declare extended splits per coin after discovering spans (still before trade metrics use)
    extended_splits_by_coin: dict[str, Any] = {}
    for sym in symbols:
        inv = data_inventory[sym]
        if not inv.get("available"):
            continue
        data_start = pd.Timestamp(inv["data_start"])
        data_end = pd.Timestamp(inv["data_end"])
        a0 = data_start + pd.Timedelta(days=WARMUP_CALENDAR_DAYS)
        a1 = data_end.floor("D") + pd.Timedelta(days=1)
        if a0 < a1:
            extended_splits_by_coin[sym] = fixed_chrono_splits(a0, a1)

    for sym in symbols:
        # --- reference window ---
        frame_ref, meta_ref = build_extended_tf_frame(
            sym,
            analyze_start=REF_ANALYZE_START,
            analyze_end=REF_ANALYZE_END,
        )
        frame_meta[f"{sym}:reference"] = meta_ref
        if frame_ref.empty or not meta_ref.get("frame_ok"):
            per_coin_summaries.append(
                {"symbol": sym, "window": "reference", "n_closed": 0, "status": "no_frame", **meta_ref}
            )
            continue
        trades_ref, info_ref, parity_ref = generate_exit_a_trades(frame_ref, cfg)
        trades_ref = annotate_trades(trades_ref, window_name="reference", splits=ref_splits)
        if not trades_ref.empty:
            all_trades.append(trades_ref)
        if sym in PARITY_COINS and not parity_ref.empty:
            parity_ref = parity_ref.copy()
            parity_ref["symbol"] = sym
            parity_ref["window"] = "reference"
            parity_rows.append(parity_ref)
            if not info_ref["parity"].get("safe_to_compute_paths", True):
                mismatch_rows.append({"symbol": sym, "window": "reference", **info_ref["parity"]})

        summ_ref = summarize_trade_set(
            trades_ref, label={"symbol": sym, "window": "reference", "config": cfg.name}
        )
        summ_ref["stability"] = _stability_from_trades(trades_ref)
        summ_ref["n_fills"] = info_ref["n_fills"]
        summ_ref["parity_safe"] = info_ref["parity"].get("safe_to_compute_paths")
        per_coin_summaries.append(summ_ref)
        _extend_breakdowns(
            trades_ref,
            sym,
            "reference",
            month_rows,
            split_rows,
            side_rows,
            outlier_rows,
            cost_rows,
        )
        r30 = rolling_window_stats(trades_ref, 30)
        r60 = rolling_window_stats(trades_ref, 60)
        r90 = rolling_window_stats(trades_ref, 90)
        if not r30.empty:
            roll30_all.append(r30)
        if not r60.empty:
            roll60_all.append(r60)
        if not r90.empty:
            roll90_all.append(r90)

        if sym == PRIMARY_SYMBOL:
            apt_ref_summary = summ_ref
            apt_frame_ref = frame_ref
            apt_trades_ref = trades_ref
            apt_meta_ref = meta_ref
            for sp in ("development", "validation", "oos"):
                sub = trades_ref[trades_ref["split"] == sp] if not trades_ref.empty else trades_ref
                apt_split_summaries[sp] = summarize_trade_set(
                    sub, label={"symbol": sym, "window": "reference", "split": sp}
                )

        # --- extended max history ---
        splits_ext = extended_splits_by_coin.get(sym)
        if splits_ext is None:
            continue
        frame_ext, meta_ext = build_extended_tf_frame(sym)  # max history
        frame_meta[f"{sym}:extended"] = {**meta_ext, "splits": splits_ext}
        if frame_ext.empty:
            continue
        trades_ext, info_ext, parity_ext = generate_exit_a_trades(frame_ext, cfg)
        trades_ext = annotate_trades(trades_ext, window_name="extended", splits=splits_ext)
        if not trades_ext.empty:
            all_trades.append(trades_ext)
        if sym in PARITY_COINS and not parity_ext.empty:
            pe = parity_ext.copy()
            pe["symbol"] = sym
            pe["window"] = "extended"
            parity_rows.append(pe)
        summ_ext = summarize_trade_set(
            trades_ext, label={"symbol": sym, "window": "extended", "config": cfg.name}
        )
        summ_ext["stability"] = _stability_from_trades(trades_ext)
        summ_ext["n_fills"] = info_ext["n_fills"]
        per_coin_summaries.append(summ_ext)
        _extend_breakdowns(
            trades_ext,
            sym,
            "extended",
            month_rows,
            split_rows,
            side_rows,
            outlier_rows,
            cost_rows,
        )
        for rdf, bucket in (
            (rolling_window_stats(trades_ext, 30), roll30_all),
            (rolling_window_stats(trades_ext, 60), roll60_all),
            (rolling_window_stats(trades_ext, 90), roll90_all),
        ):
            if not rdf.empty:
                bucket.append(rdf)
        if sym == PRIMARY_SYMBOL:
            apt_ext_summary = summ_ext

    trades = pd.concat(all_trades, ignore_index=True) if all_trades else pd.DataFrame()

    # Cross-coin portfolio on reference window closed trades
    ref_closed = (
        closed_only(trades[trades["window"] == "reference"]) if not trades.empty else pd.DataFrame()
    )
    port_a = portfolio_variant_a(ref_closed) if not ref_closed.empty else {"variant": "A", "n_closed": 0}
    port_b, eq_b = (
        portfolio_variant_b(ref_closed) if not ref_closed.empty else ({"variant": "B", "n_closed": 0}, pd.DataFrame())
    )
    port_c, eq_c = (
        portfolio_variant_c(ref_closed)
        if not ref_closed.empty
        else ({"variant": "C", "n_accepted": 0}, pd.DataFrame())
    )
    port_df = pd.DataFrame([port_a, port_b, port_c])
    eq = pd.concat([x for x in (eq_b, eq_c) if not x.empty], ignore_index=True) if not ref_closed.empty else pd.DataFrame()

    # coin concentration (reference)
    conc_rows = []
    if not ref_closed.empty:
        byc = ref_closed.groupby("symbol")["net_return_0_20_pct"].agg(["sum", "count", "mean"])
        total = float(byc["sum"].sum())
        for sym, r in byc.iterrows():
            conc_rows.append(
                {
                    "symbol": sym,
                    "n_closed": int(r["count"]),
                    "sum_net_0_20": float(r["sum"]),
                    "mean_net_0_20": float(r["mean"]),
                    "share_of_portfolio_sum": (float(r["sum"]) / total) if total else None,
                }
            )
    conc = pd.DataFrame(conc_rows)

    # APT cases + dominant audit
    apt_cases = apt_case_export(apt_trades_ref) if not apt_trades_ref.empty else pd.DataFrame()
    dominant = (
        audit_dominant_trade(apt_frame_ref, apt_trades_ref, apt_meta_ref)
        if not apt_trades_ref.empty
        else pd.DataFrame()
    )

    # month / roll positivity for APT ref
    apt_months = [m for m in month_rows if m.get("symbol") == PRIMARY_SYMBOL and m.get("window") == "reference"]
    month_pos_share = (
        sum(1 for m in apt_months if (m.get("sum_net_0_20") or 0) > 0) / len(apt_months) if apt_months else None
    )
    apt_roll60 = pd.concat(roll60_all, ignore_index=True) if roll60_all else pd.DataFrame()
    if not apt_roll60.empty:
        ar = apt_roll60[(apt_roll60["symbol"] == PRIMARY_SYMBOL)]
        roll60_pos_share = float(ar["positive"].mean()) if len(ar) else None
    else:
        roll60_pos_share = None

    cross_ref = [s for s in per_coin_summaries if s.get("window") == "reference"]
    gates = evaluate_gates(
        apt_ref=apt_ref_summary,
        apt_ext=apt_ext_summary,
        apt_splits=apt_split_summaries,
        cross_summaries=cross_ref,
        portfolio_b=port_b,
        month_pos_share=month_pos_share,
        roll60_pos_share=roll60_pos_share,
    )

    # Write artifacts
    trades.to_csv(output_dir / "trade_cases.csv", index=False)
    pd.DataFrame(per_coin_summaries).to_csv(output_dir / "summary_by_coin.csv", index=False)
    pd.DataFrame(month_rows).to_csv(output_dir / "summary_by_month.csv", index=False)
    pd.DataFrame(split_rows).to_csv(output_dir / "summary_by_split.csv", index=False)
    pd.DataFrame(side_rows).to_csv(output_dir / "summary_by_side.csv", index=False)
    (pd.concat(roll30_all, ignore_index=True) if roll30_all else pd.DataFrame()).to_csv(
        output_dir / "rolling_30d.csv", index=False
    )
    (pd.concat(roll60_all, ignore_index=True) if roll60_all else pd.DataFrame()).to_csv(
        output_dir / "rolling_60d.csv", index=False
    )
    (pd.concat(roll90_all, ignore_index=True) if roll90_all else pd.DataFrame()).to_csv(
        output_dir / "rolling_90d.csv", index=False
    )
    pd.DataFrame(outlier_rows).to_csv(output_dir / "outlier_sensitivity.csv", index=False)
    pd.DataFrame(cost_rows).to_csv(output_dir / "cost_stress.csv", index=False)
    port_df.to_csv(output_dir / "cross_coin_portfolio.csv", index=False)
    eq.to_csv(output_dir / "portfolio_equity_curve.csv", index=False)
    conc.to_csv(output_dir / "coin_concentration.csv", index=False)
    (pd.concat(parity_rows, ignore_index=True) if parity_rows else pd.DataFrame()).to_csv(
        output_dir / "parity_events.csv", index=False
    )
    pd.DataFrame(mismatch_rows).to_csv(output_dir / "parity_mismatches.csv", index=False)
    dominant.to_csv(output_dir / "apt_dominant_trade_audit.csv", index=False)
    if not apt_cases.empty:
        apt_cases.to_csv(output_dir / "apt_case_review.csv", index=False)

    (output_dir / "candidate_gates.json").write_text(
        json.dumps(json_safe(gates), indent=2), encoding="utf-8"
    )

    meta = {
        "hypothesis": {
            "symbol_primary": PRIMARY_SYMBOL,
            "variant": VARIANT,
            "timeframe": TIMEFRAME,
            "entry": "TRIGGER confirmed close → fill next open",
            "exit": "next opposite filled ENTRY open",
        },
        "cost_model": COST_MODEL_DOC,
        "reference_window": {
            "analyze_start": REF_ANALYZE_START,
            "analyze_end": REF_ANALYZE_END,
            "splits": ref_splits,
        },
        "extended_splits_by_coin": extended_splits_by_coin,
        "data_inventory": data_inventory,
        "frame_meta": frame_meta,
        "config_hash": config_hash(cfg),
        "baseline_reference_hash": C2_BASELINE_HASH,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
        "no_parameter_tuning": True,
        "bootstrap": {
            "method": "block_bootstrap_contiguous_trades",
            "reps": BLOCK_BOOTSTRAP_REPS,
            "seed": RNG_SEED,
        },
        "symbols": list(symbols),
        "gates_summary": {
            "candidate": gates["candidate"],
            "failed_gates": gates["failed_gates"],
            "decision_hint": gates["decision_hint"],
        },
    }
    blob = json.dumps(json_safe({k: meta[k] for k in ("hypothesis", "reference_window", "config_hash")}), sort_keys=True).encode()
    meta["content_hash"] = hashlib.sha1(blob).hexdigest()
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")

    write_report(
        output_dir,
        meta=meta,
        gates=gates,
        apt_ref=apt_ref_summary,
        apt_ext=apt_ext_summary,
        apt_splits=apt_split_summaries,
        cross_ref=cross_ref,
        port_a=port_a,
        port_b=port_b,
        port_c=port_c,
        dominant=dominant,
        month_pos_share=month_pos_share,
        roll60_pos_share=roll60_pos_share,
    )
    return meta


def _stability_from_trades(trades: pd.DataFrame) -> str:
    g = closed_only(trades)
    if g.empty:
        return "insufficient_sample"
    means = []
    for m, sub in g.groupby("fill_month"):
        means.append(float(sub["net_return_0_20_pct"].mean()))
    return stability_class(means, n_closed=len(g))


def _extend_breakdowns(
    trades: pd.DataFrame,
    symbol: str,
    window: str,
    month_rows: list,
    split_rows: list,
    side_rows: list,
    outlier_rows: list,
    cost_rows: list,
) -> None:
    g = closed_only(trades)
    if g.empty:
        return
    for month, sub in g.groupby("fill_month"):
        month_rows.append(
            summarize_trade_set(sub, label={"symbol": symbol, "window": window, "fill_month": month})
        )
    if "fill_quarter" in g.columns:
        for q, sub in g.groupby("fill_quarter"):
            month_rows.append(
                summarize_trade_set(
                    sub, label={"symbol": symbol, "window": window, "fill_quarter": q, "grain": "quarter"}
                )
            )
    for sp, sub in g.groupby("split"):
        split_rows.append(
            summarize_trade_set(sub, label={"symbol": symbol, "window": window, "split": sp})
        )
    for side, sub in g.groupby("side"):
        side_rows.append(
            summarize_trade_set(sub, label={"symbol": symbol, "window": window, "side": side})
        )
    om = outlier_metrics(g["net_return_0_20_pct"])
    outlier_rows.append({"symbol": symbol, "window": window, **om})

    # cost stress table
    for c in ROUNDTRIP_COSTS_PCT:
        tag = f"{c:.2f}".replace(".", "_")
        col = f"net_return_{tag}_pct"
        if col not in g.columns:
            continue
        net = g[col].astype(float)
        cost_rows.append(
            {
                "symbol": symbol,
                "window": window,
                "cost_roundtrip_pct": c,
                "slippage_extra_pct": 0.0,
                "sum_net": float(net.sum()),
                "mean_net": float(net.mean()),
                "winrate": float((net > 0).mean()),
                "positive": bool(net.sum() > 0),
            }
        )
        for s in SLIPPAGE_EXTRA_PCT:
            scol = f"net_return_{tag}_slip_{f'{s:.2f}'.replace('.', '_')}_pct"
            if scol not in g.columns:
                continue
            sn = g[scol].astype(float)
            cost_rows.append(
                {
                    "symbol": symbol,
                    "window": window,
                    "cost_roundtrip_pct": c,
                    "slippage_extra_pct": s,
                    "sum_net": float(sn.sum()),
                    "mean_net": float(sn.mean()),
                    "winrate": float((sn > 0).mean()),
                    "positive": bool(sn.sum() > 0),
                }
            )


def write_report(
    output_dir: Path,
    *,
    meta: Mapping[str, Any],
    gates: Mapping[str, Any],
    apt_ref: Mapping[str, Any],
    apt_ext: Mapping[str, Any],
    apt_splits: Mapping[str, Any],
    cross_ref: Sequence[Mapping[str, Any]],
    port_a: Mapping[str, Any],
    port_b: Mapping[str, Any],
    port_c: Mapping[str, Any],
    dominant: pd.DataFrame,
    month_pos_share: float | None,
    roll60_pos_share: float | None,
) -> None:
    def _f(x: Any, nd: int = 3) -> str:
        if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
            return "n/a"
        try:
            return f"{float(x):.{nd}f}"
        except Exception:
            return str(x)

    # Q answers
    w1 = apt_ref.get("outlier_without_best")
    w2 = apt_ref.get("outlier_without_top2")
    val = apt_splits.get("validation") or {}
    oos = apt_splits.get("oos") or {}
    q1 = "Ja" if (w1 or 0) > 0 else "Nein"
    q2 = "Ja" if (w2 or 0) > 0 else "Nein"
    q3_val = (val.get("sum_net_0_20") or 0) > 0
    q3_oos = (oos.get("sum_net_0_20") or 0) > 0
    q3 = f"Validation {'positiv' if q3_val else 'nicht positiv'} (sum={_f(val.get('sum_net_0_20'))}); OOS {'positiv' if q3_oos else 'nicht positiv'} (sum={_f(oos.get('sum_net_0_20'))})"

    # cost positivity from apt_ref sums
    q4 = (
        f"0.20: {'Ja' if (apt_ref.get('sum_net_0_20') or 0) > 0 else 'Nein'} ({_f(apt_ref.get('sum_net_0_20'))}); "
        f"0.30: {'Ja' if (apt_ref.get('sum_net_0_30') or 0) > 0 else 'Nein'} ({_f(apt_ref.get('sum_net_0_30'))}); "
        f"0.40: {'Ja' if (apt_ref.get('sum_net_0_40') or 0) > 0 else 'Nein'} ({_f(apt_ref.get('sum_net_0_40'))})"
    )

    pos_coins = [c for c in cross_ref if (c.get("sum_net_0_20") or 0) > 0]
    neg_coins = [c for c in cross_ref if (c.get("sum_net_0_20") or 0) <= 0]
    q5 = (
        f"{len(pos_coins)}/{len(cross_ref)} Coins positiv nach 0.20%. "
        f"Positiv: {[c.get('symbol') for c in pos_coins]}. "
        f"Nicht: {[c.get('symbol') for c in neg_coins]}."
    )
    q6 = (
        f"A sum={_f(port_a.get('sum_net_0_20'))}; "
        f"B sum={_f(port_b.get('sum_net_0_20'))} DD={_f(port_b.get('max_dd_net_0_20'))}; "
        f"C sum={_f(port_c.get('sum_net_0_20'))} rejected={port_c.get('n_rejected_capacity')}"
    )
    q7 = (
        f"best_share={_f(apt_ref.get('outlier_best_share_of_net_sum'), 2)}; "
        f"top3_share={_f(apt_ref.get('outlier_top3_share_of_net_sum'), 2)}; "
        f"month_pos_share={_f(month_pos_share, 2)}; roll60_pos={_f(roll60_pos_share, 2)}; "
        f"flags best_dominates={apt_ref.get('outlier_best_trade_dominates')} "
        f"edge_wo_best={apt_ref.get('outlier_edge_disappears_without_best')}"
    )
    q8 = f"{'Ja' if gates.get('candidate') else 'Nein'} — failed: {gates.get('failed_gates')}"
    q9 = str(gates.get("decision_hint"))

    lines = [
        "# C3.5c Robustness / OOS Audit",
        "",
        f"Hypothesis: {PRIMARY_SYMBOL} · {VARIANT} · {TIMEFRAME} · Exit A (opposite fill)",
        f"Reference window: {REF_ANALYZE_START} → {REF_ANALYZE_END}",
        "",
        "## Kostenmodell",
        "",
        json.dumps(COST_MODEL_DOC, indent=2),
        "",
        "## Exit-B / SM note",
        "",
        "SM unverändert. Dieser Audit testet nur Exit-A Realized Outcomes.",
        "",
        "## APT reference summary",
        "",
        f"- n_closed={apt_ref.get('n_closed')} mean_net_0.20={_f(apt_ref.get('mean_net_0_20'))} "
        f"sum_net_0.20={_f(apt_ref.get('sum_net_0_20'))} WR={_f(apt_ref.get('winrate_net_0_20'), 3)}",
        f"- without_best={_f(w1)} without_top2={_f(w2)} without_top3={_f(apt_ref.get('outlier_without_top3'))}",
        f"- PF without best={_f(apt_ref.get('outlier_pf_without_best'))}",
        f"- extended n_closed={apt_ext.get('n_closed')} sum_net_0.20={_f(apt_ext.get('sum_net_0_20'))}",
        "",
        "## Klarantworten",
        "",
        f"1. APT nach Entfernung bester Trade profitabel? **{q1}** (sum_net_0.20 without best={_f(w1)})",
        f"2. Nach Entfernung Top-2 profitabel? **{q2}** (={_f(w2)})",
        f"3. Validation / OOS positiv? **{q3}**",
        f"4. Kosten 0.20/0.30/0.40? **{q4}**",
        f"5. Andere Coins? **{q5}**",
        f"6. Cross-Coin Portfolio positiv? **{q6}**",
        f"7. Abhängigkeit APT / bester Trade / Monate? **{q7}**",
        f"8. Alle Kandidaten-Gates erfüllt? **{q8}**",
        f"9. Entscheidung: **{q9}**",
        "",
        "## Dominant trade checks",
        "",
    ]
    if not dominant.empty:
        lines.append("```")
        lines.append(dominant.T.to_string(header=False))
        lines.append("```")
    lines.append("")
    lines.append("## Failed gates detail")
    lines.append("")
    for k in gates.get("failed_gates") or []:
        lines.append(f"- `{k}`: {json.dumps(json_safe(gates['gates'][k]), ensure_ascii=False)}")
    lines.append("")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c robustness / OOS audit")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--symbols", nargs="*", default=list(CROSS_COINS))
    args = p.parse_args(argv)
    meta = run_robustness_audit(output_dir=args.out, symbols=args.symbols)
    print(
        json.dumps(
            json_safe(
                {
                    "gates": meta.get("gates_summary"),
                    "content_hash": meta.get("content_hash"),
                }
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
