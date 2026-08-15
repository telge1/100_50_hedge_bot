"""Frame loading and outcome/audit helpers for short_trend_pullback_v1."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.c35c_signal_store.build import (
    evaluate_outcome_on_fill,
    load_symbol_5m_mysql,
    resolve_analyze_window,
    sha1_ohlcv,
)
from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.indicator_feature_store import required_indicator_warmup_bars
from research.regime_scanner.pullback_entry_c3_5 import prepare_research_frame
from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import aggregate_complete_from_5m
from research.regime_scanner.pullback_entry_c3_5c_fill_excursion_audit import (
    first_touch_level,
    path_arrays,
    signed_return_pct,
)
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    assign_split,
    fixed_chrono_splits,
)
from research.regime_scanner.short_trend_pullback.config import (
    A6_PARENT_LABEL,
    FIRST_TOUCH_LEVELS,
    MFE_HORIZONS,
    STPConfig,
    default_config,
    variant_id,
)
from research.regime_scanner.short_trend_pullback.strategy import run_strategy_on_frame


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
    fts = pd.to_datetime(frame["timestamp"], utc=True)
    # keep warmup bars in frame for indicators/structure continuity, but strategy starts at a0
    meta = {
        **mysql_meta,
        "analyze_start": str(a0),
        "analyze_end_exclusive": str(a1),
        "n_15m": int(len(frame)),
        "ohlcv_sha1_5m": mysql_meta.get("ohlcv_sha1"),
        "warmup_5m_bars": warm_bars,
    }
    return frame.reset_index(drop=True), meta, a0, a1


def forward_outcomes_for_signal(
    frame: pd.DataFrame,
    *,
    fill_i: int,
    entry: float,
) -> dict[str, Any]:
    side = -1  # short
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    atr = frame["atr_14"].to_numpy(dtype=float) if "atr_14" in frame.columns else frame.get("atr", pd.Series(np.nan, index=frame.index)).to_numpy(dtype=float)
    n = len(frame)
    out: dict[str, Any] = {}
    for h in MFE_HORIZONS:
        end = min(n - 1, fill_i + int(h) - 1)
        if end < fill_i:
            out[f"mfe_pct_h{h}"] = None
            out[f"mae_pct_h{h}"] = None
            out[f"dir_close_h{h}"] = None
            continue
        path = path_arrays(side, entry, highs, lows, closes, fill_i, end)
        out[f"mfe_pct_h{h}"] = path.get("maximum_favorable_excursion_pct")
        out[f"mae_pct_h{h}"] = path.get("maximum_adverse_excursion_pct")
        out[f"dir_close_h{h}"] = path.get("close_return_pct")
        atr0 = float(atr[fill_i]) if fill_i < len(atr) and np.isfinite(atr[fill_i]) and atr[fill_i] > 0 else None
        if atr0 and entry:
            out[f"mfe_atr_h{h}"] = (out[f"mfe_pct_h{h}"] / 100.0 * entry) / atr0 if out[f"mfe_pct_h{h}"] is not None else None
            out[f"mae_atr_h{h}"] = (out[f"mae_pct_h{h}"] / 100.0 * entry) / atr0 if out[f"mae_pct_h{h}"] is not None else None
        else:
            out[f"mfe_atr_h{h}"] = None
            out[f"mae_atr_h{h}"] = None
    for lvl in FIRST_TOUCH_LEVELS:
        end = min(n - 1, fill_i + 192 - 1)
        ft = first_touch_level(side, entry, highs, lows, fill_i, end, float(lvl))
        key = f"ft_{'p' if lvl > 0 else 'm'}{abs(lvl):.2f}".replace(".", "_")
        out[f"{key}_reached"] = bool(ft.get("reached"))
        out[f"{key}_bars"] = ft.get("bar_offset")
    # same-bar ambiguity at +/- levels closest to TP/SL
    tp = first_touch_level(side, entry, highs, lows, fill_i, min(n - 1, fill_i), 3.0)
    sl = first_touch_level(side, entry, highs, lows, fill_i, min(n - 1, fill_i), -2.0)
    out["same_bar_ambiguous_fill"] = bool(tp.get("reached") and sl.get("reached"))
    return out


def tp3_sl2_outcome(frame: pd.DataFrame, fill_i: int, entry: float) -> dict[str, Any]:
    highs = frame["high"].to_numpy(dtype=float)
    lows = frame["low"].to_numpy(dtype=float)
    closes = frame["close"].to_numpy(dtype=float)
    timestamps = list(pd.to_datetime(frame["timestamp"], utc=True))
    return evaluate_outcome_on_fill(
        side=-1,
        entry=entry,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=timestamps,
        fill_i=fill_i,
        n_bars=len(frame),
    )


def load_a6_short_fills(store: C35cPathStore, parent_label: str = A6_PARENT_LABEL) -> pd.DataFrame:
    children = store.find_child_runs(parent_label)
    rows = []
    for run in children:
        rid = str(run["run_id"])
        sym = str(run.get("symbol") or "").upper()
        sigs, outcomes, _, _ = store.load_signals_bundle(rid, outcome_version="tp3_sl2_h192_cost020_v1")
        for s in sigs:
            if str(s.get("direction") or "").lower() != "short":
                continue
            oc = outcomes.get(int(s["id"])) or {}
            meta = s.get("metadata_json") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            rows.append(
                {
                    "symbol": sym,
                    "fill_time": pd.Timestamp(s["entry_time"]),
                    "entry_price": float(s["entry_price"]),
                    "net_pnl_pct": oc.get("net_pnl_pct"),
                    "exit_reason": oc.get("exit_reason"),
                    "mfe_pct": oc.get("mfe_pct"),
                    "mae_pct": oc.get("mae_pct"),
                    "split": (meta.get("split") if isinstance(meta, dict) else None),
                    "signal_key": s.get("signal_key"),
                }
            )
    df = pd.DataFrame(rows)
    if not df.empty:
        df["fill_time"] = pd.to_datetime(df["fill_time"], utc=True)
    return df


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
        }
    wins = pnls[pnls > 0].sum()
    losses = -pnls[pnls < 0].sum()
    pf = None if losses < 1e-15 else float(wins / losses)
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = float((eq - peak).min())
    streak = 0
    best = 0
    for x in pnls:
        if x < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return {
        "n": int(len(pnls)),
        "expectation": float(np.mean(pnls)),
        "pf": pf,
        "sum_pnl": float(np.sum(pnls)),
        "winrate": float(np.mean(pnls > 0)),
        "max_dd": dd,
        "max_losing_streak": int(best),
    }


def collect_variant_signals(
    symbol: str,
    frame: pd.DataFrame,
    a0: pd.Timestamp,
    a1: pd.Timestamp,
    *,
    contexts: tuple[str, ...],
    triggers: tuple[str, ...],
    cfg: STPConfig,
) -> list[dict[str, Any]]:
    splits = fixed_chrono_splits(a0, a1)
    fts = pd.to_datetime(frame["timestamp"], utc=True)
    out_rows: list[dict[str, Any]] = []
    for ctx in contexts:
        for trig in triggers:
            events = run_strategy_on_frame(
                frame, symbol=symbol, context=ctx, trigger=trig, cfg=cfg, analyze_start=a0
            )
            for ev in events:
                # filter fill inside analyze window
                fill_ts = pd.Timestamp(ev.fill_timestamp)
                if fill_ts.tzinfo is None:
                    fill_ts = fill_ts.tz_localize("UTC")
                else:
                    fill_ts = fill_ts.tz_convert("UTC")
                if fill_ts < a0 or fill_ts >= a1:
                    continue
                fwd = forward_outcomes_for_signal(frame, fill_i=ev.fill_bar, entry=ev.entry_price)
                bench = tp3_sl2_outcome(frame, ev.fill_bar, ev.entry_price)
                split_raw = assign_split(fill_ts, splits)
                split = {"development": "dev", "validation": "validation", "oos": "oos"}.get(
                    split_raw, split_raw
                )
                out_rows.append(
                    {
                        "symbol": symbol,
                        "variant": ev.variant,
                        "context": ctx,
                        "trigger": trig,
                        "side": "short",
                        "trigger_timestamp": str(ev.trigger_timestamp),
                        "fill_timestamp": str(fill_ts),
                        "entry_price": ev.entry_price,
                        "trigger_price": ev.trigger_price,
                        "pullback_high": ev.pullback_high,
                        "protected_high": ev.protected_high,
                        "pullback_retracement": ev.pullback_retracement,
                        "impulse_strength": ev.impulse_strength,
                        "distance_to_protected_high": ev.distance_to_protected_high,
                        "split": split,
                        "fill_bar": ev.fill_bar,
                        "trigger_bar": ev.trigger_bar,
                        **fwd,
                        "net_pnl_pct": bench.get("net_pnl_pct"),
                        "gross_pnl_pct": bench.get("gross_pnl_pct"),
                        "exit_reason": bench.get("exit_reason"),
                        "bars_held": bench.get("bars_held"),
                        "mfe_pct": bench.get("mfe_pct"),
                        "mae_pct": bench.get("mae_pct"),
                        "is_winner": bench.get("is_winner"),
                        **{f"feat_{k}": v for k, v in ev.features.items()},
                    }
                )
    return out_rows
