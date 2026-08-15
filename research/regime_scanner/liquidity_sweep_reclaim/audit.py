"""Frame loading, aggregation, gates and report helpers for LSR v1."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.c35c_signal_store.build import (
    load_symbol_5m_mysql,
    resolve_analyze_window,
)
from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.indicator_feature_store import required_indicator_warmup_bars
from research.regime_scanner.liquidity_sweep_reclaim.config import (
    A6_PARENT_LABEL,
    COST_STRESS_PCT,
    EXIT_BENCHMARKS,
    LEVEL_FAMILIES,
    MAJORS,
    MFE_HORIZONS,
    PENETRATIONS,
    RECLAIMS,
    STP_RESULTS_DIR,
    STP_VARIANT,
    TOP3,
    LSRConfig,
    all_variants,
    default_config,
    variant_id,
)
from research.regime_scanner.liquidity_sweep_reclaim.levels import (
    L3_AVAILABLE,
    L3_UNAVAILABLE_REASON,
    attach_c31_range_columns,
)
from research.regime_scanner.liquidity_sweep_reclaim.outcomes import (
    cost_stress_from_gross,
    exit_benchmark_outcome_arrays,
    forward_outcomes_fast,
    frame_arrays,
)
from research.regime_scanner.liquidity_sweep_reclaim.sequential import apply_sequential
from research.regime_scanner.liquidity_sweep_reclaim.strategy import run_strategy_on_frame
from research.regime_scanner.pullback_entry_c3_5 import prepare_research_frame
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import aggregate_complete_from_5m
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    assign_split,
    fixed_chrono_splits,
)


def build_15m_frame(symbol: str) -> tuple[pd.DataFrame, dict[str, Any], pd.Timestamp, pd.Timestamp]:
    full_5m, mysql_meta = load_symbol_5m_mysql(symbol)
    a0, a1 = resolve_analyze_window(full_5m)
    warm_bars = max(required_indicator_warmup_bars(), 400)
    load_start = a0 - pd.Timedelta(minutes=5 * warm_bars)
    ts = pd.to_datetime(full_5m["timestamp"], utc=True)
    sliced = full_5m.loc[(ts >= load_start) & (ts < a1)].copy().reset_index(drop=True)
    decision = a1 + pd.Timedelta(hours=1)
    ohlcv15 = aggregate_complete_from_5m(sliced, "15m", decision_time=decision)
    frame = prepare_research_frame(ohlcv15, ohlcv_15m=None, ohlcv_30m=None)
    frame = attach_c31_range_columns(frame, analyze_start=a0, analyze_end=a1)
    meta = {
        **mysql_meta,
        "analyze_start": str(a0),
        "analyze_end_exclusive": str(a1),
        "n_15m": int(len(frame)),
        "ohlcv_sha1_5m": mysql_meta.get("ohlcv_sha1"),
        "warmup_5m_bars": warm_bars,
        "l3_available": L3_AVAILABLE,
        "l3_reason": L3_UNAVAILABLE_REASON,
    }
    return frame.reset_index(drop=True), meta, a0, a1


def metrics_block(pnls: np.ndarray) -> dict[str, Any]:
    pnls = np.asarray(pnls, dtype=float)
    pnls = pnls[np.isfinite(pnls)]
    if len(pnls) == 0:
        return {
            "n": 0,
            "expectation": None,
            "pf": None,
            "sum_pnl": 0.0,
            "winrate": None,
            "max_dd": 0.0,
            "max_losing_streak": 0,
            "median_bars_held": None,
            "payoff_ratio": None,
        }
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_sum = float(wins.sum()) if len(wins) else 0.0
    loss_sum = float(-losses.sum()) if len(losses) else 0.0
    pf = None if loss_sum < 1e-15 else win_sum / loss_sum
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = float((eq - peak).min())
    streak = best = 0
    for x in pnls:
        if x < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(-losses.mean()) if len(losses) else None
    payoff = None
    if avg_win is not None and avg_loss is not None and avg_loss > 1e-15:
        payoff = avg_win / avg_loss
    return {
        "n": int(len(pnls)),
        "expectation": float(np.mean(pnls)),
        "pf": pf,
        "sum_pnl": float(np.sum(pnls)),
        "winrate": float(np.mean(pnls > 0)),
        "max_dd": dd,
        "max_losing_streak": int(best),
        "payoff_ratio": payoff,
    }


def collect_symbol_signals(
    symbol: str,
    frame: pd.DataFrame,
    a0: pd.Timestamp,
    a1: pd.Timestamp,
    *,
    level_families: tuple[str, ...],
    penetrations: tuple[str, ...],
    reclaims: tuple[str, ...],
    cfg: LSRConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    splits = fixed_chrono_splits(a0, a1)
    events, setups = run_strategy_on_frame(
        frame,
        symbol=symbol,
        level_families=level_families,
        penetrations=penetrations,
        reclaims=reclaims,
        cfg=cfg,
        analyze_start=a0,
    )
    signal_rows: list[dict[str, Any]] = []
    trade_rows: list[dict[str, Any]] = []
    arrays = frame_arrays(frame)
    keep_feat_keys = (
        "level_age",
        "major_direction",
        "adx",
        "atr_pct",
        "penetration_class",
        "reclaim_type",
        "sw_penetration_atr",
        "sw_penetration_pct",
        "sw_wick_beyond_level_pct",
        "sw_candle_range_atr",
        "sw_volume_ratio",
        "reclaim_strength_pct",
        "bars_to_reclaim",
        "next_open_gap_atr",
        "confirmation_strength",
        "c31_state",
        "utc_hour",
        "weekday",
        "month",
        "distance_ema_20_atr",
        "distance_ema_59_atr",
        "distance_ema_200_atr",
        "lvl_range_width_atr",
        "lvl_range_score",
        "lvl_level_age",
    )
    for ev in events:
        fill_ts = pd.Timestamp(ev.fill_timestamp)
        if fill_ts.tzinfo is None:
            fill_ts = fill_ts.tz_localize("UTC")
        else:
            fill_ts = fill_ts.tz_convert("UTC")
        if fill_ts < a0 or fill_ts >= a1:
            continue
        fwd = forward_outcomes_fast(
            arrays, fill_i=ev.fill_bar, entry=ev.entry_price, side=ev.side
        )
        split_raw = assign_split(fill_ts, splits)
        split = {"development": "dev", "validation": "validation", "oos": "oos"}.get(
            split_raw, split_raw
        )
        feats = {f"feat_{k}": ev.features.get(k) for k in keep_feat_keys if k in ev.features}
        base = {
            "symbol": symbol,
            "variant": ev.variant,
            "level_family": ev.level_family,
            "penetration_class": ev.penetration_class,
            "reclaim_type": ev.reclaim_type,
            "side": ev.side,
            "setup_id": ev.setup_id,
            "level_id": ev.level_id,
            "level_value": ev.level_value,
            "level_confirmed_timestamp": ev.level_confirmed_timestamp,
            "sweep_timestamp": ev.sweep_timestamp,
            "reclaim_timestamp": ev.reclaim_timestamp,
            "confirmation_timestamp": ev.confirmation_timestamp,
            "trigger_timestamp": ev.trigger_timestamp,
            "fill_timestamp": str(fill_ts),
            "entry_price": ev.entry_price,
            "trigger_price": ev.trigger_price,
            "penetration_atr": ev.penetration_atr,
            "penetration_pct": ev.penetration_pct,
            "bars_sweep_to_reclaim": ev.bars_sweep_to_reclaim,
            "bars_reclaim_to_trigger": ev.bars_reclaim_to_trigger,
            "setup_age": ev.setup_age,
            "split": split,
            "fill_bar": ev.fill_bar,
            "trigger_bar": ev.trigger_bar,
            **fwd,
            **feats,
        }
        signal_rows.append(base)
        trade_common = {
            "symbol": symbol,
            "variant": ev.variant,
            "level_family": ev.level_family,
            "penetration_class": ev.penetration_class,
            "reclaim_type": ev.reclaim_type,
            "side": ev.side,
            "setup_id": ev.setup_id,
            "fill_timestamp": str(fill_ts),
            "entry_price": ev.entry_price,
            "split": split,
        }
        for exit_id in EXIT_BENCHMARKS:
            oc = exit_benchmark_outcome_arrays(
                arrays,
                fill_i=ev.fill_bar,
                entry=ev.entry_price,
                side=ev.side,
                exit_id=exit_id,
            )
            gross = oc.get("gross_pnl_pct")
            trade_rows.append(
                {
                    **trade_common,
                    "exit_id": exit_id,
                    "effective_cost_pct": EXIT_BENCHMARKS[exit_id][3],
                    "net_pnl_pct": oc.get("net_pnl_pct"),
                    "gross_pnl_pct": gross,
                    "exit_reason": oc.get("exit_reason"),
                    "bars_held": oc.get("bars_held"),
                    "mfe_pct": oc.get("mfe_pct"),
                    "mae_pct": oc.get("mae_pct"),
                    "is_winner": oc.get("is_winner"),
                    "same_bar_ambiguous": oc.get("same_bar_ambiguous"),
                    "time_exit": oc.get("time_exit"),
                }
            )
            # cost stress derived from same path (gross − 0.25), no second replay
            net25 = cost_stress_from_gross(gross, COST_STRESS_PCT)
            trade_rows.append(
                {
                    **trade_common,
                    "exit_id": f"{exit_id}_c025",
                    "effective_cost_pct": COST_STRESS_PCT,
                    "net_pnl_pct": net25,
                    "gross_pnl_pct": gross,
                    "exit_reason": oc.get("exit_reason"),
                    "bars_held": oc.get("bars_held"),
                    "mfe_pct": oc.get("mfe_pct"),
                    "mae_pct": oc.get("mae_pct"),
                    "is_winner": bool(net25 is not None and net25 > 0),
                    "same_bar_ambiguous": oc.get("same_bar_ambiguous"),
                    "time_exit": oc.get("time_exit"),
                }
            )
    for s in setups:
        s.setdefault("symbol", symbol)
    return signal_rows, trade_rows, setups


def summarize_trades(trades: pd.DataFrame, *, mode: str) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame()
    df = trades.copy()
    if mode == "sequential":
        df = df[df["taken_sequential"] == True]  # noqa: E712
    rows = []
    for (variant, side, exit_id), g in df.groupby(["variant", "side", "exit_id"], dropna=False):
        m = metrics_block(g["net_pnl_pct"].to_numpy())
        m["median_bars_held"] = float(g["bars_held"].median()) if len(g) else None
        same_bar_rate = float(g["same_bar_ambiguous"].mean()) if "same_bar_ambiguous" in g else None
        time_exit_rate = float(g["time_exit"].mean()) if "time_exit" in g else None
        rows.append(
            {
                "mode": mode,
                "variant": variant,
                "side": side,
                "exit_id": exit_id,
                **m,
                "same_bar_rate": same_bar_rate,
                "time_exit_rate": time_exit_rate,
                "mean_mfe": float(g["mfe_pct"].mean()) if g["mfe_pct"].notna().any() else None,
                "mean_mae": float(g["mae_pct"].mean()) if g["mae_pct"].notna().any() else None,
            }
        )
    # both sides combined
    for (variant, exit_id), g in df.groupby(["variant", "exit_id"], dropna=False):
        m = metrics_block(g["net_pnl_pct"].to_numpy())
        m["median_bars_held"] = float(g["bars_held"].median()) if len(g) else None
        rows.append(
            {
                "mode": mode,
                "variant": variant,
                "side": "both",
                "exit_id": exit_id,
                **m,
                "same_bar_rate": float(g["same_bar_ambiguous"].mean())
                if "same_bar_ambiguous" in g
                else None,
                "time_exit_rate": float(g["time_exit"].mean()) if "time_exit" in g else None,
                "mean_mfe": float(g["mfe_pct"].mean()) if g["mfe_pct"].notna().any() else None,
                "mean_mae": float(g["mae_pct"].mean()) if g["mae_pct"].notna().any() else None,
            }
        )
    return pd.DataFrame(rows)


def slice_summaries(trades: pd.DataFrame, signals: pd.DataFrame) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if trades.empty:
        return out
    base = trades[
        trades["exit_id"].isin(list(EXIT_BENCHMARKS.keys()) + [f"{k}_c025" for k in EXIT_BENCHMARKS])
    ].copy()
    seq = apply_sequential(base)
    out["independent_summary"] = summarize_trades(seq, mode="independent")
    out["sequential_summary"] = summarize_trades(seq, mode="sequential")

    def _by(col: str, name: str) -> pd.DataFrame:
        rows = []
        for (variant, side, exit_id, key), g in seq.groupby(
            ["variant", "side", "exit_id", col], dropna=False
        ):
            m = metrics_block(g["net_pnl_pct"].to_numpy())
            rows.append(
                {
                    "variant": variant,
                    "side": side,
                    "exit_id": exit_id,
                    name: key,
                    **m,
                }
            )
        return pd.DataFrame(rows)

    out["summary_by_coin"] = _by("symbol", "symbol")
    out["summary_by_split"] = _by("split", "split")
    out["summary_by_level_family"] = _by("level_family", "level_family")
    out["summary_by_penetration"] = _by("penetration_class", "penetration_class")
    out["summary_by_reclaim_type"] = _by("reclaim_type", "reclaim_type")

    # month
    if not signals.empty:
        sig = signals.copy()
        sig["fill_timestamp"] = pd.to_datetime(sig["fill_timestamp"], utc=True)
        sig["month"] = sig["fill_timestamp"].dt.strftime("%Y-%m")
        month_map = sig.set_index("setup_id")["month"].to_dict()
        seq2 = seq.copy()
        seq2["month"] = seq2["setup_id"].map(month_map)
        rows = []
        for (variant, side, exit_id, month), g in seq2.groupby(
            ["variant", "side", "exit_id", "month"], dropna=False
        ):
            m = metrics_block(g["net_pnl_pct"].to_numpy())
            rows.append(
                {"variant": variant, "side": side, "exit_id": exit_id, "month": month, **m}
            )
        out["summary_by_month"] = pd.DataFrame(rows)

    # equal / median coin (per variant×side×exit on sequential)
    eq_rows, med_rows = [], []
    for (variant, side, exit_id), g in seq[seq["taken_sequential"] == True].groupby(  # noqa: E712
        ["variant", "side", "exit_id"]
    ):
        per_coin = []
        for sym, gs in g.groupby("symbol"):
            m = metrics_block(gs["net_pnl_pct"].to_numpy())
            per_coin.append({"symbol": sym, **m})
        if not per_coin:
            continue
        expectations = [r["expectation"] for r in per_coin if r["expectation"] is not None]
        eq_rows.append(
            {
                "variant": variant,
                "side": side,
                "exit_id": exit_id,
                "n_coins": len(per_coin),
                "equal_coin_expectation": float(np.mean(expectations)) if expectations else None,
                "pct_positive_coins": float(np.mean([e > 0 for e in expectations]))
                if expectations
                else None,
            }
        )
        med_rows.append(
            {
                "variant": variant,
                "side": side,
                "exit_id": exit_id,
                "median_coin_expectation": float(np.median(expectations)) if expectations else None,
            }
        )
    out["summary_equal_coin"] = pd.DataFrame(eq_rows)
    out["summary_median_coin"] = pd.DataFrame(med_rows)

    # ablation slices
    def _filter_sum(mask, label: str) -> pd.DataFrame:
        g0 = seq[seq["taken_sequential"] == True]  # noqa: E712
        sub = g0[mask(g0)]
        rows = []
        for (variant, side, exit_id), g in sub.groupby(["variant", "side", "exit_id"]):
            m = metrics_block(g["net_pnl_pct"].to_numpy())
            rows.append({"slice": label, "variant": variant, "side": side, "exit_id": exit_id, **m})
        return pd.DataFrame(rows)

    out["summary_without_apt"] = _filter_sum(lambda d: d["symbol"] != "APTUSDT", "without_apt")
    # top1 by n within variant
    top1_rows = []
    g0 = seq[seq["taken_sequential"] == True]  # noqa: E712
    for (variant, side, exit_id), g in g0.groupby(["variant", "side", "exit_id"]):
        if g.empty:
            continue
        top = g["symbol"].value_counts().idxmax()
        sub = g[g["symbol"] != top]
        m = metrics_block(sub["net_pnl_pct"].to_numpy())
        top1_rows.append(
            {
                "variant": variant,
                "side": side,
                "exit_id": exit_id,
                "excluded_top1": top,
                **m,
            }
        )
    out["summary_without_top1"] = pd.DataFrame(top1_rows)
    out["summary_without_top3"] = _filter_sum(lambda d: ~d["symbol"].isin(TOP3), "without_top3")
    maj = _filter_sum(lambda d: d["symbol"].isin(MAJORS), "majors")
    alt = _filter_sum(lambda d: ~d["symbol"].isin(MAJORS), "altcoins")
    out["summary_majors_vs_altcoins"] = pd.concat([maj, alt], ignore_index=True)

    # common window: intersection of fill ranges across coins that have any signal
    if not signals.empty:
        sig = signals.copy()
        sig["fill_timestamp"] = pd.to_datetime(sig["fill_timestamp"], utc=True)
        starts = sig.groupby("symbol")["fill_timestamp"].min()
        ends = sig.groupby("symbol")["fill_timestamp"].max()
        cw0, cw1 = starts.max(), ends.min()
        out["common_window_bounds"] = pd.DataFrame(
            [{"common_start": str(cw0), "common_end": str(cw1)}]
        )
        if cw0 < cw1:
            sub = g0[
                (pd.to_datetime(g0["fill_timestamp"], utc=True) >= cw0)
                & (pd.to_datetime(g0["fill_timestamp"], utc=True) <= cw1)
            ]
            rows = []
            for (variant, side, exit_id), g in sub.groupby(["variant", "side", "exit_id"]):
                m = metrics_block(g["net_pnl_pct"].to_numpy())
                rows.append(
                    {
                        "variant": variant,
                        "side": side,
                        "exit_id": exit_id,
                        **m,
                        "common_start": str(cw0),
                        "common_end": str(cw1),
                    }
                )
            out["summary_common_window"] = pd.DataFrame(rows)
    return out


def load_a6_fills(store: C35cPathStore, parent_label: str = A6_PARENT_LABEL) -> pd.DataFrame:
    children = store.find_child_runs(parent_label)
    rows = []
    for run in children:
        rid = str(run["run_id"])
        sym = str(run.get("symbol") or "").upper()
        sigs, outcomes, _, _ = store.load_signals_bundle(
            rid, outcome_version="tp3_sl2_h192_cost020_v1"
        )
        for s in sigs:
            direction = str(s.get("direction") or "").lower()
            oc = outcomes.get(int(s["id"])) or {}
            rows.append(
                {
                    "source": "A6",
                    "symbol": sym,
                    "side": direction,
                    "fill_timestamp": pd.Timestamp(s["entry_time"]),
                    "entry_price": float(s["entry_price"]),
                    "net_pnl_pct": oc.get("net_pnl_pct"),
                    "exit_reason": oc.get("exit_reason"),
                    "mfe_pct": oc.get("mfe_pct"),
                    "mae_pct": oc.get("mae_pct"),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["fill_timestamp"] = pd.to_datetime(df["fill_timestamp"], utc=True)
    return df


def load_stp_b2e1_fills(results_dir: Path | None = None) -> pd.DataFrame:
    root = results_dir or Path(STP_RESULTS_DIR)
    path = root / "signals.csv"
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "variant" in df.columns:
        df = df[df["variant"].astype(str).str.replace("×", "x").str.contains("B2") &
                df["variant"].astype(str).str.contains("E1")]
        # also accept B2xE1
        if df.empty:
            df = pd.read_csv(path)
            df = df[df["variant"].astype(str) == STP_VARIANT]
    if df.empty:
        return df
    df["source"] = "STP_B2xE1"
    df["side"] = "short"
    df["fill_timestamp"] = pd.to_datetime(df["fill_timestamp"], utc=True)
    return df


def overlap_table(lsr: pd.DataFrame, bench: pd.DataFrame, label: str) -> pd.DataFrame:
    rows = []
    if lsr.empty or bench.empty:
        return pd.DataFrame()
    for (variant, side), g in lsr.groupby(["variant", "side"]):
        b = bench.copy()
        if side in {"long", "short"}:
            b = b[b["side"] == side] if "side" in b.columns else b
        gt = pd.to_datetime(g["fill_timestamp"], utc=True)
        bt = pd.to_datetime(b["fill_timestamp"], utc=True)
        exact = near1 = near4 = 0
        for t in gt:
            deltas = (bt - t).abs().dt.total_seconds() / 60.0
            if (deltas == 0).any():
                exact += 1
            if (deltas <= 15).any():
                near1 += 1
            if (deltas <= 60).any():
                near4 += 1
        n = len(g)
        rows.append(
            {
                "benchmark": label,
                "variant": variant,
                "side": side,
                "n_lsr": n,
                "n_bench": len(b),
                "exact_fill_overlap": exact,
                "within_1_bar": near1,
                "within_4_bars": near4,
                "exact_rate": exact / n if n else None,
                "within_1_bar_rate": near1 / n if n else None,
                "sweep_only": n - exact,
            }
        )
    return pd.DataFrame(rows)


def apply_candidate_gates(
    summaries: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    signals: pd.DataFrame,
    overlap: pd.DataFrame,
) -> pd.DataFrame:
    """Evaluate Phase-16 gates per variant×side on primary exit X5 sequential."""
    ind = summaries.get("independent_summary", pd.DataFrame())
    seq = summaries.get("sequential_summary", pd.DataFrame())
    eq = summaries.get("summary_equal_coin", pd.DataFrame())
    med = summaries.get("summary_median_coin", pd.DataFrame())
    cw = summaries.get("summary_common_window", pd.DataFrame())
    wapt = summaries.get("summary_without_apt", pd.DataFrame())
    wtop3 = summaries.get("summary_without_top3", pd.DataFrame())
    by_coin = summaries.get("summary_by_coin", pd.DataFrame())
    by_split = summaries.get("summary_by_split", pd.DataFrame())
    by_month = summaries.get("summary_by_month", pd.DataFrame())

    rows = []
    variants = sorted(set(signals["variant"].tolist()) if not signals.empty else [])
    for variant in variants:
        for side in ("long", "short", "both"):
            def _get(df, **kw):
                if df is None or df.empty:
                    return None
                m = df
                for k, v in kw.items():
                    if k in m.columns:
                        m = m[m[k] == v]
                return m.iloc[0].to_dict() if len(m) else None

            s_x5 = _get(seq, variant=variant, side=side, exit_id="X5", mode="sequential")
            i_x5 = _get(ind, variant=variant, side=side, exit_id="X5", mode="independent")
            gates: dict[str, Any] = {
                "variant": variant,
                "side": side,
                "exit_id": "X5",
            }
            checks = {}
            e_seq = s_x5.get("expectation") if s_x5 else None
            pf_seq = s_x5.get("pf") if s_x5 else None
            checks["1_seq_expectation_positive"] = bool(e_seq is not None and e_seq > 0)
            checks["2_seq_pf_gt_1"] = bool(pf_seq is not None and pf_seq > 1)
            oos = _get(by_split, variant=variant, side=side, exit_id="X5", split="oos")
            val = _get(by_split, variant=variant, side=side, exit_id="X5", split="validation")
            oos_e = oos.get("expectation") if oos else None
            val_e = val.get("expectation") if val else None
            checks["3_oos_not_negative"] = bool(oos_e is not None and oos_e >= 0)
            checks["4_val_not_contradict_oos"] = not (
                oos_e is not None and val_e is not None and oos_e > 0 and val_e < -0.05
            )
            eq_r = _get(eq, variant=variant, side=side, exit_id="X5")
            med_r = _get(med, variant=variant, side=side, exit_id="X5")
            checks["5_equal_coin_positive"] = bool(
                eq_r and eq_r.get("equal_coin_expectation") is not None and eq_r["equal_coin_expectation"] > 0
            )
            checks["6_median_coin_not_negative"] = bool(
                med_r
                and med_r.get("median_coin_expectation") is not None
                and med_r["median_coin_expectation"] >= 0
            )
            cw_r = _get(cw, variant=variant, side=side, exit_id="X5")
            checks["7_common_window_positive"] = bool(
                cw_r and cw_r.get("expectation") is not None and cw_r["expectation"] > 0
            )
            wa = _get(wapt, variant=variant, side=side, exit_id="X5")
            checks["8_without_apt_positive"] = bool(
                wa and wa.get("expectation") is not None and wa["expectation"] > 0
            )
            wt = _get(wtop3, variant=variant, side=side, exit_id="X5")
            checks["9_without_top3_not_negative"] = bool(
                wt and wt.get("expectation") is not None and wt["expectation"] >= 0
            )
            pct_pos = eq_r.get("pct_positive_coins") if eq_r else None
            checks["10_pct_positive_coins_ge_60"] = bool(pct_pos is not None and pct_pos >= 0.60)
            n = s_x5.get("n") if s_x5 else 0
            checks["12_sufficient_n"] = bool(n is not None and n >= 30)
            # coin dominance
            coin_dom = False
            if not by_coin.empty:
                sub = by_coin[
                    (by_coin["variant"] == variant)
                    & (by_coin["side"] == side)
                    & (by_coin["exit_id"] == "X5")
                ]
                if len(sub) and sub["n"].sum() > 0:
                    coin_dom = float(sub["n"].max() / sub["n"].sum()) > 0.45
            checks["13_no_coin_dominance"] = not coin_dom
            month_dom = False
            if not by_month.empty:
                sub = by_month[
                    (by_month["variant"] == variant)
                    & (by_month["side"] == side)
                    & (by_month["exit_id"] == "X5")
                ]
                if len(sub) and sub["n"].sum() > 0:
                    month_dom = float(sub["n"].max() / sub["n"].sum()) > 0.40
            checks["14_no_month_dominance"] = not month_dom
            e_ind = i_x5.get("expectation") if i_x5 else None
            checks["15_ind_seq_same_sign"] = bool(
                e_ind is not None and e_seq is not None and (e_ind > 0) == (e_seq > 0)
            )
            same_bar = s_x5.get("same_bar_rate") if s_x5 else None
            checks["16_same_bar_not_excessive"] = bool(same_bar is None or same_bar < 0.35)
            te = s_x5.get("time_exit_rate") if s_x5 else None
            checks["17_not_only_time_exits"] = bool(te is None or te < 0.85)
            # cost 0.25 stress
            s_c = _get(seq, variant=variant, side=side, exit_id="X5_c025", mode="sequential")
            e_c = s_c.get("expectation") if s_c else None
            checks["18_cost025_stable"] = bool(
                e_seq is not None and e_c is not None and e_c > -0.05 and (e_c > 0 or e_seq <= 0)
            ) or bool(e_seq is not None and e_c is not None and abs(e_c - e_seq) < 0.15 and e_c >= 0)
            # overlap independence
            ov = None
            if overlap is not None and not overlap.empty:
                osub = overlap[(overlap["variant"] == variant) & (overlap["side"] == side)]
                if len(osub):
                    ov = float(osub["exact_rate"].max())
            checks["19_overlap_not_rename"] = bool(ov is None or ov < 0.50)
            checks["20_mfe_mae_plausible"] = bool(
                s_x5
                and s_x5.get("mean_mfe") is not None
                and s_x5.get("mean_mae") is not None
                and s_x5["mean_mfe"] > abs(float(s_x5["mean_mae"])) * 0.25
            )
            # sides evaluated separately is always true for long/short rows
            checks["11_sides_separate"] = side in {"long", "short", "both"}
            gates.update(checks)
            gates["n"] = n
            gates["seq_expectation"] = e_seq
            gates["seq_pf"] = pf_seq
            gates["all_pass"] = all(bool(v) for k, v in checks.items() if k != "11_sides_separate")
            # both-side candidate needs both long and short evaluated; don't auto-pass both
            if side == "both":
                gates["all_pass"] = False
            rows.append(gates)
    return pd.DataFrame(rows)


def pick_candidates(gate_df: pd.DataFrame) -> dict[str, Any]:
    if gate_df.empty:
        return {
            "best_long": None,
            "best_short": None,
            "best_overall": None,
            "track_verdict": "REJECT",
            "reason": "no_gate_rows",
        }
    passed = gate_df[gate_df["all_pass"] == True]  # noqa: E712
    best_long = None
    best_short = None
    if len(passed):
        longs = passed[passed["side"] == "long"].sort_values("seq_expectation", ascending=False)
        shorts = passed[passed["side"] == "short"].sort_values("seq_expectation", ascending=False)
        if len(longs):
            best_long = longs.iloc[0]["variant"]
        if len(shorts):
            best_short = shorts.iloc[0]["variant"]
    overall = best_long or best_short
    if best_long and best_short:
        # pick higher expectation
        le = float(passed[(passed["variant"] == best_long) & (passed["side"] == "long")].iloc[0]["seq_expectation"])
        se = float(
            passed[(passed["variant"] == best_short) & (passed["side"] == "short")].iloc[0]["seq_expectation"]
        )
        overall = best_long if le >= se else best_short
    return {
        "best_long": best_long,
        "best_short": best_short,
        "best_overall": overall,
        "track_verdict": "PASS" if overall else "REJECT",
        "n_passed": int(len(passed)),
    }
