"""C3.5c realized-outcome audit: natural exits on real fills (research-only).

Exit A: next opposite C3.5c fill open
Exit B: offline structure events (SM has no post-ENTRY trade lifecycle)
Exit C: wall-clock horizon close

No SM / Pine changes. No commits.
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

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import apply_pullback_entry, config_hash
from research.regime_scanner.pullback_entry_c3_5_diagnostics import baseline_a6
from research.regime_scanner.pullback_entry_c3_5_simple_path_audit import collect_filled_entries
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    ANALYZE_END,
    ANALYZE_START,
    DEFAULT_BASELINE_DIR,
    TF_MINUTES,
    build_parity_table,
    build_tf_frame,
    horizon_bars_for_tf,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path(
    "research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine/c35c_realized_outcome_audit"
)

TIMEFRAMES: tuple[str, ...] = ("15m", "5m")  # 15m primary
HORIZON_HOURS: tuple[float, ...] = (6, 12, 24, 48, 96, 192)
HORIZON_LABELS: tuple[str, ...] = ("6h", "12h", "24h", "48h", "4d", "8d")
COST_BPS: tuple[float, ...] = (10.0, 20.0)  # 0.10%, 0.20% roundtrip
MONTHS: tuple[str, ...] = ("2026-02", "2026-03", "2026-04")
MIN_CLOSED_FOR_CANDIDATE = 20

EXIT_B_DOC = {
    "sm_post_entry": (
        "C3.5 resets IDLE on the bar after ENTERED (reset_after_entry). "
        "No productive trade-lifecycle invalidation after fill."
    ),
    "research_exit_b": (
        "Offline: first confirmed opposite structure event after fill bar, "
        "exit at next bar open. Events from prepare_research_frame edges only."
    ),
    "exit_b_modes": [
        "opposite_external_bos",
        "opposite_major_dir",
        "protected_level_break_close",
        "first_any_structure_invalidator",
    ],
}


def _ret_pct(side: int, entry: float, exit_px: float) -> float:
    if side > 0:
        return (exit_px / entry - 1.0) * 100.0
    return (entry / exit_px - 1.0) * 100.0


def _mfe_mae(
    side: int,
    entry: float,
    highs: np.ndarray,
    lows: np.ndarray,
    start: int,
    end_inclusive: int,
) -> tuple[float, float]:
    """Path MFE/MAE in % from bars start..end_inclusive (inclusive).

    Favorable is positive; adverse is negative (signed from entry).
    """
    if end_inclusive < start:
        return 0.0, 0.0
    h = highs[start : end_inclusive + 1]
    l = lows[start : end_inclusive + 1]
    if side > 0:
        mfe = (float(np.max(h)) - entry) / entry * 100.0
        mae = (float(np.min(l)) - entry) / entry * 100.0
    else:
        mfe = (entry - float(np.min(l))) / entry * 100.0
        mae = (entry - float(np.max(h))) / entry * 100.0
    return float(mfe), float(mae)


def _filled_sorted(frame: pd.DataFrame, entries: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    n = len(frame)
    ts = list(frame["timestamp"])
    opens = frame["open"].astype(float).to_numpy()
    out = []
    for e in collect_filled_entries(entries, n):
        fi = int(e["fill_bar"])
        out.append(
            {
                **e,
                "fill_timestamp": ts[fi],
                "entry_price": float(e["entry_price"]),
                "entry_open_check": float(opens[fi]),
            }
        )
    out.sort(key=lambda x: (int(x["fill_bar"]), int(x["side"])))
    return out


def trades_exit_a_opposite_entry(
    frame: pd.DataFrame,
    filled: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
    variant: str,
) -> pd.DataFrame:
    """Sequential: each fill holds until next opposite-side fill open.

    Same-direction fills while flat after exit can open new trades; while in a
    trade, same-direction fills are ignored (do not exit). Opposite fill both
    closes prior and opens new (documented).
    """
    n = len(frame)
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    opens = frame["open"].astype(float).to_numpy()
    closes = frame["close"].astype(float).to_numpy()
    timestamps = list(frame["timestamp"])
    bar_h = TF_MINUTES[timeframe] / 60.0

    rows: list[dict[str, Any]] = []
    i = 0
    fills = list(filled)
    while i < len(fills):
        e = fills[i]
        side = int(e["side"])
        fill_i = int(e["fill_bar"])
        entry_px = float(e["entry_price"])
        # find next opposite fill
        j = i + 1
        exit_fill = None
        while j < len(fills):
            if int(fills[j]["side"]) == -side:
                exit_fill = fills[j]
                break
            j += 1

        closes_and_opens = False
        open_at_end = False
        if exit_fill is None:
            open_at_end = True
            # mark-to-market last close
            exit_i = n - 1
            exit_px = float(closes[exit_i])
            exit_reason = "open_at_end_mtm_last_close"
            exit_event_ts = timestamps[exit_i]
            exit_ts = timestamps[exit_i]
            closed = False
        else:
            exit_i = int(exit_fill["fill_bar"])
            exit_px = float(exit_fill["entry_price"])
            exit_reason = "opposite_c35c_entry"
            exit_event_ts = exit_fill.get("trigger_timestamp")  # opposite trigger bar
            exit_ts = exit_fill["fill_timestamp"]
            closed = True
            closes_and_opens = True  # opposite fill opens new trade

        # path bars: fill_i .. exit_i (inclusive of fill; for closed trade up to exit bar)
        path_end = exit_i if not open_at_end else exit_i
        mfe, mae = _mfe_mae(side, entry_px, highs, lows, fill_i, path_end)
        hold_bars = max(0, exit_i - fill_i)
        gross = _ret_pct(side, entry_px, exit_px)
        row = {
            "symbol": frame["symbol"].iloc[0],
            "timeframe": timeframe,
            "variant": variant,
            "side": e["side_name"],
            "setup_id": e.get("setup_id"),
            "trigger_timestamp": e.get("trigger_timestamp"),
            "entry_timestamp": e["fill_timestamp"],
            "entry_price": entry_px,
            "exit_family": "A_opposite_entry",
            "exit_reason": exit_reason,
            "exit_event_timestamp": exit_event_ts,
            "exit_timestamp": exit_ts,
            "exit_price": exit_px,
            "holding_bars": hold_bars,
            "holding_hours": hold_bars * bar_h,
            "gross_return_pct": gross,
            "net_return_0_10_pct": gross - 0.10,
            "net_return_0_20_pct": gross - 0.20,
            "maximum_favorable_pct": mfe,
            "maximum_adverse_pct": mae,
            "open_at_end": open_at_end,
            "incomplete_horizon": False,
            "closed": closed,
            "closes_and_opens_opposite": closes_and_opens,
            "fill_month": pd.Timestamp(e["fill_timestamp"]).tz_convert("UTC").strftime("%Y-%m"),
            "overlap_model": "sequential_non_overlapping",
            "horizon_label": "-",
        }
        rows.append(row)
        if exit_fill is None:
            break
        # continue from opposite fill as next open trade
        i = j
    return pd.DataFrame(rows)


def _first_structure_exit_bar(
    frame: pd.DataFrame,
    *,
    side: int,
    fill_bar: int,
    mode: str,
) -> tuple[int | None, str | None]:
    """Return event bar index (confirmed close) after fill_bar, or None."""
    n = len(frame)

    def _hit_at(j: int) -> str | None:
        row = frame.iloc[j]
        close = float(row["close"])
        if mode in {"opposite_external_bos", "first_any_structure_invalidator"}:
            if side > 0 and bool(row.get("arm_edge_external_bear")):
                return "opposite_external_bos"
            if side < 0 and bool(row.get("arm_edge_external_bull")):
                return "opposite_external_bos"
            if mode == "opposite_external_bos":
                return None
        if mode in {"opposite_major_dir", "first_any_structure_invalidator"}:
            if side > 0 and bool(row.get("arm_edge_major_bear")):
                return "opposite_major_dir"
            if side < 0 and bool(row.get("arm_edge_major_bull")):
                return "opposite_major_dir"
            if mode == "opposite_major_dir":
                return None
        if mode in {"protected_level_break_close", "first_any_structure_invalidator"}:
            if side > 0:
                pl = row.get("protected_low")
                if pl is not None and pd.notna(pl) and close < float(pl):
                    return "protected_low_break_close"
            else:
                ph = row.get("protected_high")
                if ph is not None and pd.notna(ph) and close > float(ph):
                    return "protected_high_break_close"
        return None

    for j in range(fill_bar + 1, n):
        reason = _hit_at(j)
        if reason is not None:
            return j, reason
    return None, None


def trades_exit_b_structure(
    frame: pd.DataFrame,
    filled: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
    variant: str,
    mode: str,
) -> pd.DataFrame:
    n = len(frame)
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    opens = frame["open"].astype(float).to_numpy()
    closes = frame["close"].astype(float).to_numpy()
    timestamps = list(frame["timestamp"])
    bar_h = TF_MINUTES[timeframe] / 60.0
    rows = []
    for e in filled:
        side = int(e["side"])
        fill_i = int(e["fill_bar"])
        entry_px = float(e["entry_price"])
        ev_bar, reason = _first_structure_exit_bar(frame, side=side, fill_bar=fill_i, mode=mode)
        open_at_end = ev_bar is None
        if open_at_end:
            exit_i = n - 1
            exit_px = float(closes[exit_i])
            exit_reason = f"open_at_end_mtm:{mode}"
            exit_event_ts = timestamps[exit_i]
            exit_ts = timestamps[exit_i]
            closed = False
        else:
            # exit next open after confirmed event bar
            if ev_bar + 1 >= n:
                open_at_end = True
                exit_i = n - 1
                exit_px = float(closes[exit_i])
                exit_reason = f"open_at_end_no_next_open:{reason}"
                exit_event_ts = timestamps[ev_bar]
                exit_ts = timestamps[exit_i]
                closed = False
            else:
                exit_i = ev_bar + 1
                exit_px = float(opens[exit_i])
                exit_reason = f"research_structure:{reason}"
                exit_event_ts = timestamps[ev_bar]
                exit_ts = timestamps[exit_i]
                closed = True
        mfe, mae = _mfe_mae(side, entry_px, highs, lows, fill_i, exit_i)
        hold = max(0, exit_i - fill_i)
        gross = _ret_pct(side, entry_px, exit_px)
        rows.append(
            {
                "symbol": frame["symbol"].iloc[0],
                "timeframe": timeframe,
                "variant": variant,
                "side": e["side_name"],
                "setup_id": e.get("setup_id"),
                "trigger_timestamp": e.get("trigger_timestamp"),
                "entry_timestamp": e["fill_timestamp"],
                "entry_price": entry_px,
                "exit_family": f"B_structure_{mode}",
                "exit_reason": exit_reason,
                "exit_event_timestamp": exit_event_ts,
                "exit_timestamp": exit_ts,
                "exit_price": exit_px,
                "holding_bars": hold,
                "holding_hours": hold * bar_h,
                "gross_return_pct": gross,
                "net_return_0_10_pct": gross - 0.10,
                "net_return_0_20_pct": gross - 0.20,
                "maximum_favorable_pct": mfe,
                "maximum_adverse_pct": mae,
                "open_at_end": open_at_end,
                "incomplete_horizon": False,
                "closed": closed,
                "fill_month": pd.Timestamp(e["fill_timestamp"]).tz_convert("UTC").strftime("%Y-%m"),
                "overlap_model": "independent_per_entry_may_overlap",
                "research_exit_not_in_sm": True,
                "horizon_label": "-",
            }
        )
    return pd.DataFrame(rows)


def trades_exit_c_horizon(
    frame: pd.DataFrame,
    filled: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
    variant: str,
) -> pd.DataFrame:
    n = len(frame)
    highs = frame["high"].astype(float).to_numpy()
    lows = frame["low"].astype(float).to_numpy()
    closes = frame["close"].astype(float).to_numpy()
    timestamps = list(frame["timestamp"])
    bar_h = TF_MINUTES[timeframe] / 60.0
    rows = []
    for label, hours in zip(HORIZON_LABELS, HORIZON_HOURS):
        bars, actual = horizon_bars_for_tf(timeframe, hours)
        for e in filled:
            side = int(e["side"])
            fill_i = int(e["fill_bar"])
            entry_px = float(e["entry_price"])
            # horizon end = fill_i + bars - 1 (inclusive window of `bars` bars from fill)
            target_i = fill_i + bars - 1
            incomplete = target_i >= n
            exit_i = min(target_i, n - 1)
            exit_px = float(closes[exit_i])
            hold = max(0, exit_i - fill_i)
            gross = _ret_pct(side, entry_px, exit_px)
            mfe, mae = _mfe_mae(side, entry_px, highs, lows, fill_i, exit_i)
            rows.append(
                {
                    "symbol": frame["symbol"].iloc[0],
                    "timeframe": timeframe,
                    "variant": variant,
                    "side": e["side_name"],
                    "setup_id": e.get("setup_id"),
                    "trigger_timestamp": e.get("trigger_timestamp"),
                    "entry_timestamp": e["fill_timestamp"],
                    "entry_price": entry_px,
                    "exit_family": "C_horizon",
                    "exit_reason": f"horizon_close_{label}",
                    "exit_event_timestamp": timestamps[exit_i],
                    "exit_timestamp": timestamps[exit_i],
                    "exit_price": exit_px,
                    "holding_bars": hold,
                    "holding_hours": hold * bar_h,
                    "horizon_label": label,
                    "horizon_target_hours": hours,
                    "horizon_actual_hours": actual,
                    "gross_return_pct": gross,
                    "net_return_0_10_pct": gross - 0.10,
                    "net_return_0_20_pct": gross - 0.20,
                    "maximum_favorable_pct": mfe,
                    "maximum_adverse_pct": mae,
                    "open_at_end": False,
                    "incomplete_horizon": incomplete,
                    "closed": not incomplete,
                    "fill_month": pd.Timestamp(e["fill_timestamp"]).tz_convert("UTC").strftime("%Y-%m"),
                    "overlap_model": "independent_per_entry_may_overlap",
                }
            )
    return pd.DataFrame(rows)


def _profit_factor(rets: pd.Series) -> float | None:
    gains = rets[rets > 0].sum()
    losses = rets[rets < 0].sum()
    if losses == 0:
        return float("inf") if gains > 0 else None
    return float(gains / abs(losses))


def _max_dd(rets: Sequence[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rets:
        equity += float(r)
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return float(max_dd)


def summarize_trades(df: pd.DataFrame, *, closed_only: bool = True) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    rows = []
    group_cols = ["timeframe", "exit_family", "side"]
    if "horizon_label" in df.columns:
        # split C by horizon
        pass
    # Build a grouping key including horizon when present
    work = df.copy()
    if "horizon_label" not in work.columns:
        work["horizon_label"] = "-"
    work["horizon_label"] = work["horizon_label"].fillna("-").astype(str)

    def _one_row(tf: str, fam: str, side: str, hz: str, g0: pd.DataFrame) -> dict[str, Any]:
        g = g0[g0["closed"] == True] if closed_only else g0  # noqa: E712
        n_all = len(g0)
        n = len(g)
        if n == 0:
            return {
                "timeframe": tf,
                "exit_family": fam,
                "side": side,
                "horizon_label": hz,
                "n_trades": n_all,
                "n_closed": 0,
            }
        g = g.sort_values("entry_timestamp") if "entry_timestamp" in g.columns else g
        gross = g["gross_return_pct"]
        net10 = g["net_return_0_10_pct"]
        net20 = g["net_return_0_20_pct"]
        wins_g = gross > 0
        losses_g = ~wins_g
        sum_g = float(gross.sum())
        best = float(gross.max())
        best_share = (best / sum_g) if sum_g > 0 else None
        return {
            "timeframe": tf,
            "exit_family": fam,
            "side": side,
            "horizon_label": hz,
            "n_trades": n_all,
            "n_closed": n,
            "winrate_gross": float(wins_g.mean()),
            "winrate_net_0_10": float((net10 > 0).mean()),
            "winrate_net_0_20": float((net20 > 0).mean()),
            "mean_gross": float(gross.mean()),
            "median_gross": float(gross.median()),
            "sum_gross": sum_g,
            "mean_net_0_10": float(net10.mean()),
            "mean_net_0_20": float(net20.mean()),
            "sum_net_0_20": float(net20.sum()),
            "profit_factor_gross": _profit_factor(gross),
            "avg_win_gross": float(gross[wins_g].mean()) if wins_g.any() else None,
            "avg_loss_gross": float(gross[losses_g].mean()) if losses_g.any() else None,
            "payoff_ratio": (
                float(gross[wins_g].mean() / abs(gross[losses_g].mean()))
                if wins_g.any() and losses_g.any() and gross[losses_g].mean() != 0
                else None
            ),
            "worst_trade": float(gross.min()),
            "best_trade": best,
            "best_trade_share_of_sum": best_share,
            "median_holding_hours": float(g["holding_hours"].median()),
            "max_dd_sum_gross": _max_dd(gross.tolist()),
        }

    for (tf, fam, side, hz), g0 in work.groupby(
        ["timeframe", "exit_family", "side", "horizon_label"], dropna=False
    ):
        rows.append(_one_row(str(tf), str(fam), str(side), str(hz), g0))
    for (tf, fam, hz), g0 in work.groupby(["timeframe", "exit_family", "horizon_label"], dropna=False):
        rows.append(_one_row(str(tf), str(fam), "both", str(hz), g0))
    return pd.DataFrame(rows)


def summarize_by_month(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    work = df[df["closed"] == True].copy()  # noqa: E712
    if "horizon_label" not in work.columns:
        work["horizon_label"] = "-"
    work["horizon_label"] = work["horizon_label"].fillna("-").astype(str)
    rows = []
    for keys, g in work.groupby(
        ["timeframe", "exit_family", "horizon_label", "fill_month"], dropna=False
    ):
        tf, fam, hz, month = keys
        gross = g["gross_return_pct"]
        net20 = g["net_return_0_20_pct"]
        rows.append(
            {
                "timeframe": tf,
                "exit_family": fam,
                "horizon_label": str(hz) if pd.notna(hz) else "-",
                "fill_month": month,
                "n_closed": len(g),
                "mean_gross": float(gross.mean()),
                "mean_net_0_20": float(net20.mean()),
                "sum_net_0_20": float(net20.sum()),
                "winrate_net_0_20": float((net20 > 0).mean()),
            }
        )
    return pd.DataFrame(rows)


def stability_for_rule(by_month: pd.DataFrame, *, timeframe: str, exit_family: str, horizon_label: str = "-") -> str:
    hz = "-" if horizon_label is None or (isinstance(horizon_label, float) and math.isnan(horizon_label)) else str(horizon_label)
    bm = by_month.copy()
    bm["horizon_label"] = bm["horizon_label"].fillna("-").astype(str)
    sub = bm[
        (bm["timeframe"] == timeframe)
        & (bm["exit_family"] == exit_family)
        & (bm["horizon_label"] == hz)
    ]
    vals = []
    for m in MONTHS:
        r = sub[sub["fill_month"] == m]
        if r.empty or int(r["n_closed"].sum()) == 0:
            vals.append(None)
        else:
            vals.append(float(r["mean_net_0_20"].mean()))
    present = [v for v in vals if v is not None]
    if len(present) < 2:
        return "insufficient_sample"
    signs = [1 if v > 0 else (-1 if v < 0 else 0) for v in present]
    if len(set(s for s in signs if s != 0)) > 1:
        return "sign_flip"
    abs_vals = [abs(v) for v in present]
    if sum(abs_vals) > 0 and max(abs_vals) / sum(abs_vals) >= 0.7:
        return "one_month_dominates"
    if all(v > 0 for v in present):
        return "stable_positive"
    if all(v < 0 for v in present):
        return "stable_negative"
    return "insufficient_sample"


def equity_curve_exit_a(df_a: pd.DataFrame) -> pd.DataFrame:
    """Non-overlapping sequential Exit A closed trades only."""
    if df_a.empty:
        return pd.DataFrame()
    g = df_a[(df_a["closed"] == True) & (df_a["exit_family"] == "A_opposite_entry")].copy()  # noqa: E712
    g = g.sort_values("entry_timestamp")
    rows = []
    eq_g = eq10 = eq20 = 0.0
    for _, t in g.iterrows():
        eq_g += float(t["gross_return_pct"])
        eq10 += float(t["net_return_0_10_pct"])
        eq20 += float(t["net_return_0_20_pct"])
        rows.append(
            {
                "timeframe": t["timeframe"],
                "entry_timestamp": t["entry_timestamp"],
                "exit_timestamp": t["exit_timestamp"],
                "side": t["side"],
                "setup_id": t["setup_id"],
                "gross_return_pct": t["gross_return_pct"],
                "net_return_0_10_pct": t["net_return_0_10_pct"],
                "net_return_0_20_pct": t["net_return_0_20_pct"],
                "equity_gross": eq_g,
                "equity_net_0_10": eq10,
                "equity_net_0_20": eq20,
            }
        )
    return pd.DataFrame(rows)


def run_realized_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    cfg = baseline_a6()
    all_trades: list[pd.DataFrame] = []
    equity_parts: list[pd.DataFrame] = []
    counts: dict[str, Any] = {}

    for tf in TIMEFRAMES:
        frame = build_tf_frame(symbol, tf)
        _tl, entries, _lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
        _parity_df, parity_rep = build_parity_table(
            frame, entries, variant=cfg.name, timeframe=tf, arming_type=cfg.arming_type
        )
        if not parity_rep["safe_to_compute_paths"]:
            raise RuntimeError(f"parity unsafe for {tf}")
        filled = _filled_sorted(frame, entries)
        counts[tf] = {"n_fills": len(filled), "parity_ok": True}

        a = trades_exit_a_opposite_entry(frame, filled, timeframe=tf, variant=cfg.name)
        b_modes = []
        for mode in EXIT_B_DOC["exit_b_modes"]:
            b_modes.append(
                trades_exit_b_structure(frame, filled, timeframe=tf, variant=cfg.name, mode=mode)
            )
        b = pd.concat(b_modes, ignore_index=True) if b_modes else pd.DataFrame()
        c = trades_exit_c_horizon(frame, filled, timeframe=tf, variant=cfg.name)

        all_trades.extend([a, b, c])
        eq = equity_curve_exit_a(a)
        if not eq.empty:
            equity_parts.append(eq)

        a.to_csv(output_dir / f"opposite_signal_trades_{tf}.csv", index=False)
        b.to_csv(output_dir / f"structure_exit_trades_{tf}.csv", index=False)
        c.to_csv(output_dir / f"horizon_exit_trades_{tf}.csv", index=False)

    trades = pd.concat([t for t in all_trades if not t.empty], ignore_index=True)
    summary = summarize_trades(trades, closed_only=True)
    by_month = summarize_by_month(trades)
    by_side = summary[summary["side"].isin(["long", "short"])].copy() if not summary.empty else summary

    # stability annotations on summary both-sides rows
    if not summary.empty and not by_month.empty:
        stabs = []
        for _, r in summary.iterrows():
            hz = r.get("horizon_label")
            if hz is None or (isinstance(hz, float) and math.isnan(hz)):
                hz = "-"
            stabs.append(
                stability_for_rule(
                    by_month,
                    timeframe=str(r["timeframe"]),
                    exit_family=str(r["exit_family"]),
                    horizon_label=str(hz),
                )
            )
        summary = summary.copy()
        summary["stability"] = stabs

    equity = pd.concat(equity_parts, ignore_index=True) if equity_parts else pd.DataFrame()

    trades.to_csv(output_dir / "realized_trade_cases.csv", index=False)
    summary.to_csv(output_dir / "realized_summary.csv", index=False)
    by_month.to_csv(output_dir / "realized_by_month.csv", index=False)
    by_side.to_csv(output_dir / "realized_by_side.csv", index=False)
    # Combined convenience copies
    if not trades.empty:
        trades[trades["exit_family"] == "A_opposite_entry"].to_csv(
            output_dir / "opposite_signal_trades.csv", index=False
        )
        trades[trades["exit_family"].astype(str).str.startswith("B_")].to_csv(
            output_dir / "structure_exit_trades.csv", index=False
        )
        trades[trades["exit_family"] == "C_horizon"].to_csv(
            output_dir / "horizon_exit_trades.csv", index=False
        )
    equity.to_csv(output_dir / "equity_curve_non_overlapping.csv", index=False)

    meta = {
        "symbol": symbol,
        "variant": cfg.name,
        "config_hash": config_hash(cfg),
        "analyze_start": ANALYZE_START,
        "analyze_end": ANALYZE_END,
        "timeframes": list(TIMEFRAMES),
        "counts": counts,
        "exit_b_documentation": EXIT_B_DOC,
        "cost_model": {
            "gross": "no costs",
            "net_0_10": "subtract 0.10% roundtrip from gross return_pct",
            "net_0_20": "subtract 0.20% roundtrip from gross return_pct",
            "no_leverage": True,
        },
        "baseline_reference_hash": C2_BASELINE_HASH,
        "production_sm_unchanged": True,
        "pine_unchanged": True,
    }
    # candidate flags
    candidates = []
    if not summary.empty:
        for _, r in summary[(summary["side"] == "both")].iterrows():
            share = r.get("best_trade_share_of_sum")
            dominated = share is not None and pd.notna(share) and float(share) >= 0.5
            ok = (
                int(r["n_closed"]) >= MIN_CLOSED_FOR_CANDIDATE
                and float(r.get("mean_net_0_20") or -1) > 0
                and str(r.get("stability")) == "stable_positive"
                and not dominated
            )
            if ok:
                # also need >=2 positive months — implied by stable_positive
                candidates.append(
                    {
                        "timeframe": r["timeframe"],
                        "exit_family": r["exit_family"],
                        "horizon_label": r["horizon_label"],
                        "mean_net_0_20": r["mean_net_0_20"],
                        "n_closed": r["n_closed"],
                        "stability": r["stability"],
                    }
                )
    meta["candidates"] = candidates
    blob = json.dumps(json_safe({k: v for k, v in meta.items()}), sort_keys=True).encode()
    meta["content_hash"] = hashlib.sha1(blob).hexdigest()
    (output_dir / "metadata.json").write_text(json.dumps(json_safe(meta), indent=2), encoding="utf-8")
    write_report(output_dir, meta, summary, by_month)
    return meta


def write_report(
    output_dir: Path,
    meta: Mapping[str, Any],
    summary: pd.DataFrame,
    by_month: pd.DataFrame,
) -> None:
    lines = [
        "# C3.5c Realized Outcome Audit",
        "",
        f"Symbol: {meta.get('symbol')} · Variant: {meta.get('variant')} · "
        f"Window: {meta.get('analyze_start')} → {meta.get('analyze_end')}",
        "",
        "## Exit B note",
        "",
        EXIT_B_DOC["sm_post_entry"],
        "",
        EXIT_B_DOC["research_exit_b"],
        "",
        "## 15m primary answers",
        "",
    ]

    def _pick(tf: str, fam: str, hz: str = "-") -> pd.Series | None:
        if summary.empty:
            return None
        s = summary.copy()
        s["horizon_label"] = s["horizon_label"].fillna("-").astype(str)
        sub = s[
            (s["timeframe"] == tf)
            & (s["exit_family"] == fam)
            & (s["side"] == "both")
            & (s["horizon_label"] == hz)
        ]
        return sub.iloc[0] if len(sub) else None

    a15 = _pick("15m", "A_opposite_entry")
    c24 = _pick("15m", "C_horizon", "24h")
    c48 = _pick("15m", "C_horizon", "48h")
    a5 = _pick("5m", "A_opposite_entry")
    c24_5 = _pick("5m", "C_horizon", "24h")
    c48_5 = _pick("5m", "C_horizon", "48h")

    def _fmt(r: pd.Series | None) -> str:
        if r is None:
            return "n/a"
        share = r.get("best_trade_share_of_sum")
        share_s = f"{100 * float(share):.0f}%" if share is not None and pd.notna(share) else "?"
        return (
            f"n_closed={int(r['n_closed'])} · mean_gross={float(r['mean_gross']):.3f}% · "
            f"mean_net0.20={float(r['mean_net_0_20']):.3f}% · "
            f"WR_net0.20={100 * float(r['winrate_net_0_20']):.1f}% · "
            f"stab={r.get('stability')} · best_share={share_s}"
        )

    # Q1
    if a15 is not None and float(a15["mean_net_0_20"]) > 0:
        q1 = f"Ja (netto positiv), aber kein Kandidat: {_fmt(a15)}"
    elif a15 is not None:
        q1 = f"Nein: {_fmt(a15)}"
    else:
        q1 = "n/a"
    # Q2
    def _hz_ans(r: pd.Series | None, label: str) -> str:
        if r is None:
            return f"{label}: n/a"
        ok = float(r["mean_net_0_20"]) > 0
        return f"{label}: {'Ja' if ok else 'Nein'} — {_fmt(r)}"

    # Q3 most stable
    both = summary[(summary["side"] == "both") & (summary["timeframe"] == "15m")].copy()
    if not both.empty:
        both["horizon_label"] = both["horizon_label"].fillna("-").astype(str)
        pos = both[both["stability"] == "stable_positive"].sort_values(
            "mean_net_0_20", ascending=False
        )
        if len(pos):
            best = pos.iloc[0]
            q3 = (
                f"{best['exit_family']}/{best['horizon_label']} "
                f"(stable_positive, mean_net0.20={float(best['mean_net_0_20']):.3f}%)"
            )
        else:
            neg = both[both["stability"] == "stable_negative"]
            if len(neg):
                q3 = (
                    "Kein stable_positive; stabil negativ: "
                    + ", ".join(f"{r.exit_family}/{r.horizon_label}" for r in neg.itertuples())
                )
            else:
                q3 = "Kein stable_positive auf 15m (sign_flip / one_month_dominates / insufficient)."
    else:
        q3 = "n/a"

    # Q4 any positive net EV after 0.20?
    pos_net = both[both["mean_net_0_20"] > 0] if not both.empty else both
    q4_rules = (
        ", ".join(f"{r.exit_family}/{r.horizon_label}" for r in pos_net.itertuples())
        if len(pos_net)
        else "keine"
    )

    # Q5 15 vs 5
    if a15 is not None and a5 is not None:
        better = float(a15["mean_net_0_20"]) > float(a5["mean_net_0_20"])
        q5 = (
            f"{'Ja' if better else 'Nein'} für Exit A "
            f"(15m mean_net0.20={float(a15['mean_net_0_20']):.3f}% vs "
            f"5m {float(a5['mean_net_0_20']):.3f}%). "
            f"5m C24: {_fmt(c24_5)}; 5m C48: {_fmt(c48_5)}"
        )
    else:
        q5 = "n/a"

    lines.append(f"1. Opposite-signal bis Gegensignal netto profitabel? **{q1}**")
    lines.append(f"2. Zeitbasiert 24h/48h netto? {_hz_ans(c24, '24h')}; {_hz_ans(c48, '48h')}")
    lines.append(f"3. Stabilster Exit Feb–Apr: {q3}")
    lines.append(
        f"4. Positiver EV nach 0.20% Roundtrip? Teilweise ja bei: {q4_rules}. "
        f"Kandidaten (Gates): {meta.get('candidates') or []}."
    )
    lines.append(f"5. 15m klar besser als 5m? {q5}")
    lines.append("")
    lines.append("## 15m summary (both sides)")
    lines.append("")
    if not both.empty:
        show = both[
            [
                "exit_family",
                "horizon_label",
                "n_closed",
                "mean_gross",
                "mean_net_0_20",
                "winrate_net_0_20",
                "stability",
            ]
        ].sort_values(["exit_family", "horizon_label"])
        lines.append("```")
        lines.append(show.to_string(index=False))
        lines.append("```")
    lines.append("")
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5c realized outcome audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = p.parse_args(argv)
    meta = run_realized_audit(symbol=args.symbol, output_dir=args.out)
    print(json.dumps(json_safe({"counts": meta["counts"], "candidates": meta["candidates"]}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
