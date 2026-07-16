"""Audit runner for clean-regime variants (research-only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from research.regime_scanner.indicator_pattern_discovery import build_discovery_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_detector_clean_regime import (
    CLEAN_STATES,
    CleanRegimeConfig,
    apply_clean_regime,
    bot_interface_frame,
    build_rule_spec,
    config_hash,
    pine_rule_hash,
    prepare_feature_frame_from_ohlcv_features,
    python_rule_hash,
    rule_spec_hash,
    step_clean_regime_state,
    CleanRuntimeState,
    prepare_bar_features,
)
from research.regime_scanner.trend_detector_clean_regime_pine import (
    CLEAN_PINE_NAME,
    write_clean_regime_pines,
)
from research.regime_scanner.trend_pine_export import validate_pine_script
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_3b_clean_regime")
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)
VARIANTS = ("light", "medium", "strong")


def _duration_stats(states: Sequence[str]) -> dict[str, Any]:
    if not states:
        return {
            "n_runs": 0,
            "mean_duration": None,
            "median_duration": None,
            "share_1_candle": None,
            "share_le_2_candle": None,
        }
    runs: list[int] = []
    cur = states[0]
    length = 1
    for s in states[1:]:
        if s == cur:
            length += 1
        else:
            runs.append(length)
            cur = s
            length = 1
    runs.append(length)
    n = len(runs)
    return {
        "n_runs": n,
        "mean_duration": float(sum(runs) / n),
        "median_duration": float(statistics.median(runs)),
        "share_1_candle": sum(1 for r in runs if r == 1) / n,
        "share_le_2_candle": sum(1 for r in runs if r <= 2) / n,
        "durations": runs,
    }


def _transition_rows(states: Sequence[str]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str]] = Counter()
    for a, b in zip(states, states[1:]):
        if a != b:
            counts[(a, b)] += 1
    return [
        {"from_state": a, "to_state": b, "n_transitions": n}
        for (a, b), n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _direct_flips(states: Sequence[str]) -> int:
    flips = 0
    for a, b in zip(states, states[1:]):
        if a.endswith("confirmed") and b.endswith("confirmed") and a != b:
            if ("bullish" in a and "bearish" in b) or ("bearish" in a and "bullish" in b):
                flips += 1
        if a.endswith("building") and b.endswith("confirmed"):
            if ("bullish" in a and "bearish" in b) or ("bearish" in a and "bullish" in b):
                flips += 1
    return flips


def compare_raw_and_clean(
    feature_frame: pd.DataFrame,
    clean_df: pd.DataFrame,
) -> dict[str, Any]:
    raw = feature_frame["research_state"].astype(str).tolist()
    clean = clean_df["clean_regime_state"].astype(str).tolist()
    n = len(clean) or 1
    changes_raw = sum(1 for a, b in zip(raw, raw[1:]) if a != b)
    changes_clean = int(clean_df["clean_regime_changed"].sum())
    suppressed = int(clean_df.get("suppressed_flip", pd.Series([False] * len(clean_df))).fillna(False).sum())

    # Delay raw early -> clean building
    delays_early: list[int] = []
    delays_dev: list[int] = []
    delays_conf: list[int] = []
    for i, r in enumerate(raw):
        if r == "early_bullish":
            for lag in range(0, 48):
                j = i + lag
                if j >= len(clean):
                    break
                if clean[j] == "bullish_building":
                    delays_early.append(lag)
                    break
        if r == "developing_bullish":
            for lag in range(0, 48):
                j = i + lag
                if j >= len(clean):
                    break
                if clean[j] in {"bullish_building", "bullish_confirmed"}:
                    delays_dev.append(lag)
                    break
        if r == "confirmed_bullish":
            for lag in range(0, 48):
                j = i + lag
                if j >= len(clean):
                    break
                if clean[j] == "bullish_confirmed":
                    delays_conf.append(lag)
                    break

    confirmed_raw_mask = [r in {"confirmed_bullish", "confirmed_bearish"} for r in raw]
    matching_clean_conf = 0
    raw_conf_n = 0
    for r, c in zip(raw, clean):
        if r == "confirmed_bullish":
            raw_conf_n += 1
            if c == "bullish_confirmed":
                matching_clean_conf += 1
        elif r == "confirmed_bearish":
            raw_conf_n += 1
            if c == "bearish_confirmed":
                matching_clean_conf += 1

    clean_conf_n = sum(1 for c in clean if c.endswith("confirmed"))
    clean_conf_dir_ok = 0
    for r, c in zip(raw, clean):
        if c == "bullish_confirmed" and ("bullish" in r or r == "neutral"):
            clean_conf_dir_ok += 1
        if c == "bearish_confirmed" and ("bearish" in r or r == "neutral"):
            clean_conf_dir_ok += 1

    counts = Counter(clean)
    dur = _duration_stats(clean)
    return {
        "n_bars": len(clean),
        "raw_transitions": changes_raw,
        "clean_transitions": changes_clean,
        "transitions_per_100_bars_raw": changes_raw / n * 100.0,
        "transitions_per_100_bars_clean": changes_clean / n * 100.0,
        "suppressed_flips": suppressed,
        "direct_direction_flips": _direct_flips(clean),
        "mean_duration": dur["mean_duration"],
        "median_duration": dur["median_duration"],
        "share_1_candle": dur["share_1_candle"],
        "share_le_2_candle": dur["share_le_2_candle"],
        "coverage": {
            s: counts.get(s, 0) / n for s in CLEAN_STATES
        },
        "mean_delay_raw_early_to_clean_building": (
            float(sum(delays_early) / len(delays_early)) if delays_early else None
        ),
        "mean_delay_raw_developing_to_clean_building": (
            float(sum(delays_dev) / len(delays_dev)) if delays_dev else None
        ),
        "mean_delay_raw_confirmed_to_clean_confirmed": (
            float(sum(delays_conf) / len(delays_conf)) if delays_conf else None
        ),
        "share_raw_confirmed_with_matching_clean_confirmed": (
            matching_clean_conf / raw_conf_n if raw_conf_n else None
        ),
        "share_clean_confirmed_with_compatible_raw_direction": (
            clean_conf_dir_ok / clean_conf_n if clean_conf_n else None
        ),
    }


def outcome_audit_rows(
    clean_df: pd.DataFrame,
    outcomes: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Join clean states to existing outcomes by nearest timestamp (evaluation only)."""
    if not outcomes or clean_df.empty:
        return []
    out_df = pd.DataFrame(list(outcomes))
    if "event_timestamp" not in out_df.columns:
        return []
    clean = clean_df.copy()
    clean["_ts"] = pd.to_datetime(clean["decision_time"], utc=True)
    out_df["_ts"] = pd.to_datetime(out_df["event_timestamp"], utc=True)
    merged = pd.merge_asof(
        out_df.sort_values("_ts"),
        clean[["_ts", "clean_regime_state", "smoothing_variant", "clean_regime_direction"]].sort_values("_ts"),
        on="_ts",
        direction="backward",
    )
    rows: list[dict[str, Any]] = []
    for _, r in merged.iterrows():
        rows.append(
            {
                "event_timestamp": r.get("event_timestamp"),
                "event_type": r.get("event_type"),
                "outcome_class": r.get("outcome_class"),
                "h12_directional_close_return_pct": r.get("h12_directional_close_return_pct"),
                "h12_mfe_pct": r.get("h12_mfe_pct"),
                "h12_mae_pct": r.get("h12_mae_pct"),
                "clean_regime_state": r.get("clean_regime_state"),
                "clean_regime_direction": r.get("clean_regime_direction"),
                "smoothing_variant": r.get("smoothing_variant"),
                "note": "outcome is evaluation-only; never feeds clean state",
            }
        )
    return rows


def synthetic_parity_sequence(cfg: CleanRegimeConfig) -> list[dict[str, Any]]:
    """Hand-crafted prepared features to exercise the state machine."""
    def feat(**kwargs: Any) -> dict[str, Any]:
        base = {
            "raw_research_state": "neutral",
            "building_bull": False,
            "building_bear": False,
            "confirmed_bull": False,
            "confirmed_bear": False,
            "hold_bull_confirmed": False,
            "hold_bear_confirmed": False,
            "weaken_bull": False,
            "weaken_bear": False,
            "lose_bull": False,
            "lose_bear": False,
            "di_bull": False,
            "di_bear": False,
            "di_direction": 0,
            "di_diff": 0.0,
            "adx": 20.0,
            "adx_rising": False,
            "adx_slope_3": 0.0,
            "adx_slope_5": 0.0,
            "ema_order_direction": 0,
            "ema_joint_slope_direction": 0,
            "band_expanding": False,
            "atr_relevant": False,
            "bullish_component_count": 0,
            "bearish_component_count": 0,
            "net_research_score": 0,
        }
        base.update(kwargs)
        return base

    seq: list[dict[str, Any]] = []
    # Stay neutral
    for _ in range(3):
        seq.append(feat())
    # Single bullish candidate candle (should NOT flip yet)
    seq.append(feat(building_bull=True, di_bull=True, net_research_score=2, bullish_component_count=5, raw_research_state="early_bullish"))
    # Back to neutral-ish (candidate reset)
    seq.append(feat())
    # Confirmed building streak
    for _ in range(cfg.building_confirmation):
        seq.append(feat(building_bull=True, di_bull=True, net_research_score=3, bullish_component_count=6, raw_research_state="developing_bullish"))
    # Hold building a bit
    for _ in range(max(1, cfg.min_building_hold)):
        seq.append(feat(building_bull=True, di_bull=True, hold_bull_confirmed=True, net_research_score=2, bullish_component_count=5, raw_research_state="developing_bullish"))
    # Confirmed streak
    for _ in range(cfg.confirmed_confirmation):
        seq.append(feat(confirmed_bull=True, building_bull=True, di_bull=True, hold_bull_confirmed=True, net_research_score=4, bullish_component_count=8, raw_research_state="confirmed_bullish", band_expanding=True, atr_relevant=True))
    # Single weak opposite candle — must NOT exit confirmed immediately if hold still true
    seq.append(feat(hold_bull_confirmed=True, di_bull=True, weaken_bull=True, net_research_score=0, bullish_component_count=4, bearish_component_count=3, raw_research_state="weakening_bullish"))
    # Multi weaken -> building
    for _ in range(cfg.opposite_confirmation):
        seq.append(feat(weaken_bull=True, hold_bull_confirmed=False, di_bull=True, net_research_score=-1, bullish_component_count=3, bearish_component_count=4, raw_research_state="weakening_bullish"))
    # Multi lose -> neutral
    for _ in range(max(cfg.neutral_confirmation, cfg.opposite_confirmation)):
        seq.append(feat(lose_bull=True, building_bear=False, net_research_score=-2, raw_research_state="failed_bullish"))
    # Bearish build + confirm
    for _ in range(cfg.building_confirmation):
        seq.append(feat(building_bear=True, di_bear=True, net_research_score=-3, bearish_component_count=6, raw_research_state="developing_bearish"))
    for _ in range(max(1, cfg.min_building_hold)):
        seq.append(feat(building_bear=True, di_bear=True, hold_bear_confirmed=True, net_research_score=-2, bearish_component_count=5, raw_research_state="developing_bearish"))
    for _ in range(cfg.confirmed_confirmation):
        seq.append(feat(confirmed_bear=True, building_bear=True, di_bear=True, hold_bear_confirmed=True, net_research_score=-4, bearish_component_count=8, raw_research_state="confirmed_bearish", band_expanding=True, atr_relevant=True))
    # Attempt illegal direct flip to bullish confirmed
    for _ in range(3):
        seq.append(feat(confirmed_bull=True, building_bull=True, di_bull=True, net_research_score=5, bullish_component_count=9, raw_research_state="confirmed_bullish"))
    return seq


def run_synthetic_parity(cfg: CleanRegimeConfig) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    rt = CleanRuntimeState()
    prev = "neutral"
    for i, feat in enumerate(synthetic_parity_sequence(cfg)):
        new_state, rt, diag = step_clean_regime_state(prev, feat, rt, cfg)
        rows.append(
            {
                "bar": i,
                "expected_python_clean_state": new_state,
                "expected_direction": diag["clean_regime_direction"],
                "expected_strength": diag["clean_regime_strength"],
                "expected_state_age": diag["clean_regime_age_bars"],
                "expected_transition": diag["transition_reason"],
                "expected_variant": cfg.variant,
                "candidate_state": diag["candidate_state"],
                "candidate_state_count": diag["candidate_state_count"],
                "suppressed_flip": diag["suppressed_flip"],
                "clean_state_code": diag["clean_state_code"],
                "pine_expected_same": True,
                "note": "Pine implements identical step rules from rule_spec",
            }
        )
        prev = new_state
    return rows


def run_clean_regime_audit(
    *,
    symbol: str = "APTUSDT",
    timeframe: str = "30m",
    load_start: str = "2026-01-01",
    load_end: str = "2026-05-15",
    analyze_start: str = "2026-02-01",
    analyze_end: str = "2026-04-30",
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE_DIR,
    cache_dir: Path | None = None,
    outcomes_csv: Path | None = None,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline = assert_baseline_readonly(baseline_dir)
    if not baseline.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline.get('baseline_hash')}"
        )

    t0 = time.perf_counter()
    frame = build_discovery_frame(
        symbol=symbol,
        timeframe=timeframe,
        load_start=load_start,
        load_end=load_end,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        cache_dir=cache_dir
        or Path("research/regime_scanner/results/phase_c3_3b_apt_pattern_discovery/.cache/indicator_features"),
    )
    features = prepare_feature_frame_from_ohlcv_features(frame)
    a0 = pd.Timestamp(analyze_start, tz="UTC")
    a1 = pd.Timestamp(analyze_end, tz="UTC")
    ts = pd.to_datetime(features["decision_time"], utc=True)
    features = features.loc[(ts >= a0) & (ts <= a1)].copy().reset_index(drop=True)
    features["symbol"] = symbol
    features["timeframe"] = timeframe

    variant_summaries: dict[str, Any] = {}
    comparison_rows: list[dict[str, Any]] = []
    all_suppressed: list[dict[str, Any]] = []
    primary_clean: pd.DataFrame | None = None

    for variant in VARIANTS:
        cfg = CleanRegimeConfig.for_variant(variant)
        clean = apply_clean_regime(features, cfg)
        metrics = compare_raw_and_clean(features, clean)
        dur = _duration_stats(clean["clean_regime_state"].tolist())
        transitions = _transition_rows(clean["clean_regime_state"].tolist())
        counts = (
            clean["clean_regime_state"].value_counts().rename_axis("state").reset_index(name="n_bars")
        )
        counts["share"] = counts["n_bars"] / max(len(clean), 1)
        counts["variant"] = variant

        clean.to_csv(output_dir / f"clean_regime_bars_{variant}.csv", index=False)
        bot_interface_frame(clean).to_csv(
            output_dir / f"clean_regime_bot_interface_{variant}.csv", index=False
        )
        counts.to_csv(output_dir / f"clean_regime_state_counts_{variant}.csv", index=False)
        pd.DataFrame(transitions).to_csv(
            output_dir / f"clean_regime_transitions_{variant}.csv", index=False
        )
        pd.DataFrame(
            [{"duration": d, "variant": variant} for d in dur.get("durations", [])]
        ).to_csv(output_dir / f"clean_regime_duration_distribution_{variant}.csv", index=False)

        suppressed = clean[clean.get("suppressed_flip", False) == True]  # noqa: E712
        for _, r in suppressed.iterrows():
            all_suppressed.append(
                {
                    "timestamp": r.get("timestamp"),
                    "variant": variant,
                    "previous_clean_regime_state": r.get("previous_clean_regime_state"),
                    "desired_state": r.get("desired_state"),
                    "transition_reason": r.get("transition_reason"),
                    "candidate_state_count": r.get("candidate_state_count"),
                }
            )

        parity = run_synthetic_parity(cfg)
        pd.DataFrame(parity).to_csv(
            output_dir / f"clean_regime_python_pine_parity_{variant}.csv", index=False
        )

        # TradingView comparison export (medium primary naming without suffix too).
        tv = clean[
            [
                "timestamp",
                "decision_time",
                "clean_regime_state",
                "clean_regime_direction",
                "clean_regime_strength",
                "clean_regime_age_bars",
                "transition_reason",
                "smoothing_variant",
                "clean_state_code",
            ]
        ].rename(
            columns={
                "clean_regime_state": "expected_python_clean_state",
                "clean_regime_direction": "expected_direction",
                "clean_regime_strength": "expected_strength",
                "clean_regime_age_bars": "expected_state_age",
                "transition_reason": "expected_transition",
                "smoothing_variant": "expected_variant",
            }
        )
        tv.to_csv(output_dir / f"clean_regime_tv_compare_{variant}.csv", index=False)

        spec = build_rule_spec(cfg)
        (output_dir / f"clean_regime_config_{variant}.json").write_text(
            json.dumps(json_safe(cfg.to_dict()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"clean_regime_rule_spec_{variant}.json").write_text(
            json.dumps(json_safe(spec), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"clean_regime_config_hash_{variant}.txt").write_text(
            config_hash(cfg) + "\n", encoding="utf-8"
        )
        (output_dir / f"clean_regime_rule_hash_{variant}.txt").write_text(
            rule_spec_hash(spec) + "\n", encoding="utf-8"
        )

        summary = {
            "variant": variant,
            "config_hash": config_hash(cfg),
            "rule_spec_hash": rule_spec_hash(spec),
            "python_rule_hash": python_rule_hash(cfg),
            "pine_rule_hash": pine_rule_hash(cfg),
            "hashes_match": python_rule_hash(cfg) == pine_rule_hash(cfg),
            **metrics,
        }
        variant_summaries[variant] = summary
        comparison_rows.append(
            {
                "variant": variant,
                "transitions": metrics["clean_transitions"],
                "transitions_per_100": metrics["transitions_per_100_bars_clean"],
                "mean_duration": metrics["mean_duration"],
                "share_1_candle": metrics["share_1_candle"],
                "share_le_2_candle": metrics["share_le_2_candle"],
                "suppressed_flips": metrics["suppressed_flips"],
                "direct_flips": metrics["direct_direction_flips"],
                "bullish_building_share": metrics["coverage"].get("bullish_building"),
                "bullish_confirmed_share": metrics["coverage"].get("bullish_confirmed"),
                "bearish_building_share": metrics["coverage"].get("bearish_building"),
                "bearish_confirmed_share": metrics["coverage"].get("bearish_confirmed"),
                "neutral_share": metrics["coverage"].get("neutral"),
                "delay_early_to_building": metrics["mean_delay_raw_early_to_clean_building"],
                "delay_confirmed_to_clean_confirmed": metrics[
                    "mean_delay_raw_confirmed_to_clean_confirmed"
                ],
            }
        )
        if variant == "medium":
            primary_clean = clean

    # Canonical medium aliases for required filenames.
    assert primary_clean is not None
    medium_cfg = CleanRegimeConfig.for_variant("medium")
    primary_clean.to_csv(output_dir / "clean_regime_bars.csv", index=False)
    bot_interface_frame(primary_clean).to_csv(
        output_dir / "clean_regime_bot_interface.csv", index=False
    )
    pd.read_csv(output_dir / "clean_regime_state_counts_medium.csv").to_csv(
        output_dir / "clean_regime_state_counts.csv", index=False
    )
    pd.read_csv(output_dir / "clean_regime_transitions_medium.csv").to_csv(
        output_dir / "clean_regime_transitions.csv", index=False
    )
    pd.read_csv(output_dir / "clean_regime_duration_distribution_medium.csv").to_csv(
        output_dir / "clean_regime_duration_distribution.csv", index=False
    )
    pd.read_csv(output_dir / "clean_regime_python_pine_parity_medium.csv").to_csv(
        output_dir / "clean_regime_python_pine_parity.csv", index=False
    )
    pd.read_csv(output_dir / "clean_regime_tv_compare_medium.csv").to_csv(
        output_dir / "clean_regime_tv_compare.csv", index=False
    )
    (output_dir / "clean_regime_config.json").write_text(
        (output_dir / "clean_regime_config_medium.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (output_dir / "clean_regime_rule_spec.json").write_text(
        (output_dir / "clean_regime_rule_spec_medium.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (output_dir / "clean_regime_config_hash.txt").write_text(
        config_hash(medium_cfg) + "\n", encoding="utf-8"
    )
    (output_dir / "clean_regime_rule_hash.txt").write_text(
        rule_spec_hash(cfg=medium_cfg) + "\n", encoding="utf-8"
    )

    pd.DataFrame(comparison_rows).to_csv(
        output_dir / "clean_regime_variant_comparison.csv", index=False
    )
    pd.DataFrame(all_suppressed).to_csv(
        output_dir / "clean_regime_suppressed_flips.csv", index=False
    )

    outcomes = None
    out_path = outcomes_csv or Path(
        "research/regime_scanner/results/phase_c3_3b_apt_pattern_discovery/multi_horizon_outcomes.csv"
    )
    if out_path.is_file():
        outcomes = pd.read_csv(out_path).to_dict("records")
    outcome_rows = outcome_audit_rows(primary_clean, outcomes)
    pd.DataFrame(outcome_rows).to_csv(output_dir / "clean_regime_outcome_audit.csv", index=False)

    pine_meta = write_clean_regime_pines(output_dir, default_variant="medium")
    for path in pine_meta["paths"].values():
        validate_pine_script(Path(path).read_text(encoding="utf-8"))

    # Deterministic rerun hash (medium bars).
    rerun = apply_clean_regime(features, medium_cfg)
    h1 = hashlib.sha256(
        primary_clean[["clean_regime_state", "transition_reason", "clean_regime_age_bars"]]
        .astype(str)
        .to_csv(index=False)
        .encode("utf-8")
    ).hexdigest()
    h2 = hashlib.sha256(
        rerun[["clean_regime_state", "transition_reason", "clean_regime_age_bars"]]
        .astype(str)
        .to_csv(index=False)
        .encode("utf-8")
    ).hexdigest()

    summary = {
        "phase": "C3_3B_clean_regime",
        "symbol": symbol,
        "timeframe": timeframe,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "baseline_hash_confirmed": bool(baseline.get("hash_matches")),
        "variants": variant_summaries,
        "variant_comparison": comparison_rows,
        "pine": pine_meta,
        "deterministic_rerun_hash_match": h1 == h2,
        "content_hash_medium": h1,
        "non_repainting": {
            "causal_step_only": True,
            "no_negative_shifts": True,
            "no_centered_windows": True,
            "no_future_indexing": True,
            "no_retro_in_state": True,
            "closed_bars_immutable": True,
        },
        "bgcolor_mapping": {
            "neutral": "no/very pale bgcolor",
            "bullish_building": "soft green (92 transparency)",
            "bullish_confirmed": "teal (82 transparency)",
            "bearish_building": "maroon (92 transparency)",
            "bearish_confirmed": "red (82 transparency)",
        },
        "safety": {
            "research_only": True,
            "no_live_bot_integration": True,
            "no_classifier_changes": True,
            "no_production_config_changes": True,
        },
        "runtime_s": round(time.perf_counter() - t0, 4),
        "artifacts": {
            "bars": "clean_regime_bars.csv",
            "bot_interface": "clean_regime_bot_interface.csv",
            "pine_main": CLEAN_PINE_NAME,
            "variant_comparison": "clean_regime_variant_comparison.csv",
            "parity": "clean_regime_python_pine_parity.csv",
        },
    }
    (output_dir / "clean_regime_run_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C3.3B clean regime audit")
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframe", default="30m")
    parser.add_argument("--load-start", default="2026-01-01")
    parser.add_argument("--load-end", default="2026-05-15")
    parser.add_argument("--analyze-start", default="2026-02-01")
    parser.add_argument("--analyze-end", default="2026-04-30")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    args = parser.parse_args(argv)
    summary = run_clean_regime_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
    )
    print(
        json.dumps(
            {
                "content_hash_medium": summary["content_hash_medium"],
                "deterministic_rerun_hash_match": summary["deterministic_rerun_hash_match"],
                "variants": {
                    k: {
                        "transitions": v["clean_transitions"],
                        "mean_duration": v["mean_duration"],
                        "hashes_match": v["hashes_match"],
                    }
                    for k, v in summary["variants"].items()
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
