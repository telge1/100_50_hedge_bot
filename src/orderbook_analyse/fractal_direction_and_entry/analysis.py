"""Orchestrate direction regime test + entry signal test (separate decisions)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_direction_and_entry import (
    AUDIT_VERSION,
    HORIZONS_MIN,
    MIN_SAMPLE,
    ROUNDTRIP_FEE_PCT,
    STATES,
    SYMBOL,
    TF_PREFIX,
)
from orderbook_analyse.fractal_direction_and_entry.entry import (
    EPISODE_DOC,
    dedupe_entry_episodes,
    flag_entries,
)
from orderbook_analyse.fractal_direction_and_entry.outcomes import (
    attach_forward_outcomes,
    signed_direction_metrics,
)
from orderbook_analyse.fractal_direction_and_entry.regime import (
    REGIME_RULES_DOC,
    classify_direction_state,
)
from orderbook_analyse.fractal_direction_and_entry.snapshots import (
    attach_tf_states,
    load_decision_grid_5m,
)


def _bullish_state(state: str) -> bool | None:
    if state in ("STRONG_BULL", "BULL"):
        return True
    if state in ("STRONG_BEAR", "BEAR"):
        return False
    return None


def summarize_states(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for state in STATES:
        sub = df[df["direction_state"] == state]
        bullish = _bullish_state(state)
        meta = {
            "state": state,
            "n": int(len(sub)),
            "small_sample": len(sub) < MIN_SAMPLE,
            "share": float(len(sub) / len(df)) if len(df) else None,
        }
        if bullish is None:
            # MIXED control: report unsigned raw stats only
            m = {"n": int(len(sub)), "small_sample": len(sub) < MIN_SAMPLE}
            for h in HORIZONS_MIN:
                r = sub[f"raw_ret_{h}m"].astype(float)
                m[f"median_raw_ret_{h}m"] = float(r.median()) if r.notna().any() else None
                m[f"mean_raw_ret_{h}m"] = float(r.mean()) if r.notna().any() else None
                m[f"hit_rate_raw_pos_{h}m"] = float((r > 0).mean()) if r.notna().any() else None
            rows.append({**meta, **m, "role": "control_mixed"})
            continue
        metrics = signed_direction_metrics(sub, bullish=bullish)
        rows.append({**meta, **metrics, "role": "directional"})
    return rows


def monthly_robustness(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    tmp = df.copy()
    tmp["month"] = tmp["decision_time"].dt.strftime("%Y-%m")
    for state in ("STRONG_BULL", "BULL", "BEAR", "STRONG_BEAR"):
        bullish = _bullish_state(state)
        assert bullish is not None
        for month, sub in tmp[tmp["direction_state"] == state].groupby("month"):
            m = signed_direction_metrics(sub, bullish=bullish)
            rows.append(
                {
                    "month": month,
                    "state": state,
                    "n": m["n"],
                    "small_sample": m["n"] < MIN_SAMPLE,
                    "hit_rate_60m": m.get("hit_rate_60m"),
                    "median_dir_ret_60m": m.get("median_dir_ret_60m"),
                    "hit_rate_120m": m.get("hit_rate_120m"),
                    "median_dir_ret_120m": m.get("median_dir_ret_120m"),
                }
            )
    return rows


def half_blocks(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    t = df["decision_time"]
    mid = t.min() + (t.max() - t.min()) / 2
    for label, mask in (("H1_first_half", t < mid), ("H2_second_half", t >= mid)):
        part = df[mask]
        for state in ("STRONG_BULL", "BULL", "BEAR", "STRONG_BEAR"):
            bullish = bool(_bullish_state(state))
            sub = part[part["direction_state"] == state]
            m = signed_direction_metrics(sub, bullish=bullish)
            rows.append({"block": label, "state": state, **m, "small_sample": m["n"] < MIN_SAMPLE})
    return rows


def decide_direction(
    state_rows: list[dict],
    monthly_rows: list[dict],
    block_rows: list[dict],
) -> str:
    """
    ROBUST: bullish and bearish sides show correct-sign median at 60m & hit>0.52,
            and >=60% of non-small months have hit_60m>0.5 on both combined sides.
    CONTEXT: overall edge but monthly unstable or only one side works.
    NOT_USEFUL: otherwise.
    """
    by = {r["state"]: r for r in state_rows if r.get("role") == "directional"}

    def side_ok(states: tuple[str, ...]) -> tuple[bool, bool]:
        """returns (has_edge, strong_edge)"""
        parts = [by[s] for s in states if s in by and by[s].get("n", 0) >= MIN_SAMPLE]
        if not parts:
            return False, False
        # n-weighted
        n = sum(p["n"] for p in parts)
        hit60 = sum((p.get("hit_rate_60m") or 0) * p["n"] for p in parts) / n
        med60 = sum((p.get("median_dir_ret_60m") or 0) * p["n"] for p in parts) / n
        hit120 = sum((p.get("hit_rate_120m") or 0) * p["n"] for p in parts) / n
        edge = hit60 > 0.50 and med60 > 0
        strong = hit60 >= 0.52 and med60 > 0 and hit120 > 0.50
        return edge, strong

    bull_edge, bull_strong = side_ok(("STRONG_BULL", "BULL"))
    bear_edge, bear_strong = side_ok(("STRONG_BEAR", "BEAR"))

    # monthly stability
    def month_stability(states: tuple[str, ...]) -> float | None:
        rel = [
            r
            for r in monthly_rows
            if r["state"] in states and not r.get("small_sample") and r.get("hit_rate_60m") is not None
        ]
        if len(rel) < 4:
            return None
        return float(np.mean([1.0 if (r["hit_rate_60m"] or 0) > 0.5 else 0.0 for r in rel]))

    bull_m = month_stability(("STRONG_BULL", "BULL"))
    bear_m = month_stability(("STRONG_BEAR", "BEAR"))

    # half-block consistency
    def block_ok(states: tuple[str, ...]) -> int:
        ok = 0
        for b in ("H1_first_half", "H2_second_half"):
            parts = [
                r
                for r in block_rows
                if r["block"] == b and r["state"] in states and r.get("n", 0) >= MIN_SAMPLE
            ]
            if not parts:
                continue
            n = sum(p["n"] for p in parts)
            hit = sum((p.get("hit_rate_60m") or 0) * p["n"] for p in parts) / n
            med = sum((p.get("median_dir_ret_60m") or 0) * p["n"] for p in parts) / n
            if hit > 0.5 and med > 0:
                ok += 1
        return ok

    bull_blocks = block_ok(("STRONG_BULL", "BULL"))
    bear_blocks = block_ok(("STRONG_BEAR", "BEAR"))

    if (
        bull_strong
        and bear_strong
        and (bull_m is not None and bull_m >= 0.6)
        and (bear_m is not None and bear_m >= 0.6)
        and bull_blocks == 2
        and bear_blocks == 2
    ):
        return "MTF_DIRECTIONAL_BIAS_ROBUST"
    if bull_edge or bear_edge:
        return "MTF_DIRECTIONAL_BIAS_CONTEXT_DEPENDENT"
    return "MTF_DIRECTIONAL_BIAS_NOT_USEFUL"


def entry_metrics(sub: pd.DataFrame, *, side: str) -> dict[str, Any]:
    bullish = side == "LONG"
    m = signed_direction_metrics(sub, bullish=bullish)
    return {"side": side, **m, "small_sample": m["n"] < MIN_SAMPLE}


def decide_entry(baseline_rows: list[dict]) -> str:
    """
    HAS_EDGE: LONG and SHORT entry beat regime baseline and cw-no-realign on hit60+med60.
    CONTEXT: edge on one side or only vs some baselines.
    NO_EDGE: otherwise.
    """
    by = {(r["side"], r["slice"]): r for r in baseline_rows}

    def beats(entry: dict, base: dict) -> bool:
        if entry.get("n", 0) < 15 or base.get("n", 0) < 15:
            return False
        return (entry.get("hit_rate_60m") or 0) > (base.get("hit_rate_60m") or 0) and (
            entry.get("median_dir_ret_60m") or 0
        ) > (base.get("median_dir_ret_60m") or 0)

    edges = []
    for side in ("LONG", "SHORT"):
        e = by.get((side, "entry"))
        if not e:
            continue
        b_reg = by.get((side, "regime_all"))
        b_cw = by.get((side, "cw_fail_no_realign"))
        b_ra = by.get((side, "realign_no_htf"))
        score = 0
        if b_reg and beats(e, b_reg):
            score += 1
        if b_cw and beats(e, b_cw):
            score += 1
        if b_ra and beats(e, b_ra):
            score += 1
        # fee-aware soft check
        fee_ok = (e.get("median_dir_ret_60m_net_fee") or -1) > 0
        edges.append({"side": side, "score": score, "fee_ok": fee_ok, "n": e.get("n", 0)})

    if not edges:
        return "FRACTAL_REALIGN_ENTRY_NO_EDGE"
    strong = [e for e in edges if e["score"] >= 2 and e["n"] >= MIN_SAMPLE]
    mild = [e for e in edges if e["score"] >= 1]
    if len(strong) == 2:
        return "FRACTAL_REALIGN_ENTRY_HAS_EDGE"
    if mild:
        return "FRACTAL_REALIGN_ENTRY_CONTEXT_DEPENDENT"
    return "FRACTAL_REALIGN_ENTRY_NO_EDGE"


def _snapshot_export_cols(df: pd.DataFrame) -> list[str]:
    cols = [
        "decision_time",
        "symbol",
        "close",
        "direction_state",
    ]
    for tf, pref in TF_PREFIX.items():
        for c in (
            "direction",
            "stoch_k_end",
            "stoch_zone_end",
            "stoch_state_end",
            "directional_efficiency",
            "signed_price_move_pct",
            "rsi_end",
            "rsi_delta",
            "rsi_end_gt_50",
            "price_vs_ema20_end",
            "ema9_vs_ema20_end",
            "cci_end",
            "end_available_at",
        ):
            name = f"{pref}_{c}"
            if name in df.columns:
                cols.append(name)
    for h in HORIZONS_MIN:
        for stem in ("raw_ret", "raw_mfe", "raw_mae"):
            name = f"{stem}_{h}m"
            if name in df.columns:
                cols.append(name)
    return cols


def run_analysis() -> dict[str, Any]:
    print("[grid] load 5m decision grid", flush=True)
    grid = load_decision_grid_5m()
    print(f"[grid] n={len(grid)}", flush=True)

    print("[join] multi-TF wave states", flush=True)
    df = attach_tf_states(grid)

    # Require 1D available for meaningful regime; drop early warmup without 1d
    d1 = f"{TF_PREFIX['1d']}_end_available_at"
    before = len(df)
    df = df[df[d1].notna()].copy()
    print(f"[warmup] drop no-1d: {before} -> {len(df)}", flush=True)

    # Need forward 240m = 48 bars
    max_hb = 48
    df = df.iloc[: len(df) - max_hb].copy() if len(df) > max_hb else df

    print("[regime] classify", flush=True)
    df["direction_state"] = classify_direction_state(df)

    print("[outcomes] forward returns", flush=True)
    df = attach_forward_outcomes(df)

    print("[summary] direction states", flush=True)
    state_rows = summarize_states(df)
    monthly_rows = monthly_robustness(df)
    block_rows = half_blocks(df)
    dir_decision = decide_direction(state_rows, monthly_rows, block_rows)

    # Forward returns flat table
    fwd_rows = []
    for r in state_rows:
        fwd_rows.append(r)

    print("[entry] flag + dedupe episodes", flush=True)
    df = flag_entries(df)
    df = dedupe_entry_episodes(df)

    entry_signal_rows = []
    baseline_rows = []

    long_e = df[df["long_entry"]]
    short_e = df[df["short_entry"]]
    entry_signal_rows.append(entry_metrics(long_e, side="LONG"))
    entry_signal_rows.append(entry_metrics(short_e, side="SHORT"))

    # Baselines (dedupe similarly for fair episode comparison where applicable)
    def first_per_episode(mask: pd.Series) -> pd.DataFrame:
        sub = df[mask.fillna(False)]
        if sub.empty:
            return sub
        return sub.groupby("episode_key_15m", sort=False).head(1)

    # regime_all = all regime snapshots (autocorrelated; baseline density)
    # cw / realign baselines episode-deduped like entries for overlap control
    baselines = {
        "LONG": {
            "entry": long_e,
            "regime_all": df[df["baseline_bull_regime"].fillna(False)],
            "cw_fail_no_realign": first_per_episode(df["baseline_bull_cw_no_realign"]),
            "realign_no_htf": first_per_episode(df["baseline_realign_up_no_htf"]),
        },
        "SHORT": {
            "entry": short_e,
            "regime_all": df[df["baseline_bear_regime"].fillna(False)],
            "cw_fail_no_realign": first_per_episode(df["baseline_bear_cw_no_realign"]),
            "realign_no_htf": first_per_episode(df["baseline_realign_down_no_htf"]),
        },
    }
    for side, parts in baselines.items():
        for name, sub in parts.items():
            row = entry_metrics(sub, side=side)
            row["slice"] = name
            baseline_rows.append(row)

    entry_decision = decide_entry(baseline_rows)

    # Compact entry signal log
    sig = df[df["long_entry"] | df["short_entry"]].copy()
    sig["side"] = np.where(sig["long_entry"], "LONG", "SHORT")
    sig_cols = [
        "decision_time",
        "side",
        "direction_state",
        "close",
        "episode_key_15m",
        f"{TF_PREFIX['15m']}_direction",
        f"{TF_PREFIX['15m']}_signed_price_move_pct",
        f"{TF_PREFIX['15m']}_directional_efficiency",
        f"{TF_PREFIX['5m']}_direction",
        f"{TF_PREFIX['1m']}_direction",
    ]
    for h in HORIZONS_MIN:
        sig_cols.append(f"raw_ret_{h}m")
        sig_cols.append(f"raw_ret_{h}m_net_fee")
    sig_cols = [c for c in sig_cols if c in sig.columns]
    entry_signals = sig[sig_cols].copy()

    snap_cols = _snapshot_export_cols(df)
    # Add entry flags to snapshots
    for c in ("long_entry", "short_entry", "cw_fail_long", "cw_fail_short", "realign_up", "realign_down"):
        if c in df.columns:
            snap_cols.append(c)

    print("[export prep] snapshots", flush=True)
    snapshots = df[snap_cols].copy()

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "n_snapshots": int(len(df)),
        "n_long_entries": int(df["long_entry"].sum()),
        "n_short_entries": int(df["short_entry"].sum()),
        "direction_state_snapshots": snapshots,
        "direction_state_summary": state_rows,
        "direction_forward_returns": fwd_rows,
        "direction_monthly_robustness": monthly_rows,
        "direction_half_blocks": block_rows,
        "entry_signals": entry_signals,
        "entry_signal_summary": entry_signal_rows,
        "entry_baseline_comparison": baseline_rows,
        "decisions": {
            "direction": dir_decision,
            "entry": entry_decision,
        },
        "method": {
            "grid": "every closed APTUSDT 5m candle; decision_time=close/available_at",
            "causality": "wave end_available_at <= decision_time",
            "regime_rules": REGIME_RULES_DOC.strip(),
            "episode_dedupe": EPISODE_DOC.strip(),
            "fee_reference_pct": ROUNDTRIP_FEE_PCT,
            "cci_role": "carried only; not used in regime or entry",
            "no_threshold_search": True,
            "no_protected_levels": True,
        },
    }
