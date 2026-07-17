"""Audit runner for C3.5 pullback entry state machine (research-only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import load_ohlcv_with_warmup
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.pullback_entry_c3_5 import (
    ABLATION_BASE,
    ARMING_TYPES,
    RESEARCH_VARIANTS,
    PullbackEntryConfig,
    ablation_configs,
    apply_pullback_entry,
    compute_entry_outcomes,
    config_hash,
    prepare_research_frame,
)
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_5_pullback_entry_state_machine")
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)


def _variant_summary(outcomes: list[dict[str, Any]], *, n_armed: int, n_pullback: int, n_ready: int, n_invalid: int) -> dict[str, Any]:
    if not outcomes:
        return {
            "n_entries": 0,
            "n_armed": n_armed,
            "n_pullback": n_pullback,
            "n_ready": n_ready,
            "n_invalidated": n_invalid,
            "fake_rate": None,
            "mean_mfe": None,
            "mean_mae": None,
            "median_mfe_mae": None,
            "mean_fwd_10": None,
            "profit_factor_proxy": None,
        }
    mfe = [float(o["mfe"]) for o in outcomes if o.get("mfe") is not None]
    mae = [float(o["mae"]) for o in outcomes if o.get("mae") is not None]
    ratios = [float(o["mfe_mae_ratio"]) for o in outcomes if o.get("mfe_mae_ratio") not in (None, float("inf"))]
    fwd10 = [float(o["fwd_ret_10"]) for o in outcomes if o.get("fwd_ret_10") is not None]
    fake = [bool(o.get("is_fake")) for o in outcomes]
    wins = [x for x in fwd10 if x > 0]
    losses = [x for x in fwd10 if x <= 0]
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None
    return {
        "n_entries": len(outcomes),
        "n_armed": n_armed,
        "n_pullback": n_pullback,
        "n_ready": n_ready,
        "n_invalidated": n_invalid,
        "fake_rate": float(np.mean(fake)) if fake else None,
        "mean_mfe": float(np.mean(mfe)) if mfe else None,
        "mean_mae": float(np.mean(mae)) if mae else None,
        "median_mfe_mae": float(np.median(ratios)) if ratios else None,
        "mean_fwd_10": float(np.mean(fwd10)) if fwd10 else None,
        "profit_factor_proxy": float(pf) if pf is not None else None,
        "late_rate": float(np.mean([bool(o.get("is_late")) for o in outcomes])) if outcomes else None,
    }


def _count_timeline_events(timeline: pd.DataFrame) -> dict[str, int]:
    ev = timeline["events"].fillna("").astype(str)
    return {
        "n_armed": int(ev.str.contains("short_armed|long_armed", regex=True).sum()),
        "n_pullback": int(ev.str.contains("short_pullback|long_pullback", regex=True).sum()),
        "n_ready": int(ev.str.contains("short_ready|long_ready", regex=True).sum()),
        "n_invalidated": int(ev.str.contains("invalidated:", regex=False).sum()),
        "n_entered": int(ev.str.contains("_entered", regex=False).sum()),
    }


def run_variant(
    frame: pd.DataFrame,
    cfg: PullbackEntryConfig,
) -> dict[str, Any]:
    timeline, entries = apply_pullback_entry(frame, cfg)
    outcomes = compute_entry_outcomes(frame, entries, fee_bps_per_side=cfg.fee_bps_per_side)
    counts = _count_timeline_events(timeline)
    summary = _variant_summary(
        outcomes,
        n_armed=counts["n_armed"],
        n_pullback=counts["n_pullback"],
        n_ready=counts["n_ready"],
        n_invalid=counts["n_invalidated"],
    )
    return {
        "config": cfg.to_dict(),
        "config_hash": config_hash(cfg),
        "timeline": timeline,
        "entries": entries,
        "outcomes": outcomes,
        "counts": counts,
        "summary": summary,
    }


def compare_arming_types(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for arm in ARMING_TYPES:
        cfg = PullbackEntryConfig(name=f"arm_{arm}", label=arm, arming_type=arm)
        res = run_variant(frame, cfg)
        rows.append({"arming_type": arm, **res["summary"], **res["counts"]})
    return pd.DataFrame(rows)


def rank_entries(outcomes: list[dict[str, Any]]) -> dict[str, pd.DataFrame]:
    if not outcomes:
        empty = pd.DataFrame()
        return {
            "top20_good": empty,
            "top20_fake": empty,
            "top20_late": empty,
            "top20_missed_proxy": empty,
        }
    df = pd.DataFrame(outcomes)
    good = df.sort_values(["mfe_mae_ratio", "fwd_ret_10"], ascending=False).head(20)
    fake = df[df["is_fake"] == True].sort_values("mae").head(20)  # noqa: E712
    late = df[df["is_late"] == True].sort_values("move_since_arm_atr", ascending=False).head(20)
    # Missed proxy: high MFE potential not used — use armed-only isn't in outcomes;
    # approximate as entries with good fwd but classified late.
    missed = df[(df["is_late"] == True) & (df["fwd_ret_10"].fillna(0) > 0)].head(20)
    return {
        "top20_good": good,
        "top20_fake": fake,
        "top20_late": late,
        "top20_missed_proxy": missed,
    }


def build_event_trace_for_setup(
    frame: pd.DataFrame,
    timeline: pd.DataFrame,
    outcomes: list[dict[str, Any]],
    *,
    prefer_side: int = -1,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Pick a representative short setup and build event/state/variant decision traces."""
    # Find first short entry with full pullback path if possible.
    short_entries = [o for o in outcomes if int(o.get("side") or 0) == prefer_side]
    if not short_entries:
        short_entries = list(outcomes)
    if not short_entries:
        # Fall back: first armed event
        armed = timeline[timeline["events"].fillna("").str.contains("short_armed")]
        if armed.empty:
            return pd.DataFrame(), timeline.head(0), pd.DataFrame()
        start_i = int(armed.iloc[0]["bar_index"])
        end_i = min(start_i + 80, len(frame) - 1)
        focus_entry = None
    else:
        focus_entry = short_entries[0]
        start_i = max(0, int(focus_entry["bar_index"]) - 40)
        end_i = min(len(frame) - 1, int(focus_entry["bar_index"]) + 40)

    window = frame.iloc[start_i : end_i + 1].copy()
    tl = timeline[(timeline["bar_index"] >= start_i) & (timeline["bar_index"] <= end_i)].copy()
    events = []
    for _, r in tl.iterrows():
        if not r.get("events"):
            continue
        i = int(r["bar_index"])
        fr = frame.iloc[i]
        events.append(
            {
                "timestamp": r["timestamp"],
                "bar_index": i,
                "events": r["events"],
                "entry_state": r["entry_state"],
                "armed_price": r.get("armed_price"),
                "pullback_high": r.get("pullback_high"),
                "pullback_low": r.get("pullback_low"),
                "breakout_level": r.get("breakout_level"),
                "rejection_bar": r.get("rejection_bar"),
                "entry_price": r.get("entry_price"),
                "entry_reason": r.get("entry_reason"),
                "invalidation_reason": r.get("invalidation_reason"),
                "ema_9": fr.get("ema_9"),
                "ema_20": fr.get("ema_20"),
                "ema_50": fr.get("ema_50"),
                "ema_9_slope_3": fr.get("ema_9_slope_3"),
                "ema_20_slope_3": fr.get("ema_20_slope_3"),
                "adx": fr.get("adx"),
                "plus_di": fr.get("plus_di"),
                "minus_di": fr.get("minus_di"),
                "atr_14": fr.get("atr_14"),
                "m15_major_direction": fr.get("m15_major_direction"),
                "m30_major_direction": fr.get("m30_major_direction"),
                "close": fr.get("close"),
                "high": fr.get("high"),
                "low": fr.get("low"),
            }
        )
    event_df = pd.DataFrame(events)

    # Variant decisions on the focus entry bar (or armed bar).
    focus_bar = int(focus_entry["bar_index"]) if focus_entry else start_i
    decisions = []
    for cfg in RESEARCH_VARIANTS:
        res = run_variant(frame, cfg)
        # Did this variant enter near the focus window?
        near = [
            o
            for o in res["outcomes"]
            if abs(int(o["bar_index"]) - focus_bar) <= 12 and int(o.get("side") or 0) == prefer_side
        ]
        if near:
            o = near[0]
            decisions.append(
                {
                    "variant": cfg.name,
                    "decision": "entry",
                    "entry_bar": o["bar_index"],
                    "entry_reason": o.get("entry_reason"),
                    "reject_reason": None,
                    "mfe": o.get("mfe"),
                    "mae": o.get("mae"),
                    "fwd_ret_10": o.get("fwd_ret_10"),
                    "is_fake": o.get("is_fake"),
                }
            )
        else:
            # Check if armed but invalidated near window
            tl2 = res["timeline"]
            near_tl = tl2[(tl2["bar_index"] >= focus_bar - 40) & (tl2["bar_index"] <= focus_bar + 12)]
            inv = near_tl[near_tl["events"].fillna("").str.contains("invalidated:|break_rejected:")]
            reject = None
            if not inv.empty:
                reject = str(inv.iloc[-1]["events"])
            elif near_tl["events"].fillna("").str.contains("short_armed|long_armed").any():
                reject = "armed_but_no_entry_in_window"
            else:
                reject = "no_setup"
            decisions.append(
                {
                    "variant": cfg.name,
                    "decision": "reject",
                    "entry_bar": None,
                    "entry_reason": None,
                    "reject_reason": reject,
                    "mfe": None,
                    "mae": None,
                    "fwd_ret_10": None,
                    "is_fake": None,
                }
            )
    return event_df, tl, pd.DataFrame(decisions)


def run_pullback_entry_audit(
    *,
    symbol: str = "APTUSDT",
    analyze_start: str = "2026-02-01",
    analyze_end: str = "2026-04-30",
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    variants: Sequence[PullbackEntryConfig] | None = None,
    include_mtf: bool = True,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    t0 = time.perf_counter()
    full_5m, _ = load_ohlcv_with_warmup(
        symbol, "5m", analyze_start=analyze_start, analyze_end=analyze_end
    )
    ohlcv_15m = ohlcv_30m = None
    if include_mtf:
        full_15m, _ = load_ohlcv_with_warmup(
            symbol, "15m", analyze_start=analyze_start, analyze_end=analyze_end
        )
        full_30m, _ = load_ohlcv_with_warmup(
            symbol, "30m", analyze_start=analyze_start, analyze_end=analyze_end
        )
        ohlcv_15m, ohlcv_30m = full_15m, full_30m

    frame = prepare_research_frame(full_5m, ohlcv_15m=ohlcv_15m, ohlcv_30m=ohlcv_30m)
    a0 = pd.Timestamp(analyze_start, tz="UTC")
    a1 = pd.Timestamp(analyze_end, tz="UTC")
    ts = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.loc[(ts >= a0) & (ts < a1)].copy().reset_index(drop=True)
    frame["bar_index"] = np.arange(len(frame))
    frame["symbol"] = symbol
    frame["timeframe"] = "5m"

    frame.to_csv(output_dir / "research_frame_5m.csv", index=False)

    arming_cmp = compare_arming_types(frame)
    arming_cmp.to_csv(output_dir / "arming_type_comparison.csv", index=False)

    variant_list = list(variants or RESEARCH_VARIANTS)
    comparison_rows = []
    primary = None
    all_outcomes: dict[str, list[dict[str, Any]]] = {}

    for cfg in variant_list:
        res = run_variant(frame, cfg)
        suffix = cfg.name
        res["timeline"].to_csv(output_dir / f"state_timeline_{suffix}.csv", index=False)
        pd.DataFrame(res["outcomes"]).to_csv(output_dir / f"entries_outcomes_{suffix}.csv", index=False)
        comparison_rows.append({"variant": cfg.name, "label": cfg.label, **res["summary"], **res["counts"]})
        all_outcomes[cfg.name] = res["outcomes"]
        if cfg.name == "A6" or primary is None:
            primary = res
            primary_name = cfg.name

    assert primary is not None
    comparison = pd.DataFrame(comparison_rows)
    comparison.to_csv(output_dir / "variant_comparison.csv", index=False)

    # Primary exports
    primary["timeline"].to_csv(output_dir / "state_timeline.csv", index=False)
    pd.DataFrame(primary["outcomes"]).to_csv(output_dir / "entries_outcomes.csv", index=False)

    ranks = rank_entries(primary["outcomes"])
    for name, df in ranks.items():
        df.to_csv(output_dir / f"{name}.csv", index=False)

    # Event trace for concrete APT situation (first short A6 entry window)
    event_df, state_win, decisions = build_event_trace_for_setup(
        frame, primary["timeline"], primary["outcomes"], prefer_side=-1
    )
    event_df.to_csv(output_dir / "event_trace.csv", index=False)
    state_win.to_csv(output_dir / "state_timeline_focus.csv", index=False)
    decisions.to_csv(output_dir / "variant_decisions.csv", index=False)

    # Ablation on A6
    abl_rows = []
    for cfg in ablation_configs(ABLATION_BASE):
        res = run_variant(frame, cfg)
        abl_rows.append({"variant": cfg.name, **res["summary"], **res["counts"]})
        pd.DataFrame(res["outcomes"]).to_csv(output_dir / f"ablation_outcomes_{cfg.name}.csv", index=False)
    abl = pd.DataFrame(abl_rows)
    abl.to_csv(output_dir / "ablation_comparison.csv", index=False)

    # Feature usefulness vs fake (simple correlations on primary)
    fake_feature_notes = []
    odf = pd.DataFrame(primary["outcomes"])
    if not odf.empty and "is_fake" in odf.columns:
        for col in ["entry_dist_ema_atr", "move_since_arm_atr", "pullback_depth_atr", "setup_age_at_entry", "adx"]:
            if col in odf.columns:
                sub = odf[[col, "is_fake"]].dropna()
                if len(sub) >= 5:
                    fake_mean = float(sub.loc[sub["is_fake"] == True, col].mean()) if (sub["is_fake"] == True).any() else None  # noqa: E712
                    good_mean = float(sub.loc[sub["is_fake"] == False, col].mean()) if (sub["is_fake"] == False).any() else None  # noqa: E712
                    fake_feature_notes.append(
                        {"feature": col, "mean_fake": fake_mean, "mean_good": good_mean}
                    )
    pd.DataFrame(fake_feature_notes).to_csv(output_dir / "fake_feature_separability.csv", index=False)

    # Monthly stability for primary
    if not odf.empty and "timestamp" in odf.columns:
        odf = odf.copy()
        odf["month"] = pd.to_datetime(odf["timestamp"], utc=True).dt.to_period("M").astype(str)
        monthly = (
            odf.groupby("month")
            .agg(
                n_entries=("bar_index", "count"),
                mean_fwd_10=("fwd_ret_10", "mean"),
                fake_rate=("is_fake", "mean"),
                mean_mfe=("mfe", "mean"),
                mean_mae=("mae", "mean"),
            )
            .reset_index()
        )
        monthly.to_csv(output_dir / "monthly_stability_primary.csv", index=False)
    else:
        monthly = pd.DataFrame()

    # Pick "best robust" research label (not production recommendation)
    scored = comparison.copy()
    if not scored.empty and scored["n_entries"].max() > 0:
        scored["score"] = (
            scored["median_mfe_mae"].fillna(0) * 0.4
            + scored["mean_fwd_10"].fillna(0) * 50
            - scored["fake_rate"].fillna(1) * 0.4
            + np.log1p(scored["n_entries"].fillna(0)) * 0.05
        )
        best = scored.sort_values("score", ascending=False).iloc[0].to_dict()
    else:
        best = {"variant": primary_name}

    summary = {
        "phase": "C3_5_pullback_entry_state_machine",
        "symbol": symbol,
        "timeframe": "5m",
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "n_bars": len(frame),
        "baseline_reference_hash": C2_BASELINE_HASH,
        "baseline_hash_confirmed": bool(baseline.get("hash_matches")),
        "include_mtf": include_mtf,
        "variants": comparison_rows,
        "arming_comparison": arming_cmp.to_dict(orient="records"),
        "ablation": abl_rows,
        "primary_variant": primary_name,
        "primary_summary": primary["summary"],
        "research_best_variant_label": best.get("variant"),
        "research_best_variant_note": "Research ranking only — not a production rule; needs OOS confirmation.",
        "fake_feature_notes": fake_feature_notes,
        "focus_event_trace_rows": int(len(event_df)),
        "safety": {
            "research_only": True,
            "no_live_bot_integration": True,
            "no_c34b_modifications": True,
            "nothing_committed": True,
            "causal_closed_bars_only": True,
        },
        "runtime_s": round(time.perf_counter() - t0, 4),
        "artifacts": {
            "variant_comparison": "variant_comparison.csv",
            "arming_comparison": "arming_type_comparison.csv",
            "state_timeline": "state_timeline.csv",
            "entries_outcomes": "entries_outcomes.csv",
            "event_trace": "event_trace.csv",
            "variant_decisions": "variant_decisions.csv",
            "ablation": "ablation_comparison.csv",
        },
    }
    (output_dir / "run_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "REPORT.md").write_text(_render_report(summary, comparison, abl, arming_cmp), encoding="utf-8")
    return summary


def _render_report(
    summary: dict[str, Any],
    comparison: pd.DataFrame,
    abl: pd.DataFrame,
    arming: pd.DataFrame,
) -> str:
    lines = [
        "# C3.5 Pullback Entry State Machine — Research Report",
        "",
        f"Symbol: `{summary['symbol']}` 5m  |  Window: {summary['analyze_start']} → {summary['analyze_end']}",
        f"Bars: {summary['n_bars']}  |  Runtime: {summary['runtime_s']}s",
        "",
        "## Safety",
        "- Research only; no live-bot changes; C3.4B untouched; nothing committed.",
        "- Causal closed-bar decisions; HTF via merge_asof on bar close.",
        "",
        "## State machine",
        "IDLE → *_ARMED → *_PULLBACK → *_READY → *_ENTERED (short/long families).",
        "A0 = direct structure entry reference; A1–A10 add pullback/filters/MTF.",
        "",
        "## Variant comparison",
        comparison.to_string(index=False) if not comparison.empty else "(empty)",
        "",
        "## Arming types",
        arming.to_string(index=False) if not arming.empty else "(empty)",
        "",
        "## Ablation (A6)",
        abl.to_string(index=False) if not abl.empty else "(empty)",
        "",
        f"## Research best label (not production): `{summary.get('research_best_variant_label')}`",
        summary.get("research_best_variant_note", ""),
        "",
        "## Limits",
        "- Single-symbol in-sample window; no OOS claim.",
        "- Structure edges from C3.4B on 5m inherit C3.4B sensitivity.",
        "- Fees/slippage are research assumptions.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="C3.5 pullback entry audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--analyze-start", default="2026-02-01")
    p.add_argument("--analyze-end", default="2026-04-30")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    p.add_argument("--no-mtf", action="store_true")
    args = p.parse_args(argv)
    summary = run_pullback_entry_audit(
        symbol=args.symbol,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        include_mtf=not args.no_mtf,
    )
    print(json.dumps(json_safe({"primary": summary["primary_summary"], "best": summary["research_best_variant_label"]}), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
