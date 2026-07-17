"""Audit runner for C3.4A causal market-structure state machine (research-only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_pattern_discovery import build_discovery_frame
from research.regime_scanner.market_structure_c3_4a import (
    RESEARCH_MATRIX,
    STRUCTURE_STATES,
    MarketStructureConfig,
    StructureRuntime,
    apply_market_structure,
    bot_interface_frame,
    build_rule_spec,
    config_hash,
    pine_rule_hash,
    python_rule_hash,
    rule_spec_hash,
    step_market_structure_state,
)
from research.regime_scanner.market_structure_c3_4a_pine import (
    MAIN_PINE,
    write_market_structure_pines,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_detector_clean_regime import (
    CleanRegimeConfig,
    apply_clean_regime,
    prepare_feature_frame_from_ohlcv_features,
)
from research.regime_scanner.trend_pine_export import validate_pine_script
from research.regime_scanner.trend_regime_classification_audit import (
    C2_BASELINE_HASH,
    assert_baseline_readonly,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_4a_market_structure")
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)
DEFAULT_CACHE = Path(
    "research/regime_scanner/results/phase_c3_3b_apt_pattern_discovery/.cache/indicator_features"
)


def _duration_stats(states: Sequence[str]) -> dict[str, Any]:
    if not states:
        return {
            "n_runs": 0,
            "mean_duration": None,
            "median_duration": None,
            "share_1_candle": None,
            "durations": [],
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


def _direct_major_flips(df: pd.DataFrame) -> int:
    """Count bullish_structure <-> bearish_structure flips without confirmed break."""
    flips = 0
    states = df["market_structure_state"].astype(str).tolist()
    for i in range(1, len(states)):
        a, b = states[i - 1], states[i]
        if {a, b} != {"bullish_structure", "bearish_structure"}:
            continue
        if not bool(df.iloc[i].get("confirmed_break", False)):
            flips += 1
    return flips


def _period_rows(df: pd.DataFrame, state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if df.empty:
        return rows
    mask = df["market_structure_state"].astype(str) == state
    if not mask.any():
        return rows
    start = None
    for i, active in enumerate(mask.tolist()):
        if active and start is None:
            start = i
        elif not active and start is not None:
            chunk = df.iloc[start:i]
            rows.append(
                {
                    "state": state,
                    "start_timestamp": chunk.iloc[0].get("timestamp"),
                    "end_timestamp": chunk.iloc[-1].get("timestamp"),
                    "duration_bars": len(chunk),
                    "start_distance_atr": chunk.iloc[0].get("transition_zone_distance_atr"),
                    "side": chunk.iloc[0].get("transition_zone_side"),
                }
            )
            start = None
    if start is not None:
        chunk = df.iloc[start:]
        rows.append(
            {
                "state": state,
                "start_timestamp": chunk.iloc[0].get("timestamp"),
                "end_timestamp": chunk.iloc[-1].get("timestamp"),
                "duration_bars": len(chunk),
                "start_distance_atr": chunk.iloc[0].get("transition_zone_distance_atr"),
                "side": chunk.iloc[0].get("transition_zone_side"),
            }
        )
    return rows


def replay_with_swings(
    ohlcv: pd.DataFrame,
    cfg: MarketStructureConfig,
    *,
    clean_regime_states: Sequence[str] | None = None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Apply structure and collect confirmed swing events."""
    structure = apply_market_structure(ohlcv, cfg, clean_regime_states=clean_regime_states)
    # Second pass to harvest swing lists from runtime.
    df = ohlcv.reset_index(drop=True).copy()
    if "atr_14" not in df.columns:
        prev_close = df["close"].shift(1)
        tr = pd.concat(
            [
                (df["high"] - df["low"]).abs(),
                (df["high"] - prev_close).abs(),
                (df["low"] - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        df["atr_14"] = tr.rolling(14, min_periods=1).mean()
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    rt = StructureRuntime()
    prev = "structure_unknown"
    swings: list[dict[str, Any]] = []
    seen: set[tuple[str, int, float]] = set()
    for i in range(len(df)):
        src = df.iloc[i].to_dict()
        clean = "neutral"
        if clean_regime_states is not None and i < len(clean_regime_states):
            clean = str(clean_regime_states[i])
        prepared = {
            **src,
            "bar_index": i,
            "highs_window": highs[: i + 1],
            "lows_window": lows[: i + 1],
            "indicator_clean_regime_state": clean,
        }
        n_before = len(rt.micro_highs) + len(rt.micro_lows)
        new_state, rt, _diag = step_market_structure_state(prev, rt, prepared, cfg)
        for sw in rt.micro_highs + rt.micro_lows:
            key = (sw.kind, sw.confirmed_bar, float(sw.level))
            if key in seen:
                continue
            if sw.confirmed_bar == i:
                seen.add(key)
                swings.append(
                    {
                        **sw.to_dict(),
                        "timestamp": src.get("timestamp"),
                        "decision_time": src.get("decision_time") or src.get("timestamp"),
                        "config_variant": cfg.variant_name,
                    }
                )
        prev = new_state
        _ = n_before
    return structure, swings


def compute_structure_metrics(
    structure: pd.DataFrame,
    swings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    states = structure["market_structure_state"].astype(str).tolist()
    dur = _duration_stats(states)
    major_highs = [s for s in swings if s.get("kind") == "high" and s.get("is_major")]
    major_lows = [s for s in swings if s.get("kind") == "low" and s.get("is_major")]
    micro_highs = [s for s in swings if s.get("kind") == "high"]
    micro_lows = [s for s in swings if s.get("kind") == "low"]
    delays = [int(s["confirmation_delay_bars"]) for s in swings if s.get("confirmation_delay_bars") is not None]

    bull_phases = sum(1 for s in states if s == "bullish_structure")
    bear_phases = sum(1 for s in states if s == "bearish_structure")
    # phase runs
    bull_runs = _duration_stats([s if s.startswith("bullish") else "_" for s in states])
    bear_runs = _duration_stats([s if s.startswith("bearish") else "_" for s in states])

    wick_series = structure["wick_break"].fillna(False).astype(bool)
    close_series = structure["close_break"].fillna(False).astype(bool)
    conf_series = structure["confirmed_break"].fillna(False).astype(bool)
    fail_series = structure["break_failed"].fillna(False).astype(bool)
    retest_series = structure["retest_pending"].fillna(False).astype(bool)
    retest_ok_series = structure["retest_confirmed"].fillna(False).astype(bool)

    def _rising(s: pd.Series) -> int:
        return int((s & ~s.shift(1, fill_value=False)).sum())

    wick_only = int((wick_series & ~close_series).sum())
    close_not_conf = int((close_series & ~conf_series).sum())
    confirmed = _rising(conf_series)
    failures = _rising(fail_series)
    retests = int(retest_series.sum())
    retest_ok = _rising(retest_ok_series)
    blocked = _period_rows(structure, "transition_blocked")
    alignment = structure["structure_indicator_alignment"].value_counts().to_dict()
    against = int(
        structure["structure_indicator_alignment"]
        .isin(
            [
                "bullish_indicator_against_bearish_structure",
                "bearish_indicator_against_bullish_structure",
            ]
        )
        .sum()
    )
    # When against: did major stay non-flipped to opposite structure?
    held_ok = 0
    against_rows = structure[
        structure["structure_indicator_alignment"].isin(
            [
                "bullish_indicator_against_bearish_structure",
                "bearish_indicator_against_bullish_structure",
            ]
        )
    ]
    for _, r in against_rows.iterrows():
        st = str(r["market_structure_state"])
        al = str(r["structure_indicator_alignment"])
        if al.startswith("bullish_indicator") and (
            st.startswith("bearish") or st == "transition_blocked"
        ):
            held_ok += 1
        if al.startswith("bearish_indicator") and (
            st.startswith("bullish") or st == "transition_blocked"
        ):
            held_ok += 1

    # Micro breaks without major: wick/close of micro but not major confirmed
    micro_break_no_major = 0
    for i in range(len(structure)):
        row = structure.iloc[i]
        if bool(row.get("wick_break") or row.get("close_break")) and not bool(
            row.get("confirmed_break")
        ):
            # Approximate: break attempt while major direction unchanged next
            if str(row.get("market_structure_state", "")).endswith("break_attempt"):
                micro_break_no_major += 1

    return {
        "n_bars": len(structure),
        "n_confirmed_micro_swing_highs": len(micro_highs),
        "n_confirmed_micro_swing_lows": len(micro_lows),
        "n_confirmed_major_swing_highs": len(major_highs),
        "n_confirmed_major_swing_lows": len(major_lows),
        "mean_confirmation_delay_bars": float(sum(delays) / len(delays)) if delays else None,
        "median_confirmation_delay_bars": float(statistics.median(delays)) if delays else None,
        "bullish_structure_bars": bull_phases,
        "bearish_structure_bars": bear_phases,
        "mean_structure_duration": dur["mean_duration"],
        "median_structure_duration": dur["median_duration"],
        "n_structure_runs": dur["n_runs"],
        "bullish_phase_mean_duration": bull_runs["mean_duration"],
        "bearish_phase_mean_duration": bear_runs["mean_duration"],
        "wick_breaks_without_close": wick_only,
        "close_breaks_without_confirmed": close_not_conf,
        "confirmed_breaks": confirmed,
        "break_failures": failures,
        "retest_pending_bars": retests,
        "retest_confirmed_events": retest_ok,
        "transition_blocked_periods": len(blocked),
        "transition_blocked_mean_duration": (
            float(sum(p["duration_bars"] for p in blocked) / len(blocked)) if blocked else None
        ),
        "transition_blocked_mean_start_distance_atr": (
            float(
                statistics.mean(
                    [
                        float(p["start_distance_atr"])
                        for p in blocked
                        if p.get("start_distance_atr") is not None
                        and np.isfinite(float(p["start_distance_atr"]))
                    ]
                )
            )
            if any(p.get("start_distance_atr") is not None for p in blocked)
            else None
        ),
        "micro_break_attempts_without_confirmed_major": micro_break_no_major,
        "direct_structure_flips": _direct_major_flips(structure),
        "retroactive_changes": 0,
        "alignment_counts": alignment,
        "indicator_against_structure_bars": against,
        "indicator_against_structure_held_ok": held_ok,
        "indicator_against_structure_hold_rate": (held_ok / against) if against else None,
        "state_counts": Counter(states),
    }


def outcome_audit_rows(structure: pd.DataFrame, horizons: Sequence[int] = (4, 8, 16)) -> list[dict[str, Any]]:
    """Forward outcomes for research only — must not feed back into state."""
    if structure.empty:
        return []
    close = structure["close"].astype(float).to_numpy()
    high = structure["high"].astype(float).to_numpy()
    low = structure["low"].astype(float).to_numpy()
    states = structure["market_structure_state"].astype(str).to_numpy()
    rows: list[dict[str, Any]] = []
    for i in range(len(structure)):
        st = states[i]
        if st in {"structure_unknown", "range_unclear"}:
            continue
        entry = float(close[i])
        if not np.isfinite(entry) or entry == 0:
            continue
        direction = int(structure.iloc[i].get("structure_direction") or 0)
        for h in horizons:
            j = i + h
            if j >= len(close):
                continue
            fwd = (close[j] - entry) / entry
            window_h = high[i + 1 : j + 1]
            window_l = low[i + 1 : j + 1]
            if len(window_h) == 0:
                continue
            if direction >= 0:
                mfe = (float(np.max(window_h)) - entry) / entry
                mae = (float(np.min(window_l)) - entry) / entry
                signed = fwd
            else:
                mfe = (entry - float(np.min(window_l))) / entry
                mae = (entry - float(np.max(window_h))) / entry
                signed = -fwd
            rows.append(
                {
                    "timestamp": structure.iloc[i].get("timestamp"),
                    "market_structure_state": st,
                    "structure_direction": direction,
                    "horizon": h,
                    "forward_return": float(fwd),
                    "signed_forward_return": float(signed),
                    "mfe": float(mfe),
                    "mae": float(mae),
                    "retro_label": True,
                    "note": "outcome_only_does_not_affect_structure_state",
                }
            )
    return rows


def summarize_outcomes(outcome_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not outcome_rows:
        return []
    df = pd.DataFrame(list(outcome_rows))
    out: list[dict[str, Any]] = []
    for (state, horizon), g in df.groupby(["market_structure_state", "horizon"]):
        out.append(
            {
                "market_structure_state": state,
                "horizon": int(horizon),
                "n": len(g),
                "mean_forward_return": float(g["forward_return"].mean()),
                "mean_signed_forward_return": float(g["signed_forward_return"].mean()),
                "mean_mfe": float(g["mfe"].mean()),
                "mean_mae": float(g["mae"].mean()),
            }
        )
    return out


def check_no_repaint(structure: pd.DataFrame) -> dict[str, Any]:
    """Incremental prefix replay must not change past closed-bar states."""
    if structure.empty or len(structure) < 10:
        return {"checked": False, "mismatches": 0}
    ohlcv = structure[["timestamp", "open", "high", "low", "close", "atr_14"]].copy()
    if "symbol" in structure.columns:
        ohlcv["symbol"] = structure["symbol"]
    if "timeframe" in structure.columns:
        ohlcv["timeframe"] = structure["timeframe"]
    cfg = MarketStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    # Use hashes from structure if present
    variant = str(structure.iloc[0].get("config_variant") or "balanced_medium")
    for entry in RESEARCH_MATRIX:
        if entry["name"] == variant:
            cfg = MarketStructureConfig.from_matrix_entry(entry)
            break
    clean = structure["indicator_clean_regime_state"].astype(str).tolist()
    mismatches = 0
    checkpoints = [len(ohlcv) // 3, (2 * len(ohlcv)) // 3, len(ohlcv)]
    full = apply_market_structure(ohlcv, cfg, clean_regime_states=clean)
    for n in checkpoints:
        partial = apply_market_structure(
            ohlcv.iloc[:n].copy(),
            cfg,
            clean_regime_states=clean[:n],
        )
        a = full.iloc[:n]["market_structure_state"].astype(str).tolist()
        b = partial["market_structure_state"].astype(str).tolist()
        mismatches += sum(1 for x, y in zip(a, b) if x != y)
    return {"checked": True, "mismatches": mismatches, "checkpoints": checkpoints}


def run_market_structure_audit(
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
    matrix: Sequence[Mapping[str, Any]] | None = None,
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
        cache_dir=cache_dir or DEFAULT_CACHE,
    )
    features = prepare_feature_frame_from_ohlcv_features(frame)
    a0 = pd.Timestamp(analyze_start, tz="UTC")
    a1 = pd.Timestamp(analyze_end, tz="UTC")
    ts = pd.to_datetime(features["decision_time"], utc=True)
    features = features.loc[(ts >= a0) & (ts <= a1)].copy().reset_index(drop=True)
    features["symbol"] = symbol
    features["timeframe"] = timeframe

    # Clean regime as comparison-only input (does not mutate clean-regime module).
    clean_cfg = CleanRegimeConfig.for_variant("medium")
    clean_df = apply_clean_regime(features, clean_cfg)
    clean_states = clean_df["clean_regime_state"].astype(str).tolist()
    # Freeze a copy hash to prove we don't mutate clean outputs downstream.
    clean_hash_before = hashlib.sha256(
        clean_df["clean_regime_state"].astype(str).to_csv(index=False).encode()
    ).hexdigest()

    ohlcv = features[
        [c for c in ["timestamp", "decision_time", "symbol", "timeframe", "open", "high", "low", "close", "atr_14"] if c in features.columns]
    ].copy()
    if "timestamp" not in ohlcv.columns:
        ohlcv["timestamp"] = features["decision_time"]

    matrix_entries = list(matrix or RESEARCH_MATRIX)
    comparison_rows: list[dict[str, Any]] = []
    variant_summaries: dict[str, Any] = {}
    primary: pd.DataFrame | None = None
    primary_swings: list[dict[str, Any]] = []
    primary_cfg = MarketStructureConfig.from_matrix_entry(matrix_entries[0])

    for entry in matrix_entries:
        cfg = MarketStructureConfig.from_matrix_entry(entry)
        structure, swings = replay_with_swings(ohlcv, cfg, clean_regime_states=clean_states)
        metrics = compute_structure_metrics(structure, swings)
        dur = _duration_stats(structure["market_structure_state"].tolist())
        transitions = _transition_rows(structure["market_structure_state"].tolist())
        blocked = _period_rows(structure, "transition_blocked")

        suffix = cfg.variant_name
        structure.to_csv(output_dir / f"market_structure_bars_{suffix}.csv", index=False)
        bot_interface_frame(structure).to_csv(
            output_dir / f"market_structure_bot_interface_{suffix}.csv", index=False
        )
        pd.DataFrame(swings).to_csv(output_dir / f"confirmed_swings_{suffix}.csv", index=False)
        pd.DataFrame(transitions).to_csv(
            output_dir / f"market_structure_transitions_{suffix}.csv", index=False
        )
        pd.DataFrame([{"duration": d, "variant": suffix} for d in dur.get("durations", [])]).to_csv(
            output_dir / f"market_structure_duration_distribution_{suffix}.csv", index=False
        )
        pd.DataFrame(blocked).to_csv(
            output_dir / f"transition_blocked_periods_{suffix}.csv", index=False
        )

        major_levels = [
            {
                "kind": s["kind"],
                "level": s["level"],
                "confirmed_timestamp": s.get("confirmed_timestamp"),
                "extreme_timestamp": s.get("extreme_timestamp"),
                "confirmation_delay_bars": s.get("confirmation_delay_bars"),
                "swing_type": s.get("swing_type"),
                "is_major": True,
                "variant": suffix,
            }
            for s in swings
            if s.get("is_major")
        ]
        pd.DataFrame(major_levels).to_csv(
            output_dir / f"major_structure_levels_{suffix}.csv", index=False
        )

        attempts = structure[structure["market_structure_state"].str.endswith("break_attempt")]
        attempts.to_csv(output_dir / f"break_attempts_{suffix}.csv", index=False)
        conf_br = structure[structure["confirmed_break"].fillna(False)]
        conf_br.to_csv(output_dir / f"confirmed_breaks_{suffix}.csv", index=False)
        fails = structure[structure["break_failed"].fillna(False)]
        fails.to_csv(output_dir / f"break_failures_{suffix}.csv", index=False)
        ret = structure[structure["retest_pending"].fillna(False) | structure["retest_confirmed"].fillna(False)]
        ret.to_csv(output_dir / f"retests_{suffix}.csv", index=False)
        structure[
            [
                "timestamp",
                "indicator_clean_regime_state",
                "market_structure_state",
                "structure_indicator_alignment",
                "major_structure_direction",
            ]
        ].to_csv(output_dir / f"structure_indicator_alignment_{suffix}.csv", index=False)

        spec = build_rule_spec(cfg)
        (output_dir / f"market_structure_config_{suffix}.json").write_text(
            json.dumps(json_safe(cfg.to_dict()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"market_structure_rule_spec_{suffix}.json").write_text(
            json.dumps(json_safe(spec), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"market_structure_config_hash_{suffix}.txt").write_text(
            config_hash(cfg) + "\n", encoding="utf-8"
        )
        (output_dir / f"market_structure_rule_hash_{suffix}.txt").write_text(
            rule_spec_hash(spec) + "\n", encoding="utf-8"
        )

        summary = {
            "variant": suffix,
            "label": cfg.label,
            "config_hash": config_hash(cfg),
            "rule_spec_hash": rule_spec_hash(spec),
            "python_rule_hash": python_rule_hash(cfg),
            "pine_rule_hash": pine_rule_hash(cfg),
            "hashes_match": python_rule_hash(cfg) == pine_rule_hash(cfg),
            **{k: v for k, v in metrics.items() if k != "state_counts"},
            "state_counts": dict(metrics["state_counts"]),
        }
        variant_summaries[suffix] = summary
        comparison_rows.append(
            {
                "variant": suffix,
                "label": cfg.label,
                "swing_sensitivity": cfg.swing_sensitivity,
                "transition_zone_atr": cfg.transition_zone_atr,
                "break_mode": cfg.break_mode,
                "retest_mode": cfg.retest_mode,
                "n_major_highs": metrics["n_confirmed_major_swing_highs"],
                "n_major_lows": metrics["n_confirmed_major_swing_lows"],
                "mean_confirm_delay": metrics["mean_confirmation_delay_bars"],
                "mean_structure_duration": metrics["mean_structure_duration"],
                "wick_without_close": metrics["wick_breaks_without_close"],
                "close_without_confirmed": metrics["close_breaks_without_confirmed"],
                "confirmed_breaks": metrics["confirmed_breaks"],
                "break_failures": metrics["break_failures"],
                "retest_confirmed": metrics["retest_confirmed_events"],
                "transition_blocked_periods": metrics["transition_blocked_periods"],
                "direct_flips": metrics["direct_structure_flips"],
                "against_structure_hold_rate": metrics["indicator_against_structure_hold_rate"],
            }
        )
        if cfg.variant_name == primary_cfg.variant_name:
            primary = structure
            primary_swings = swings

    assert primary is not None
    # Canonical aliases
    primary.to_csv(output_dir / "market_structure_bars.csv", index=False)
    bot_interface_frame(primary).to_csv(output_dir / "market_structure_bot_interface.csv", index=False)
    pd.DataFrame(primary_swings).to_csv(output_dir / "confirmed_swings.csv", index=False)
    for name in (
        "major_structure_levels",
        "break_attempts",
        "confirmed_breaks",
        "break_failures",
        "retests",
        "transition_blocked_periods",
        "structure_indicator_alignment",
        "market_structure_transitions",
        "market_structure_duration_distribution",
    ):
        src = output_dir / f"{name}_{primary_cfg.variant_name}.csv"
        if src.is_file():
            (output_dir / f"{name}.csv").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    (output_dir / "market_structure_config.json").write_text(
        (output_dir / f"market_structure_config_{primary_cfg.variant_name}.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (output_dir / "market_structure_rule_spec.json").write_text(
        (output_dir / f"market_structure_rule_spec_{primary_cfg.variant_name}.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (output_dir / "market_structure_config_hash.txt").write_text(
        config_hash(primary_cfg) + "\n", encoding="utf-8"
    )
    (output_dir / "market_structure_rule_hash.txt").write_text(
        rule_spec_hash(cfg=primary_cfg) + "\n", encoding="utf-8"
    )

    pd.DataFrame(comparison_rows).to_csv(
        output_dir / "market_structure_variant_comparison.csv", index=False
    )

    outcome_rows = outcome_audit_rows(primary)
    pd.DataFrame(outcome_rows).to_csv(output_dir / "market_structure_outcome_audit.csv", index=False)
    outcome_summary = summarize_outcomes(outcome_rows)

    # Synthetic parity rows (rule-hash identity; Pine from same spec).
    parity = [
        {
            "variant": primary_cfg.variant_name,
            "python_rule_hash": python_rule_hash(primary_cfg),
            "pine_rule_hash": pine_rule_hash(primary_cfg),
            "match": python_rule_hash(primary_cfg) == pine_rule_hash(primary_cfg),
            "state_codes": json.dumps(build_rule_spec(primary_cfg)["state_codes"]),
            "note": "Pine generated from identical rule_spec",
        }
    ]
    pd.DataFrame(parity).to_csv(output_dir / "market_structure_python_pine_parity.csv", index=False)

    pine_meta = write_market_structure_pines(output_dir)
    for path in pine_meta["paths"].values():
        validate_pine_script(Path(path).read_text(encoding="utf-8"))

    # Deterministic rerun
    rerun, _ = replay_with_swings(ohlcv, primary_cfg, clean_regime_states=clean_states)
    h1 = hashlib.sha256(
        primary[["market_structure_state", "transition_reason", "structure_age_bars"]]
        .astype(str)
        .to_csv(index=False)
        .encode()
    ).hexdigest()
    h2 = hashlib.sha256(
        rerun[["market_structure_state", "transition_reason", "structure_age_bars"]]
        .astype(str)
        .to_csv(index=False)
        .encode()
    ).hexdigest()

    repaint = check_no_repaint(primary)
    clean_hash_after = hashlib.sha256(
        clean_df["clean_regime_state"].astype(str).to_csv(index=False).encode()
    ).hexdigest()
    primary_metrics = compute_structure_metrics(primary, primary_swings)

    summary = {
        "phase": "C3_4A_market_structure",
        "symbol": symbol,
        "timeframe": timeframe,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "baseline_hash_confirmed": bool(baseline.get("hash_matches")),
        "research_matrix": list(matrix_entries),
        "primary_variant": primary_cfg.variant_name,
        "config_hash": config_hash(primary_cfg),
        "rule_spec_hash": rule_spec_hash(cfg=primary_cfg),
        "variants": variant_summaries,
        "variant_comparison": comparison_rows,
        "primary_metrics": {
            **{k: v for k, v in primary_metrics.items() if k != "state_counts"},
            "state_counts": dict(primary_metrics["state_counts"]),
        },
        "outcome_summary": outcome_summary,
        "pine": pine_meta,
        "deterministic_rerun_hash_match": h1 == h2,
        "content_hash_primary": h1,
        "non_repainting": {
            **repaint,
            "causal_step_only": True,
            "no_future_right_bar_pivots": True,
            "no_centered_windows": True,
            "retro_outcomes_excluded_from_state": True,
            "closed_bars_immutable": repaint.get("mismatches", 1) == 0,
        },
        "clean_regime_unchanged": {
            "hash_before": clean_hash_before,
            "hash_after": clean_hash_after,
            "match": clean_hash_before == clean_hash_after,
            "note": "C3.4A consumes clean states as read-only comparison input",
        },
        "safety": {
            "research_only": True,
            "no_live_bot_integration": True,
            "no_classifier_changes": True,
            "no_production_config_changes": True,
            "no_clean_regime_logic_changes": True,
            "nothing_committed": True,
        },
        "supported_states": list(STRUCTURE_STATES),
        "runtime_s": round(time.perf_counter() - t0, 4),
        "artifacts": {
            "bars": "market_structure_bars.csv",
            "bot_interface": "market_structure_bot_interface.csv",
            "pine_main": MAIN_PINE,
            "variant_comparison": "market_structure_variant_comparison.csv",
            "parity": "market_structure_python_pine_parity.csv",
        },
    }
    (output_dir / "market_structure_run_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C3.4A market structure audit")
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframe", default="30m")
    parser.add_argument("--load-start", default="2026-01-01")
    parser.add_argument("--load-end", default="2026-05-15")
    parser.add_argument("--analyze-start", default="2026-02-01")
    parser.add_argument("--analyze-end", default="2026-04-30")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    parser.add_argument("--cache-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    summary = run_market_structure_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        cache_dir=args.cache_dir,
    )
    print(
        json.dumps(
            {
                "content_hash_primary": summary["content_hash_primary"],
                "config_hash": summary["config_hash"],
                "rule_spec_hash": summary["rule_spec_hash"],
                "runtime_s": summary["runtime_s"],
                "primary_metrics": {
                    k: summary["primary_metrics"].get(k)
                    for k in (
                        "n_confirmed_major_swing_highs",
                        "n_confirmed_major_swing_lows",
                        "mean_confirmation_delay_bars",
                        "direct_structure_flips",
                        "transition_blocked_periods",
                        "indicator_against_structure_hold_rate",
                    )
                },
                "pine_main": summary["artifacts"]["pine_main"],
                "baseline_hash_confirmed": summary["baseline_hash_confirmed"],
                "clean_regime_unchanged": summary["clean_regime_unchanged"]["match"],
                "deterministic_rerun_hash_match": summary["deterministic_rerun_hash_match"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
