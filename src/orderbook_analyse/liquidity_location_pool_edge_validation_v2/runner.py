"""Phase-2 edge validation runner (read-only research)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import ANALYSIS_ID, ANALYSIS_VERSION, VERDICT_COMPLETE, VERDICT_NO_STABLE
from .approach import enrich_approach
from .candidates import evaluate_candidates
from .entity_table import DEFAULT_V1, build_entity_table
from .episodes import assign_episodes
from .stats import block_bootstrap_rate, summarize_group

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/liquidity_location_pool_edge_validation_v2"
)


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite {path}")
    df.to_csv(path, index=False)


def assign_temporal_splits(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Chronological 60/20/20 by known_at within full sample (fixed before rule eval)."""
    out = df.sort_values("known_at_ts").reset_index(drop=True).copy()
    n = len(out)
    i60 = int(n * 0.60)
    i80 = int(n * 0.80)
    # ensure boundaries fall on distinct timestamps: expand to all rows with same ts
    t60 = out.iloc[i60]["known_at_ts"]
    t80 = out.iloc[i80]["known_at_ts"]
    # discovery: known_at < t60 boundary start of validation
    # Find first index where known_at >= t60 after i60 stretch
    while i60 > 0 and out.iloc[i60 - 1]["known_at_ts"] == t60:
        i60 -= 1
    while i80 < n - 1 and out.iloc[i80]["known_at_ts"] == t80:
        i80 += 1
    # simpler: use quantile timestamps
    q60 = out["known_at_ts"].quantile(0.60)
    q80 = out["known_at_ts"].quantile(0.80)
    out["temporal_split"] = np.where(
        out["known_at_ts"] <= q60,
        "discovery",
        np.where(out["known_at_ts"] <= q80, "validation", "oos"),
    )
    # force non-overlap by assigning equal-ts at boundary to earlier split only already handled by <=
    splits = {
        "method": "chronological_known_at_quantiles_60_20_20",
        "n_total": int(n),
        "discovery_end_known_at": str(q60),
        "validation_end_known_at": str(q80),
        "n_discovery": int((out["temporal_split"] == "discovery").sum()),
        "n_validation": int((out["temporal_split"] == "validation").sum()),
        "n_oos": int((out["temporal_split"] == "oos").sum()),
        "fixed_before_rule_selection": True,
        "symbols": sorted(out["symbol"].unique().tolist()),
        "timeframes": sorted(out["timeframe"].unique().tolist()),
    }
    # assert no overlap of time ranges incorrectly — validation starts after discovery max
    dmax = out.loc[out["temporal_split"] == "discovery", "known_at_ts"].max()
    vmin = out.loc[out["temporal_split"] == "validation", "known_at_ts"].min()
    vmax = out.loc[out["temporal_split"] == "validation", "known_at_ts"].max()
    omin = out.loc[out["temporal_split"] == "oos", "known_at_ts"].min()
    splits["discovery_max"] = str(dmax)
    splits["validation_min"] = str(vmin)
    splits["validation_max"] = str(vmax)
    splits["oos_min"] = str(omin)
    splits["nonoverlap_ok"] = bool(
        (pd.isna(vmin) or dmax <= vmin) and (pd.isna(omin) or vmax <= omin)
    )
    return out, splits


def _cohort_table(df: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for vals, g in df.groupby(keys, dropna=False):
        if not isinstance(vals, tuple):
            vals = (vals,)
        rows.append(summarize_group(g, group_cols=dict(zip(keys, vals))))
    return pd.DataFrame(rows)


def run_v2(
    *,
    v1_dir: Path = DEFAULT_V1,
    out_dir: Path = DEFAULT_OUT,
    skip_approach: bool = False,
    n_boot: int = 300,
) -> dict[str, Any]:
    t0 = time.time()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("BUILD_ENTITY_TABLE", flush=True)
    df = build_entity_table(v1_dir)
    print(f"  entities={len(df)}", flush=True)

    if not skip_approach:
        print("ENRICH_APPROACH", flush=True)
        df = enrich_approach(df)
        print(f"  approach_known={(df['approach_regime']!='unknown').sum()}", flush=True)

    print("TEMPORAL_SPLITS", flush=True)
    df, splits = assign_temporal_splits(df)

    print("EPISODES", flush=True)
    df, episodes = assign_episodes(df)
    # attach temporal_split to episodes from leader already in assign — refresh from df
    if not episodes.empty and "temporal_split" in df.columns:
        ep_split = df.groupby("episode_id")["temporal_split"].agg(
            lambda s: s.mode().iloc[0] if len(s.mode()) else s.iloc[0]
        )
        episodes["temporal_split"] = episodes["episode_id"].map(ep_split)

    # --- cohorts ---
    print("COHORTS", flush=True)
    distance_cohorts = _cohort_table(
        df, ["distance_atr_bucket", "touch_timing", "entity_kind", "timeframe"]
    )
    distance_pct = _cohort_table(df, ["distance_pct_bucket", "touch_timing"])
    distance_cohorts = pd.concat([distance_cohorts, distance_pct], ignore_index=True, sort=False)

    age_cohorts = _cohort_table(df, ["age_at_touch_bucket", "component_bucket", "timeframe"])

    # controlled component comparison: within distance × width × age × side × tf
    component_cohorts = _cohort_table(
        df,
        [
            "component_bucket",
            "distance_atr_bucket",
            "width_atr_bucket",
            "age_at_touch_bucket",
            "side",
            "timeframe",
            "vol_regime",
            "trend_to_pool",
        ],
    )
    # also uncontrolled summary
    component_simple = _cohort_table(df, ["component_bucket", "entity_kind"])
    component_cohorts = pd.concat([component_simple, component_cohorts], ignore_index=True, sort=False)

    ema_rows = []
    for flag, label in [
        ("above_ema200", "pool_above_ema200"),
        ("below_ema200", "pool_below_ema200"),
        ("overlaps_ema20", "overlaps_ema20"),
        ("overlaps_ema59", "overlaps_ema59"),
        ("overlaps_ema200", "overlaps_ema200"),
        ("between_ema20_59", "between_ema20_59"),
        ("bullish_stack", "bullish_stack"),
        ("bearish_stack", "bearish_stack"),
        ("mixed_stack", "mixed_stack"),
        ("ema_compressed", "ema_compressed"),
        ("ema_expanded", "ema_expanded"),
        ("ema20_slope_with_reaction", "ema20_slope_with_reaction"),
        ("ema20_slope_against_reaction", "ema20_slope_against_reaction"),
        ("multi_no_ema", "multi_no_ema"),
        ("multi_ema20", "multi_ema20"),
        ("multi_ema59", "multi_ema59"),
        ("multi_ema200", "multi_ema200"),
        ("multi_multi_ema", "multi_multi_ema"),
    ]:
        if flag not in df.columns:
            continue
        for side, sg in df.groupby("side"):
            sub = sg[sg[flag].fillna(False)]
            if len(sub) == 0:
                continue
            row = summarize_group(sub, group_cols={"ema_group": label, "side": side})
            row["median_mfe_after_reclaim"] = (
                float(sub["mfe_after_reclaim"].median()) if sub["mfe_after_reclaim"].notna().any() else None
            )
            row["median_mae_before_reclaim"] = (
                float(sub["mae_before_reclaim"].median()) if sub["mae_before_reclaim"].notna().any() else None
            )
            ema_rows.append(row)
    ema_confluence_cohorts = pd.DataFrame(ema_rows)

    approach_regime_cohorts = _cohort_table(
        df, ["approach_regime", "component_bucket", "side", "timeframe"]
    )
    # slow vs impulsive × multi × ema
    approach_extra = _cohort_table(
        df.assign(
            multi_ema=df["multi_pool"] & df["overlaps_ema20"].fillna(False)
        ),
        ["approach_regime", "multi_pool", "multi_ema"],
    )
    approach_regime_cohorts = pd.concat(
        [approach_regime_cohorts, approach_extra], ignore_index=True, sort=False
    )

    # bootstrap intervals for key cohorts (full populations, not pre-filtered successes)
    boot_rows = []
    boot_specs = [
        ("all_entities_touch", df, "touched", ["utc_day"]),
        ("all_entities_sweep", df, "swept", ["utc_day"]),
        ("delayed_touch_defense", df[df["touch_timing"] == "delayed_touch"], "defended", ["utc_day"]),
        ("immediate_touch_sweep", df[df["touch_timing"] == "immediate_touch"], "swept", ["utc_day"]),
        ("6plus_defense", df[df["multi_6plus"].fillna(False)], "defended", ["utc_day"]),
        ("6plus_defense_symday", df[df["multi_6plus"].fillna(False)], "defended", ["symbol", "utc_day"]),
        (
            "6plus_delayed_distant_defense",
            df[
                df["multi_6plus"].fillna(False)
                & (df["touch_timing"] == "delayed_touch")
                & df["distance_atr_bucket"].isin(["0.5-1", "1-2", "2-3", ">3"])
            ],
            "defended",
            ["utc_day"],
        ),
    ]
    if not episodes.empty:
        boot_specs.append(
            (
                "episode_6plus_defense",
                episodes[episodes["multi_6plus"].fillna(False)],
                "defended",
                ["utc_day"],
            )
        )
    for label, sub, outcome, blocks in boot_specs:
        if sub is None or len(sub) == 0 or outcome not in sub.columns:
            continue
        br = block_bootstrap_rate(sub, success_col=outcome, block_cols=blocks, n_boot=n_boot)
        boot_rows.append({"cohort": label, "outcome": outcome, "blocks": "|".join(blocks), **br})
    bootstrap_intervals = pd.DataFrame(boot_rows)

    print("CANDIDATES", flush=True)
    candidates, oos_results, selected = evaluate_candidates(df, n_boot=n_boot)
    confirmed = oos_results[oos_results["confirmed_oos"] == True]  # noqa: E712
    verdict = VERDICT_COMPLETE if len(confirmed) else VERDICT_NO_STABLE
    # still COMPLETE as analysis finished; user asked NO_STABLE_EDGE if no rule OOS stable
    # Use NO_STABLE as primary verdict when none confirmed
    final_verdict = VERDICT_NO_STABLE if len(confirmed) == 0 else VERDICT_COMPLETE

    quality = (
        df.groupby(["symbol", "timeframe", "temporal_split"], dropna=False)
        .agg(
            n=("entity_id", "count"),
            touch_rate=("touched", "mean"),
            sweep_rate=("swept", "mean"),
            defended_rate=("defended", "mean"),
            immediate_share=("touch_timing", lambda s: float((s == "immediate_touch").mean())),
        )
        .reset_index()
    )

    # write artifacts
    _write_csv(out_dir / "distance_cohorts.csv", distance_cohorts)
    _write_csv(out_dir / "age_cohorts.csv", age_cohorts)
    _write_csv(out_dir / "component_cohorts.csv", component_cohorts)
    _write_csv(out_dir / "ema_confluence_cohorts.csv", ema_confluence_cohorts)
    _write_csv(out_dir / "approach_regime_cohorts.csv", approach_regime_cohorts)
    _write_csv(out_dir / "independent_episodes.csv", episodes)
    _write_csv(out_dir / "candidate_rules.csv", candidates)
    _write_csv(out_dir / "oos_results.csv", oos_results)
    _write_csv(out_dir / "bootstrap_intervals.csv", bootstrap_intervals)
    _write_csv(out_dir / "quality_by_symbol.csv", quality)
    # entity audit table (helpful, not required) — skip if huge; write slim
    slim_cols = [
        c
        for c in [
            "entity_id",
            "entity_kind",
            "symbol",
            "timeframe",
            "side",
            "n_components",
            "component_bucket",
            "known_at",
            "utc_day",
            "temporal_split",
            "distance_atr_bucket",
            "touch_timing",
            "age_at_touch_bucket",
            "approach_regime",
            "touched",
            "swept",
            "defended",
            "swept_reclaimed",
            "consumed_accepted",
            "episode_id",
            "multi_6plus",
            "overlaps_ema200",
            "bullish_stack",
            "bearish_stack",
        ]
        if c in df.columns
    ]
    _write_csv(out_dir / "entity_enriched.csv", df[slim_cols])

    (out_dir / "temporal_splits.json").write_text(json.dumps(splits, indent=2, default=str), encoding="utf-8")

    write_methodology(out_dir)
    write_report(
        out_dir,
        final_verdict,
        df,
        episodes,
        splits,
        candidates,
        oos_results,
        confirmed,
        bootstrap_intervals,
        elapsed=time.time() - t0,
    )
    manifest = {
        "analysis_id": ANALYSIS_ID,
        "version": ANALYSIS_VERSION,
        "verdict": final_verdict,
        "v1_source": str(v1_dir),
        "created_at": pd.Timestamp.utcnow().isoformat(),
        "elapsed_sec": round(time.time() - t0, 2),
        "n_entities": len(df),
        "n_episodes": len(episodes),
        "n_rules_selected_discovery": len(selected),
        "n_rules_confirmed_oos": int(len(confirmed)),
        "splits": splits,
        "display_causality_fix": {
            "zone_start": "created_timestamp/known_at",
            "source_timestamp_in_tooltip": True,
            "trp_compose_updated": True,
            "chart_asset_v": "live-17",
            "dashboard_restart": False,
        },
        "no_commit": True,
        "no_bot_pnl": True,
        "no_clickhouse_writes": True,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    print("VERDICT", final_verdict, flush=True)
    return {"verdict": final_verdict, "manifest": manifest, "out_dir": str(out_dir)}


def write_methodology(out_dir: Path) -> None:
    text = """# Liquidity Location Pool Edge Validation V2 — Methodology

## Purpose
Validate whether apparent lifecycle patterns survive distance/age controls,
independent episodes, and chronological out-of-sample splits. No bot/PnL.

## Inputs
Reuse causal v1 artifacts under `results/liquidity_location_pool_lifecycle_v1/`
(primary outcome variant: acceptance=2, reclaim=6, reaction=0.5 ATR).

## Display causality (separate code change)
TRP `compose_pool_overlays` / cluster overlays now set `start_timestamp = known_at`
(`created_timestamp`). `source_timestamp` remains in metadata for tooltips.
Geometry unchanged. Dashboard asset cache bumped to `live-17` (no process restart).

## Distance
`distance_from_price_atr` at known_at (from v1). Buckets: 0–0.25, 0.25–0.5, 0.5–1,
1–2, 2–3, >3 ATR, plus percent buckets. Touch timing: immediate (bars_to_touch==0),
delayed, untouched.

## Age
`bars_to_touch` from known_at analysis start to first touch.

## Components
Buckets 1 / 2 / 3 / 4–5 / 6+, stratified by distance, width_atr, age, side, TF,
vol regime, trend_to_pool. Univariate rates are descriptive only.

## EMA confluence
From v1 `pool_ema_context` CREATED snapshot: overlaps, stacks, compression,
multi-pool × EMA groups A–E.

## Approach regime
Closed-candle returns 3/6/12, ATR speed, consecutive bars, volume ratio at approach
(or touch) index. Classes: impulsive_toward, slow_toward, toward_pool, away_from_pool, flat_range.

## Independent episodes
Union-find within symbol×side×TF: price overlap (0.10% gap) + time proximity
(≤12 bars) or identical first_touch_index. Report pool / cluster / episode levels.

## Temporal splits
Chronological known_at quantiles 60% discovery / 20% validation / 20% OOS.
Fixed before rule selection. Rules selected only if discovery n≥30.
OOS confirmation: same direction vs baseline, |Δ|≥3pp, n≥20, ≥2 symbols with n≥5.

## Bootstrap
Block bootstrap by UTC day and by symbol×day (B=300).

## Candidate rules
Pre-specified R1–R8 (see candidate_rules.csv). No 90%-rate cherry-picking.
"""
    path = out_dir / "methodology.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text(text, encoding="utf-8")


def write_report(
    out_dir: Path,
    verdict: str,
    df: pd.DataFrame,
    episodes: pd.DataFrame,
    splits: dict,
    candidates: pd.DataFrame,
    oos_results: pd.DataFrame,
    confirmed: pd.DataFrame,
    bootstrap_intervals: pd.DataFrame,
    *,
    elapsed: float,
) -> None:
    def rate(mask, col="defended"):
        sub = df.loc[mask]
        if len(sub) == 0:
            return "n=0"
        return f"{sub[col].mean():.3f} (n={len(sub)})"

    imm = df["touch_timing"] == "immediate_touch"
    dely = df["touch_timing"] == "delayed_touch"
    lines = [
        f"# {verdict}",
        "",
        "## 1. VERDICT",
        verdict,
        "",
        "## 2. DISPLAY-KAUSALITÄT",
        "- Zone start = `known_at` (`created_timestamp`); no paint on source bar.",
        "- Tooltip: `known_at` + `source_timestamp` separated (chart.js live-17).",
        "- Geometry/engine unchanged. No dashboard process restart.",
        "",
        "## 3. COVERAGE UND SPLITS",
        f"- entities={len(df)} episodes={len(episodes)}",
        f"- splits: {json.dumps(splits, default=str)}",
        "",
        "## 4. IMMEDIATE VS. DELAYED TOUCH",
        f"- immediate_touch share={(imm.mean()):.3f} sweep|imm={rate(imm,'swept')} defend|imm={rate(imm)}",
        f"- delayed_touch share={(dely.mean()):.3f} sweep|del={rate(dely,'swept')} defend|del={rate(dely)}",
        f"- untouched share={(df['touch_timing']=='untouched').mean():.3f}",
        "- Unconditional high sweep is largely proximity/immediate-touch — not a magnet edge.",
        "",
        "## 5. DISTANZ",
        "See distance_cohorts.csv. Expect touch/sweep ↓ as |distance_atr| ↑.",
    ]
    if "distance_atr_bucket" in df.columns:
        for b, g in df.groupby("distance_atr_bucket"):
            lines.append(
                f"- {b}: n={len(g)} touch={g['touched'].mean():.3f} sweep={g['swept'].mean():.3f} "
                f"defend={g['defended'].mean():.3f}"
            )
    lines += ["", "## 6. ALTER", "See age_cohorts.csv (age at first touch)."]
    if "age_at_touch_bucket" in df.columns:
        for b, g in df.groupby("age_at_touch_bucket"):
            lines.append(
                f"- {b}: n={len(g)} sweep={g['swept'].mean():.3f} defend={g['defended'].mean():.3f}"
            )
    lines += [
        "",
        "## 7. EINZELPOOL VS. MULTI-POOL",
        "Univariate (descriptive only):",
    ]
    for b, g in df.groupby("component_bucket"):
        lines.append(
            f"- {b}: n={len(g)} defend={g['defended'].mean():.3f} sweep={g['swept'].mean():.3f}"
        )
    # controlled: delayed + dist>=0.5
    ctrl = df[
        (df["touch_timing"] == "delayed_touch")
        & df["distance_atr_bucket"].isin(["0.5-1", "1-2", "2-3", ">3"])
    ]
    lines.append("Controlled (delayed_touch & dist≥0.5 ATR):")
    for b, g in ctrl.groupby("component_bucket"):
        lines.append(
            f"- {b}: n={len(g)} defend={g['defended'].mean():.3f} sweep={g['swept'].mean():.3f}"
        )
    lines += ["", "## 8. EMA-KONFLUENZ", "See ema_confluence_cohorts.csv."]
    for label in ["multi_no_ema", "multi_ema20", "multi_ema59", "multi_ema200", "multi_multi_ema"]:
        if label in df.columns:
            lines.append(f"- {label}: defend {rate(df[label].fillna(False))}")
    lines += ["", "## 9. APPROACH-REGIME", "See approach_regime_cohorts.csv."]
    if "approach_regime" in df.columns:
        for b, g in df.groupby("approach_regime"):
            lines.append(
                f"- {b}: n={len(g)} consumed={g['consumed_accepted'].mean():.3f} "
                f"defend={g['defended'].mean():.3f}"
            )
    lines += [
        "",
        "## 10. UNABHÄNGIGE EPISODEN",
        f"- episodes={len(episodes)} mean_members={episodes['n_members'].mean() if len(episodes) else None}",
    ]
    if len(episodes):
        lines.append(
            f"- episode defend={episodes['defended'].mean():.3f} sweep={episodes['swept'].mean():.3f}"
        )
        e6 = episodes[episodes["multi_6plus"] == True]  # noqa: E712
        lines.append(f"- episode 6+ defend={e6['defended'].mean():.3f} (n={len(e6)})" if len(e6) else "- no 6+ episodes")
    lines += ["", "## 11. DISCOVERY VS. OOS", "See oos_results.csv / candidate_rules.csv."]
    if len(oos_results):
        lines.append(oos_results.to_string(index=False))
    lines += ["", "## 12. STABILE KANDIDATENREGELN"]
    if len(confirmed):
        lines.append(confirmed.to_string(index=False))
    else:
        lines.append("None confirmed under pre-registered OOS criteria.")
    lines += ["", "## 13. VERWORFENE SCHEINPATTERNS"]
    lines.append(
        "- Unconditional ~90% sweep/touch as 'edge' — rejected (immediate_touch + near-price birth)."
    )
    lines.append(
        "- Univariate 6+ defense lift without distance/age controls — treat as hypothesis only unless OOS-confirmed."
    )
    rejected = oos_results[oos_results["confirmed_oos"] == False] if len(oos_results) else oos_results  # noqa: E712
    if len(rejected):
        for _, r in rejected.iterrows():
            lines.append(f"- {r['rule_id']}: {r['reason']}")
    lines += [
        "",
        "## 14. BLOCKER",
        "- None fatal. Soft: approach features need CH; episode defense definition is conservative.",
        "",
        "## 15. EMPFEHLUNG FÜR PHASE 3",
        "1) If any OOS-stable rule: design a narrow observational checklist (still no bot).",
        "2) If none: collect longer history / more symbols only after tightening delayed+distant filters.",
        "3) Strength-decay and orderflow overlays as next causal features.",
        f"",
        f"Elapsed: {elapsed:.1f}s",
    ]
    if bootstrap_intervals is not None and len(bootstrap_intervals):
        lines += ["", "### Bootstrap (selected)", bootstrap_intervals.to_string(index=False)]
    path = out_dir / "report.md"
    if path.exists():
        raise FileExistsError(path)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
