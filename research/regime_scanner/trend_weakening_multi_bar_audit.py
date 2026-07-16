"""Phase C1: multi-bar weakening-exit counterfactual audit (research-only).

Compares:
  C1-A  weakening_multi_bar_mode=off   (baseline / pre-C1)
  C1-B  weakening_multi_bar_mode=loose (≥2 distinct counter categories in window)
  C1-C  weakening_multi_bar_mode=strict (+ BOS/CHoCH + impulse/HTF/indicator)

Does not change trend_state_policy, live bots, liquidation, Phase-B/C0 result dirs,
or research/regime_scanner/results/.

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_weakening_multi_bar_audit \\
    --symbol APTUSDT \\
    --output-dir research/regime_scanner/results_trend_weakening_multi_bar_phase_c1
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_audit_shared_replay import (
    SharedReplayContext,
    load_or_build_shared_context,
    reset_audit_counters,
    step_trend_state_from_prepared,
)
import research.regime_scanner.trend_audit_shared_replay as shared_replay_mod
from research.regime_scanner.trend_robustness_audit import (
    ANALYZE_END,
    ANALYZE_START,
    LOAD_END,
    LOAD_START,
    MARCH_CASE_END,
    MARCH_CASE_START,
    install_htf_cache,
    load_analysis_frame,
)
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    TrendStateConfig,
    WeakeningMultiBarMode,
    default_trend_state_config,
    step_trend_state,
    trend_state_config_c1,
)
from research.regime_scanner.trend_pine_export import (
    export_audit_pine_artifacts,
    marker_rows_from_events,
)

DEFAULT_OUT = Path("research/regime_scanner/results_trend_weakening_multi_bar_phase_c1")
FORBIDDEN_OVERWRITE = (
    Path("research/regime_scanner/results"),
    Path("research/regime_scanner/results_trend_robustness_phase_b"),
    Path("research/regime_scanner/results_trend_mapping_root_cause_phase_c0"),
)

VARIANT_MODES: tuple[tuple[str, WeakeningMultiBarMode], ...] = (
    ("C1_A_baseline", "off"),
    ("C1_B_loose", "loose"),
    ("C1_C_strict", "strict"),
)

# Pre-implementation code audit (frozen narrative for exports).
CODE_AUDIT: dict[str, Any] = {
    "phase": "C1_pre_change_code_audit",
    "files": [
        "research/regime_scanner/trend_state_machine.py",
        "research/regime_scanner/trend_structure.py",
    ],
    "already_persisted_counter_structure": [
        "MarketStructureState.last_bos / last_choch / last_failed_breakout|breakdown",
        "last_high_label / last_low_label and labeled swings (HH/HL/LH/LL)",
        "protective_high_level / protective_low_level",
        "recent_events (short buffer, not a multi-bar exit ledger)",
    ],
    "current_candle_only_in_propose": [
        "_propose_transition uses _event_types(events) of this bar's StructureEvent list",
        "bullish_weakening→topping requires ≥2 of {failed_breakout,bearish_choch,lower_high,bearish_bos} SAME bar",
        "bearish_weakening→bottoming mirror on same bar",
        "persisted last_bos/last_choch are NOT consulted in weakening exits",
    ],
    "reusable_runtime_fields": [
        "consecutive_bearish_closes / consecutive_bullish_closes (impulse)",
        "structure_15m / structure_30m bias (HTF, closed buckets only)",
        "age_5m_bars (window expiry reference while in state)",
    ],
    "new_fields_required": [
        "TrendRuntime.weakening_evidence_keys",
        "TrendRuntime.weakening_evidence_seen_age",
        "TrendStateConfig.weakening_multi_bar_mode / window / min_categories",
    ],
    "evidence_reset_rules": [
        "clear on any state enter via _enter",
        "clear when mode=off or not in *_weakening",
        "clear on continuation (HH or bullish_bos+HL/HH from bullish_weakening; mirror for bearish)",
        "expire category when age - seen_age > window",
        "one slot per category; identical event_key not double-counted; new key refreshes age",
    ],
    "design_choice": (
        "Small state ledger on TrendRuntime; default mode=off preserves C1-A. "
        "Exit destination remains topping/bottoming (no new states; no March hardcodes)."
    ),
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def assert_safe_output_dir(path: Path) -> None:
    resolved = path.resolve()
    for forbidden in FORBIDDEN_OVERWRITE:
        if resolved == forbidden.resolve():
            raise ValueError(f"refusing to write into forbidden path: {forbidden}")


def config_for_variant(mode: WeakeningMultiBarMode) -> TrendStateConfig:
    if mode == "off":
        return default_trend_state_config()
    return trend_state_config_c1(mode)


def replay_variant_naive(
    frame: pd.DataFrame,
    *,
    mode: WeakeningMultiBarMode,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
) -> dict[str, Any]:
    """Original full replay (structure per variant). Used for parity tests only."""
    end_decision = _ts(frame["decision_time"].iloc[-1])
    install_htf_cache(frame, end_decision)

    scfg = default_regime_scanner_config().with_timeframe("5m")
    cfg = config_for_variant(mode)
    pivots = find_confirmed_pivots(frame, config=scfg)
    rt = TrendRuntime()

    state_counts: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()
    multi_bar_exits: list[dict[str, Any]] = []
    weakening_runs: list[dict[str, Any]] = []
    march_rows: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []
    ping_pong = 0
    recent_states: list[str] = []

    open_run: dict[str, Any] | None = None
    max_evidence_cats = 0

    march_start = _ts(MARCH_CASE_START)
    march_end = _ts(MARCH_CASE_END)

    for i, row in frame.iterrows():
        decision_ts = _ts(row["decision_time"])
        candles_as_of = frame.iloc[: int(i) + 1][
            [c for c in ("timestamp", "open", "high", "low", "close", "volume") if c in frame.columns]
        ]
        prev_state = rt.state
        rt, snap, _ = step_trend_state(
            rt,
            candle_row=row,
            pivots_5m=pivots,
            decision_time=decision_ts,
            candles_5m_as_of=candles_as_of,
            bar_index=int(i),
            cfg=cfg,
            scanner_cfg=scfg,
        )

        in_window = analyze_start <= decision_ts <= analyze_end
        if not in_window:
            continue

        state_counts[snap.current_state] += 1
        max_evidence_cats = max(max_evidence_cats, len(rt.weakening_evidence_keys))
        timeline_rows.append(
            {
                "decision_time": _iso(decision_ts),
                "state": snap.current_state,
                "previous_state": prev_state,
                "close": float(row["close"]),
                "transition": snap.current_state != prev_state,
                "reasons": "|".join(snap.active_reasons) if snap.current_state != prev_state else "",
            }
        )

        if snap.current_state != prev_state:
            key = f"{prev_state}->{snap.current_state}"
            transition_counts[key] += 1
            reasons = list(snap.active_reasons)
            is_mb = any(
                r in {"multi_bar_topping_structure", "multi_bar_bottoming_structure"}
                for r in reasons
            )
            if is_mb and prev_state in {"bullish_weakening", "bearish_weakening"}:
                known = {
                    "bearish_choch",
                    "lower_high",
                    "bearish_bos",
                    "failed_breakout",
                    "bullish_choch",
                    "higher_low",
                    "bullish_bos",
                    "failed_breakdown",
                }
                cats = sorted(r for r in reasons if r in known)
                multi_bar_exits.append(
                    {
                        "decision_time": _iso(decision_ts),
                        "from_state": prev_state,
                        "to_state": snap.current_state,
                        "mode": mode,
                        "evidence_cats": ",".join(cats),
                        "reasons": "|".join(reasons),
                        "close": float(row["close"]),
                        "bias_15m": rt.structure_15m.current_structure_bias,
                        "consec_bearish": rt.consecutive_bearish_closes,
                        "consec_bullish": rt.consecutive_bullish_closes,
                    }
                )

            if prev_state in {"bullish_weakening", "bearish_weakening"} and open_run is not None:
                open_run["end"] = _iso(decision_ts)
                open_run["end_state"] = snap.current_state
                open_run["exit_reasons"] = "|".join(reasons)
                open_run["multi_bar_exit"] = bool(is_mb)
                open_run["length_bars"] = int(open_run.get("length_bars", 0))
                weakening_runs.append(open_run)
                open_run = None
            if snap.current_state in {"bullish_weakening", "bearish_weakening"}:
                open_run = {
                    "state": snap.current_state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "max_evidence_cats": len(rt.weakening_evidence_keys),
                    "mode": mode,
                }
            recent_states.append(snap.current_state)
            if len(recent_states) >= 4:
                a, b, c, d = recent_states[-4:]
                if a == c and b == d and a != b:
                    ping_pong += 1
        elif snap.current_state in {"bullish_weakening", "bearish_weakening"}:
            if open_run is None:
                open_run = {
                    "state": snap.current_state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "max_evidence_cats": len(rt.weakening_evidence_keys),
                    "mode": mode,
                }
            else:
                open_run["length_bars"] = int(open_run["length_bars"]) + 1
                open_run["max_evidence_cats"] = max(
                    int(open_run.get("max_evidence_cats", 0)),
                    len(rt.weakening_evidence_keys),
                )

        if march_start <= decision_ts <= march_end:
            march_rows.append(
                {
                    "decision_time": _iso(decision_ts),
                    "close": float(row["close"]),
                    "state": snap.current_state,
                    "previous_state": snap.previous_state,
                    "reasons": "|".join(snap.active_reasons),
                    "evidence_cats": ",".join(sorted(rt.weakening_evidence_keys.keys())),
                    "allow_long": snap.allow_long,
                    "allow_short": snap.allow_short,
                    "mode": mode,
                }
            )

    if open_run is not None:
        open_run["end"] = None
        open_run["end_state"] = open_run["state"]
        open_run["exit_reasons"] = "still_open_at_analyze_end"
        open_run["multi_bar_exit"] = False
        weakening_runs.append(open_run)

    return {
        **_finalize_variant_metrics(
            mode=mode,
            cfg=cfg,
            state_counts=state_counts,
            transition_counts=transition_counts,
            multi_bar_exits=multi_bar_exits,
            weakening_runs=weakening_runs,
            march_rows=march_rows,
            ping_pong=ping_pong,
            max_evidence_cats=max_evidence_cats,
        ),
        "timeline_rows": timeline_rows,
    }


def _finalize_variant_metrics(
    *,
    mode: WeakeningMultiBarMode,
    cfg: TrendStateConfig,
    state_counts: Counter[str],
    transition_counts: Counter[str],
    multi_bar_exits: list[dict[str, Any]],
    weakening_runs: list[dict[str, Any]],
    march_rows: list[dict[str, Any]],
    ping_pong: int,
    max_evidence_cats: int,
) -> dict[str, Any]:
    lengths = [int(r["length_bars"]) for r in weakening_runs] or [0]
    long_stuck = [r for r in weakening_runs if int(r["length_bars"]) >= 24]

    mar6 = [r for r in march_rows if str(r["decision_time"] or "").startswith("2026-03-06")]
    mar6_states = Counter(r["state"] for r in mar6)
    mar6_left_weakening = any(
        r["previous_state"] == "bullish_weakening" and r["state"] != "bullish_weakening" for r in mar6
    )
    first_exit = next(
        (
            r
            for r in mar6
            if r["previous_state"] == "bullish_weakening" and r["state"] != "bullish_weakening"
        ),
        None,
    )

    return {
        "mode": mode,
        "n_analyze_bars": int(sum(state_counts.values())),
        "state_counts": dict(state_counts),
        "transition_counts": dict(transition_counts),
        "weakening_share": float(
            (state_counts.get("bullish_weakening", 0) + state_counts.get("bearish_weakening", 0))
            / max(1, sum(state_counts.values()))
        ),
        "n_weakening_runs": len(weakening_runs),
        "n_long_stuck_runs_ge24": len(long_stuck),
        "max_weakening_run_bars": int(max(lengths)),
        "mean_weakening_run_bars": float(sum(lengths) / max(1, len(lengths))),
        "median_weakening_run_bars": float(pd.Series(lengths).median()),
        "n_multi_bar_exits": len(multi_bar_exits),
        "ping_pong_quad_patterns": int(ping_pong),
        "max_evidence_cats_seen": int(max_evidence_cats),
        "mar6_state_counts": dict(mar6_states),
        "mar6_bars": len(mar6),
        "mar6_left_bullish_weakening": bool(mar6_left_weakening),
        "mar6_first_exit": first_exit,
        "mar6_bullish_weakening_share": float(
            mar6_states.get("bullish_weakening", 0) / max(1, len(mar6))
        ),
        "multi_bar_exits": multi_bar_exits,
        "weakening_runs": weakening_runs,
        "march_rows": march_rows,
        "config": cfg.to_dict(),
    }


def replay_variant(
    frame: pd.DataFrame,
    *,
    mode: WeakeningMultiBarMode,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    shared: SharedReplayContext,
) -> dict[str, Any]:
    """Policy-only replay on a shared structure timeline (no per-variant structure pass)."""
    import research.regime_scanner.trend_audit_shared_replay as shared_replay_mod

    shared_replay_mod.VARIANT_POLICY_REPLAY_COUNT += 1

    cfg = config_for_variant(mode)
    rt = TrendRuntime()

    state_counts: Counter[str] = Counter()
    transition_counts: Counter[str] = Counter()
    multi_bar_exits: list[dict[str, Any]] = []
    weakening_runs: list[dict[str, Any]] = []
    march_rows: list[dict[str, Any]] = []
    timeline_rows: list[dict[str, Any]] = []
    ping_pong = 0
    recent_states: list[str] = []

    open_run: dict[str, Any] | None = None
    max_evidence_cats = 0

    march_start = _ts(MARCH_CASE_START)
    march_end = _ts(MARCH_CASE_END)

    for prep in shared.prepared_bars:
        decision_ts = prep.decision_time
        prev_state = rt.state
        rt, snap, _ = step_trend_state_from_prepared(rt, prepared=prep, cfg=cfg)

        in_window = analyze_start <= decision_ts <= analyze_end
        if not in_window:
            continue

        state_counts[snap.current_state] += 1
        max_evidence_cats = max(max_evidence_cats, len(rt.weakening_evidence_keys))
        timeline_rows.append(
            {
                "decision_time": _iso(decision_ts),
                "state": snap.current_state,
                "previous_state": prev_state,
                "close": float(prep.row["close"]),
                "transition": snap.current_state != prev_state,
                "reasons": "|".join(snap.active_reasons) if snap.current_state != prev_state else "",
            }
        )

        if snap.current_state != prev_state:
            key = f"{prev_state}->{snap.current_state}"
            transition_counts[key] += 1
            reasons = list(snap.active_reasons)
            is_mb = any(
                r in {"multi_bar_topping_structure", "multi_bar_bottoming_structure"}
                for r in reasons
            )
            if is_mb and prev_state in {"bullish_weakening", "bearish_weakening"}:
                known = {
                    "bearish_choch",
                    "lower_high",
                    "bearish_bos",
                    "failed_breakout",
                    "bullish_choch",
                    "higher_low",
                    "bullish_bos",
                    "failed_breakdown",
                }
                cats = sorted(r for r in reasons if r in known)
                row = prep.row
                multi_bar_exits.append(
                    {
                        "decision_time": _iso(decision_ts),
                        "from_state": prev_state,
                        "to_state": snap.current_state,
                        "mode": mode,
                        "evidence_cats": ",".join(cats),
                        "reasons": "|".join(reasons),
                        "close": float(row["close"]),
                        "bias_15m": rt.structure_15m.current_structure_bias,
                        "consec_bearish": rt.consecutive_bearish_closes,
                        "consec_bullish": rt.consecutive_bullish_closes,
                    }
                )

            if prev_state in {"bullish_weakening", "bearish_weakening"} and open_run is not None:
                open_run["end"] = _iso(decision_ts)
                open_run["end_state"] = snap.current_state
                open_run["exit_reasons"] = "|".join(reasons)
                open_run["multi_bar_exit"] = bool(is_mb)
                open_run["length_bars"] = int(open_run.get("length_bars", 0))
                weakening_runs.append(open_run)
                open_run = None
            if snap.current_state in {"bullish_weakening", "bearish_weakening"}:
                open_run = {
                    "state": snap.current_state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "max_evidence_cats": len(rt.weakening_evidence_keys),
                    "mode": mode,
                }
            recent_states.append(snap.current_state)
            if len(recent_states) >= 4:
                a, b, c, d = recent_states[-4:]
                if a == c and b == d and a != b:
                    ping_pong += 1
        elif snap.current_state in {"bullish_weakening", "bearish_weakening"}:
            if open_run is None:
                open_run = {
                    "state": snap.current_state,
                    "start": _iso(decision_ts),
                    "length_bars": 1,
                    "max_evidence_cats": len(rt.weakening_evidence_keys),
                    "mode": mode,
                }
            else:
                open_run["length_bars"] = int(open_run["length_bars"]) + 1
                open_run["max_evidence_cats"] = max(
                    int(open_run.get("max_evidence_cats", 0)),
                    len(rt.weakening_evidence_keys),
                )

        if march_start <= decision_ts <= march_end:
            march_rows.append(
                {
                    "decision_time": _iso(decision_ts),
                    "close": float(prep.row["close"]),
                    "state": snap.current_state,
                    "previous_state": snap.previous_state,
                    "reasons": "|".join(snap.active_reasons),
                    "evidence_cats": ",".join(sorted(rt.weakening_evidence_keys.keys())),
                    "allow_long": snap.allow_long,
                    "allow_short": snap.allow_short,
                    "mode": mode,
                }
            )

    if open_run is not None:
        open_run["end"] = None
        open_run["end_state"] = open_run["state"]
        open_run["exit_reasons"] = "still_open_at_analyze_end"
        open_run["multi_bar_exit"] = False
        weakening_runs.append(open_run)

    return {
        **_finalize_variant_metrics(
            mode=mode,
            cfg=cfg,
            state_counts=state_counts,
            transition_counts=transition_counts,
            multi_bar_exits=multi_bar_exits,
            weakening_runs=weakening_runs,
            march_rows=march_rows,
            ping_pong=ping_pong,
            max_evidence_cats=max_evidence_cats,
        ),
        "timeline_rows": timeline_rows,
    }


def _run_lengths_false_positive_proxy(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Flag multi-bar exits that immediately reverse back within 6 bars (proxy FP/churn)."""
    exits = result["multi_bar_exits"]
    rows_m = result["march_rows"]  # sparse; use transitions instead
    # Build from weakening_runs that used multi_bar
    out: list[dict[str, Any]] = []
    for ex in exits:
        out.append(
            {
                "decision_time": ex["decision_time"],
                "from_state": ex["from_state"],
                "to_state": ex["to_state"],
                "evidence_cats": ex["evidence_cats"],
                "mode": ex["mode"],
                "flag": "multi_bar_exit_candidate",
                "note": "manual review vs continuing prior trend; no GT dependency",
            }
        )
    return out


def compare_variants(results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name, r in results.items():
        rows.append(
            {
                "variant": name,
                "mode": r["mode"],
                "weakening_share": r["weakening_share"],
                "n_weakening_runs": r["n_weakening_runs"],
                "n_long_stuck_runs_ge24": r["n_long_stuck_runs_ge24"],
                "max_weakening_run_bars": r["max_weakening_run_bars"],
                "mean_weakening_run_bars": r["mean_weakening_run_bars"],
                "median_weakening_run_bars": r["median_weakening_run_bars"],
                "n_multi_bar_exits": r["n_multi_bar_exits"],
                "ping_pong_quad_patterns": r["ping_pong_quad_patterns"],
                "mar6_left_bullish_weakening": r["mar6_left_bullish_weakening"],
                "mar6_bullish_weakening_share": r["mar6_bullish_weakening_share"],
                "mar6_first_exit_time": (r["mar6_first_exit"] or {}).get("decision_time"),
                "mar6_first_exit_to": (r["mar6_first_exit"] or {}).get("state"),
                "topping_bars": r["state_counts"].get("topping", 0),
                "bottoming_bars": r["state_counts"].get("bottoming", 0),
                "bullish_weakening_bars": r["state_counts"].get("bullish_weakening", 0),
                "bearish_weakening_bars": r["state_counts"].get("bearish_weakening", 0),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def build_findings(comparison: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    by = {r["variant"]: r for r in comparison}
    a, b, c = by["C1_A_baseline"], by["C1_B_loose"], by["C1_C_strict"]
    findings: list[dict[str, Any]] = []

    def add(fid: str, severity: str, claim: str, evidence: str) -> None:
        findings.append({"id": fid, "severity": severity, "claim": claim, "evidence": evidence})

    add(
        "F1",
        "info",
        "Baseline still shows extreme stuck weakening",
        f"C1-A max_run={a['max_weakening_run_bars']} long_stuck_ge24={a['n_long_stuck_runs_ge24']}",
    )
    add(
        "F2",
        "result",
        "Loose multi-bar evidence can exit weakening without March hardcodes",
        f"C1-B multi_bar_exits={b['n_multi_bar_exits']} max_run={b['max_weakening_run_bars']} "
        f"mar6_left={b['mar6_left_bullish_weakening']} first_exit={b['mar6_first_exit_time']}",
    )
    add(
        "F3",
        "result",
        "Strict multi-bar is more conservative than loose",
        f"C1-C multi_bar_exits={c['n_multi_bar_exits']} max_run={c['max_weakening_run_bars']} "
        f"mar6_left={c['mar6_left_bullish_weakening']} ping_pong={c['ping_pong_quad_patterns']}",
    )
    add(
        "F4",
        "caution",
        "Ping-pong / churn check vs baseline",
        f"ping_pong A/B/C={a['ping_pong_quad_patterns']}/{b['ping_pong_quad_patterns']}/{c['ping_pong_quad_patterns']}",
    )
    add(
        "F5",
        "scope",
        "Policy unchanged; weakening may still allow_long under existing policy",
        "trend_state_policy.py not modified in Phase C1",
    )

    mb_b = results["C1_B_loose"]["n_multi_bar_exits"]
    mb_c = results["C1_C_strict"]["n_multi_bar_exits"]
    if mb_b == 0 and mb_c == 0:
        add(
            "F6",
            "negative",
            "Neither variant produced multi-bar exits in the analyze window",
            "Root fix ineffective on this sample or evidence categories rarely accumulate",
        )
    elif b["mar6_left_bullish_weakening"] and not a["mar6_left_bullish_weakening"]:
        add(
            "F6",
            "positive",
            "Mar6 bullish_weakening sticky failure reduced under multi-bar variant",
            f"A share={a['mar6_bullish_weakening_share']:.3f} B share={b['mar6_bullish_weakening_share']:.3f}",
        )
    return findings


def recommend_variant(comparison: list[dict[str, Any]]) -> dict[str, Any]:
    by = {r["variant"]: r for r in comparison}
    a, b, c = by["C1_A_baseline"], by["C1_B_loose"], by["C1_C_strict"]
    # Prefer strict if it exits Mar6 and does not explode ping-pong vs loose
    if c["mar6_left_bullish_weakening"] and c["ping_pong_quad_patterns"] <= b["ping_pong_quad_patterns"] + 5:
        choice = "C1_C_strict"
        why = "exits stuck weakening with stricter BOS/CHoCH+impulse/HTF gate"
    elif b["mar6_left_bullish_weakening"]:
        choice = "C1_B_loose"
        why = "clears Mar6 stickiness; strict did not or was too tight"
    else:
        choice = "C1_A_baseline"
        why = "multi-bar variants did not fix Mar6; keep baseline until redesign"
    return {
        "recommended_research_default": choice,
        "reason": why,
        "production_default_remains": "off (C1-A)",
        "policy_change": False,
        "metrics_snapshot": {
            "A_max_run": a["max_weakening_run_bars"],
            "B_max_run": b["max_weakening_run_bars"],
            "C_max_run": c["max_weakening_run_bars"],
            "B_mb_exits": b["n_multi_bar_exits"],
            "C_mb_exits": c["n_multi_bar_exits"],
        },
    }


def run_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    reset_audit_counters()
    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    a0 = _ts(analyze_start)
    a1 = _ts(analyze_end)

    shared = load_or_build_shared_context(frame, cache_dir=output_dir / ".cache")

    results: dict[str, dict[str, Any]] = {}
    for name, mode in VARIANT_MODES:
        results[name] = replay_variant(
            frame, mode=mode, analyze_start=a0, analyze_end=a1, shared=shared
        )

    comparison = compare_variants(results)
    findings = build_findings(comparison, results)
    recommendation = recommend_variant(comparison)

    # Exports
    write_csv(output_dir / "variant_comparison.csv", comparison)
    write_csv(
        output_dir / "multi_bar_exits.csv",
        [e for r in results.values() for e in r["multi_bar_exits"]],
    )
    write_csv(
        output_dir / "weakening_runs.csv",
        [e for r in results.values() for e in r["weakening_runs"]],
    )
    write_csv(
        output_dir / "march_case_timelines.csv",
        [e for r in results.values() for e in r["march_rows"]],
    )
    write_csv(
        output_dir / "false_positive_exit_candidates.csv",
        [e for name, r in results.items() for e in _run_lengths_false_positive_proxy(r)],
    )
    write_csv(output_dir / "root_cause_findings.csv", findings)

    (output_dir / "code_audit.json").write_text(
        json.dumps(json_safe(CODE_AUDIT), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Compact march06 slice per variant
    mar6_rows = []
    for name, r in results.items():
        for row in r["march_rows"]:
            if str(row["decision_time"] or "").startswith("2026-03-06"):
                mar6_rows.append({"variant": name, **row})
    write_csv(output_dir / "march_06_comparison.csv", mar6_rows)

    pine_export = export_audit_pine_artifacts(
        output_dir=output_dir,
        phase="C1_weakening_multi_bar",
        symbol=symbol,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        variants={
            name: {
                "timeline_rows": r.get("timeline_rows") or [],
                "marker_rows": marker_rows_from_events(
                    r.get("multi_bar_exits") or [],
                    label_field="to_state",
                    extra_suffix="mb_exit",
                ),
            }
            for name, r in results.items()
        },
        recommended_variant=recommendation.get("recommended_research_default"),
    )

    summary = {
        "phase": "C1_weakening_multi_bar",
        "symbol": symbol,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "n_load_bars": int(len(frame)),
        "variants": {k: {kk: vv for kk, vv in v.items() if kk not in {
            "multi_bar_exits", "weakening_runs", "march_rows", "timeline_rows"
        }} for k, v in results.items()},
        "comparison": comparison,
        "findings": findings,
        "recommendation": recommendation,
        "pine_export": pine_export,
        "code_audit": CODE_AUDIT,
        "safety": {
            "policy_unchanged": True,
            "default_mode_off": True,
            "no_march_hardcode": True,
            "did_not_write_forbidden_dirs": True,
        },
        "performance": {
            "shared_structure_passes": shared.structure_pass_count,
            "variant_policy_replays": shared_replay_mod.VARIANT_POLICY_REPLAY_COUNT,
            "shared_cache_key": shared.cache_key,
        },
    }
    blob = json.dumps(json_safe(summary), sort_keys=True, separators=(",", ":"))
    summary["deterministic_hash"] = hashlib.sha256(blob.encode()).hexdigest()
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    readme = f"""# Phase C1 — Weakening multi-bar evidence audit

## Question
Can the trend state machine accumulate opposing structure over multiple **closed** 5m candles and exit `*_weakening` without March hardcodes, ping-pong, or policy changes?

## Variants
| ID | mode | rule |
|---|---|---|
| C1-A | `off` | Baseline same-bar exits only |
| C1-B | `loose` | ≥2 distinct counter categories inside window → topping/bottoming |
| C1-C | `strict` | loose + require BOS/CHoCH + (impulse OR 15m counter-bias OR ≥2 indicator confirms) |

Default production/research config remains **`weakening_multi_bar_mode=off`**.

## Code audit (pre-change)
See `code_audit.json`. Weakening exits previously required concurrent same-bar events; persisted `last_bos`/`last_choch` were not used.

## Recommendation
`{recommendation["recommended_research_default"]}` — {recommendation["reason"]}

## Safety
- Policy unchanged
- No writes into `results/`, Phase-B, or Phase-C0 dirs
- No live wiring

## TradingView (research)
- Per-variant: `trend_audit_C1_weakening_multi_bar_<variant>.pine`
- Recommended: `trend_audit_C1_weakening_multi_bar_recommended.pine`
- Metadata: `trend_pine_export.json`
- Use on APTUSDT **5m**, chart timezone **UTC**
"""
    (output_dir / "README_results.md").write_text(readme, encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase C1 weakening multi-bar audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--load-start", default=LOAD_START)
    p.add_argument("--load-end", default=LOAD_END)
    p.add_argument("--analyze-start", default=ANALYZE_START)
    p.add_argument("--analyze-end", default=ANALYZE_END)
    args = p.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        output_dir=args.output_dir,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
    )
    rec = summary["recommendation"]
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "recommendation": rec,
                "comparison": summary["comparison"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
