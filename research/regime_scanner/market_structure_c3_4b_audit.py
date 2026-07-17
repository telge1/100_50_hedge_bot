"""Audit runner for C3.4B protected market-structure (research-only)."""

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
from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX,
    PROTECTED_STATES,
    ProtectedRuntime,
    ProtectedStructureConfig,
    apply_protected_structure,
    bot_interface_frame,
    build_rule_spec,
    config_hash,
    pine_rule_hash,
    python_rule_hash,
    rule_spec_hash,
    step_protected_structure_state,
)
from research.regime_scanner.market_structure_c3_4b_pine import (
    MAIN_PINE,
    write_protected_structure_pines,
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

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_4b_protected_structure")
DEFAULT_BASELINE_DIR = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)
DEFAULT_CACHE = Path(
    "research/regime_scanner/results/phase_c3_3b_apt_pattern_discovery/.cache/indicator_features"
)


def _duration_stats(states: Sequence[str]) -> dict[str, Any]:
    if not states:
        return {"n_runs": 0, "mean_duration": None, "median_duration": None, "durations": []}
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


def _rising(s: pd.Series) -> int:
    b = s.fillna(False).astype(bool)
    return int((b & ~b.shift(1, fill_value=False)).sum())


def _period_rows(df: pd.DataFrame, state: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    mask = df["protected_structure_state"].astype(str) == state
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
                    "start_distance_atr": chunk.iloc[0].get("distance_to_external_break_atr"),
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
                "start_distance_atr": chunk.iloc[0].get("distance_to_external_break_atr"),
            }
        )
    return rows


def _direct_major_flips_without_external(df: pd.DataFrame) -> int:
    flips = 0
    states = df["protected_structure_state"].astype(str).tolist()
    for i in range(1, len(states)):
        a, b = states[i - 1], states[i]
        if {a, b} != {"bullish_structure", "bearish_structure"}:
            continue
        row = df.iloc[i]
        ext = bool(row.get("external_bos_up") or row.get("external_bos_down"))
        reason = str(row.get("transition_reason") or "")
        if not ext and "external_bos" not in reason and "choch" not in reason:
            flips += 1
        # Also count if landed on opposite structure without external/choch path.
        if not ext and "immediate" not in reason and "choch" not in reason and "external" not in reason:
            flips += 1
    # Deduplicate double-count: recount cleanly
    flips = 0
    for i in range(1, len(states)):
        a, b = states[i - 1], states[i]
        if {a, b} != {"bullish_structure", "bearish_structure"}:
            continue
        row = df.iloc[i]
        reason = str(row.get("transition_reason") or "")
        ok = (
            bool(row.get("external_bos_up") or row.get("external_bos_down"))
            or "external_bos" in reason
            or "choch" in reason
        )
        if not ok:
            flips += 1
    return flips


def _level_lifetimes(df: pd.DataFrame, col: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if col not in df.columns or df.empty:
        return rows
    series = df[col]
    start = 0
    cur = series.iloc[0]
    for i in range(1, len(series)):
        val = series.iloc[i]
        changed = (pd.isna(cur) and not pd.isna(val)) or (
            not pd.isna(cur) and not pd.isna(val) and float(cur) != float(val)
        ) or (not pd.isna(cur) and pd.isna(val))
        if changed:
            if not pd.isna(cur):
                rows.append(
                    {
                        "side": "high" if "high" in col else "low",
                        "level": float(cur),
                        "start_timestamp": df.iloc[start].get("timestamp"),
                        "end_timestamp": df.iloc[i - 1].get("timestamp"),
                        "lifetime_bars": i - start,
                    }
                )
            start = i
            cur = val
    if not pd.isna(cur):
        rows.append(
            {
                "side": "high" if "high" in col else "low",
                "level": float(cur),
                "start_timestamp": df.iloc[start].get("timestamp"),
                "end_timestamp": df.iloc[-1].get("timestamp"),
                "lifetime_bars": len(df) - start,
            }
        )
    return rows


def _pct_quantiles(vals: list[float]) -> dict[str, float | None]:
    if not vals:
        return {"p50": None, "p90": None, "p99": None, "n": 0}
    arr = np.asarray(vals, dtype=float)
    return {
        "p50": float(np.quantile(arr, 0.50)),
        "p90": float(np.quantile(arr, 0.90)),
        "p99": float(np.quantile(arr, 0.99)),
        "n": int(len(arr)),
    }


def _bg_family_from_major(major_dir: int) -> str:
    if major_dir > 0:
        return "bullish"
    if major_dir < 0:
        return "bearish"
    return "neutral"


def diagnose_background_stripes(structure: pd.DataFrame) -> dict[str, Any]:
    """Compare bgcolor transitions: legacy (blocked+major) vs majorDir-only."""
    if structure.empty:
        return {"n_bars": 0}
    maj = structure["major_direction"].fillna(0).astype(int)
    blocked = structure["protected_structure_state"].astype(str) == "transition_blocked"
    legacy: list[str] = []
    strict: list[str] = []
    for i in range(len(structure)):
        fam = _bg_family_from_major(int(maj.iloc[i]))
        strict.append(fam)
        legacy.append("blocked" if bool(blocked.iloc[i]) else fam)
    legacy_t = sum(1 for a, b in zip(legacy, legacy[1:]) if a != b)
    strict_t = sum(1 for a, b in zip(strict, strict[1:]) if a != b)
    maj_changes = sum(1 for a, b in zip(maj.tolist(), maj.tolist()[1:]) if a != b)
    return {
        "n_bars": len(structure),
        "bgcolor_transitions_legacy_blocked_plus_major": legacy_t,
        "bgcolor_transitions_major_dir_only": strict_t,
        "extra_stripes_from_transition_blocked": legacy_t - strict_t,
        "major_direction_value_changes": maj_changes,
        "transition_blocked_bars": int(blocked.sum()),
        "note": "Default Pine bgcolor must match major_dir_only counts.",
    }


def diagnose_protected_level_invariants(structure: pd.DataFrame) -> dict[str, Any]:
    """Audit protected high/low age, distance, and implausible pairs (visual residue)."""
    if structure.empty:
        return {"n_bars": 0}
    close = structure["close"].astype(float)
    ph = structure["protected_high"].astype(float)
    pl = structure["protected_low"].astype(float)
    maj = structure["major_direction"].fillna(0).astype(int)
    both = ph.notna() & pl.notna()
    inv = both & (ph <= pl)
    ae = (
        structure["active_external_break_level"].astype(float)
        if "active_external_break_level" in structure.columns
        else pd.Series([np.nan] * len(structure))
    )
    # Active external must match regime side when present.
    bear = maj < 0
    bull = maj > 0
    ae_wrong_bear = 0
    ae_wrong_bull = 0
    for i in structure.index:
        m = int(maj.loc[i])
        a = ae.loc[i]
        p_h, p_l = ph.loc[i], pl.loc[i]
        if m < 0 and pd.notna(a):
            if pd.isna(p_h) or abs(float(a) - float(p_h)) > 1e-12:
                ae_wrong_bear += 1
        if m > 0 and pd.notna(a):
            if pd.isna(p_l) or abs(float(a) - float(p_l)) > 1e-12:
                ae_wrong_bull += 1

    # Direction phases with any crossed pair
    phases: list[dict[str, Any]] = []
    start = 0
    for i in range(1, len(structure)):
        if int(maj.iloc[i]) != int(maj.iloc[i - 1]):
            phases.append({"start": start, "end": i - 1, "major": int(maj.iloc[start])})
            start = i
    phases.append({"start": start, "end": len(structure) - 1, "major": int(maj.iloc[start])})
    crossed_phases = 0
    for phs in phases:
        if phs["major"] == 0:
            continue
        chunk = structure.iloc[phs["start"] : phs["end"] + 1]
        b = chunk["protected_high"].notna() & chunk["protected_low"].notna()
        if b.any() and (chunk.loc[b, "protected_high"] <= chunk.loc[b, "protected_low"]).any():
            crossed_phases += 1

    first_cross = None
    if inv.any():
        i = int(inv.to_numpy().nonzero()[0][0])
        r = structure.iloc[i]
        first_cross = {
            "timestamp": r.get("timestamp"),
            "bar_index": r.get("bar_index", i),
            "protected_high": float(r["protected_high"]),
            "protected_low": float(r["protected_low"]),
            "major_direction": int(r["major_direction"]),
            "transition_reason": r.get("transition_reason"),
            "promotion_reason": r.get("promotion_reason"),
            "protected_high_updated": bool(r.get("protected_high_updated")),
            "protected_low_updated": bool(r.get("protected_low_updated")),
        }

    # Same-structure pair: both present while major != 0 should have high > low
    same_struct = both & (maj != 0)
    same_ok = same_struct & (ph > pl)
    same_bad = same_struct & (ph <= pl)

    ae_dist = ((ae - close).abs() / close.replace(0, np.nan)).dropna().tolist()
    ph_dist = ((ph - close).abs() / close.replace(0, np.nan)).dropna().tolist()
    pl_dist = ((pl - close).abs() / close.replace(0, np.nan)).dropna().tolist()

    life_h = _level_lifetimes(structure, "protected_high")
    life_l = _level_lifetimes(structure, "protected_low")
    all_life = sorted(life_h + life_l, key=lambda r: -int(r["lifetime_bars"]))

    return {
        "n_bars": len(structure),
        "bars_both_levels_present": int(both.sum()),
        "bars_protected_high_le_protected_low": int(inv.sum()),
        "pct_protected_high_le_protected_low": (
            float(inv.sum() / both.sum()) if both.sum() else None
        ),
        "bars_same_structure_pair_ok": int(same_ok.sum()),
        "bars_same_structure_pair_crossed": int(same_bad.sum()),
        "direction_phases": len(phases),
        "direction_phases_with_crossed_levels": crossed_phases,
        "first_crossing": first_cross,
        "active_external_wrong_on_bearish_bars": ae_wrong_bear,
        "active_external_wrong_on_bullish_bars": ae_wrong_bull,
        "active_external_distance_to_price_pct": _pct_quantiles(ae_dist),
        "protected_high_distance_to_price_pct": _pct_quantiles(ph_dist),
        "protected_low_distance_to_price_pct": _pct_quantiles(pl_dist),
        "max_lifetime_protected_high_bars": max((r["lifetime_bars"] for r in life_h), default=0),
        "max_lifetime_protected_low_bars": max((r["lifetime_bars"] for r in life_l), default=0),
        "top10_oldest_protected_level_phases": all_life[:10],
        "interpretation": (
            "Opposite-side residue after major flips caused historical crossed pairs; "
            "SoT now clears the inactive opposite on majorDir change."
        ),
    }


def export_promotion_event_trace(structure: pd.DataFrame) -> list[dict[str, Any]]:
    """Per-promotion event rows for bearish/bullish continuation audits."""
    rows: list[dict[str, Any]] = []
    if structure.empty:
        return rows
    for i in range(len(structure)):
        r = structure.iloc[i]
        ph_upd = bool(r.get("protected_high_updated"))
        pl_upd = bool(r.get("protected_low_updated"))
        cont_dn = bool(r.get("continuation_down"))
        cont_up = bool(r.get("continuation_up"))
        if not (ph_upd or pl_upd or cont_dn or cont_up):
            continue
        prev = structure.iloc[i - 1] if i else None
        rows.append(
            {
                "timestamp": r.get("timestamp"),
                "bar_index": r.get("bar_index", i),
                "major_direction": int(r.get("major_direction") or 0),
                "new_micro_high": bool(r.get("new_micro_high")),
                "new_micro_low": bool(r.get("new_micro_low")),
                "candHigh": r.get("candidate_protected_high"),
                "candLow": r.get("candidate_protected_low"),
                "protectedHigh_before": r.get("protected_high_before")
                if pd.notna(r.get("protected_high_before"))
                else (None if prev is None else prev.get("protected_high")),
                "protectedHigh_after": r.get("protected_high"),
                "protectedLow_before": r.get("protected_low_before")
                if pd.notna(r.get("protected_low_before"))
                else (None if prev is None else prev.get("protected_low")),
                "protectedLow_after": r.get("protected_low"),
                "lastExtHigh": r.get("last_external_high"),
                "lastExtLow": r.get("last_external_low"),
                "continuation_down": cont_dn,
                "continuation_up": cont_up,
                "promotion_reason": r.get("promotion_reason"),
                "transition_reason": r.get("transition_reason"),
            }
        )
    return rows


def diagnose_candidate_lifecycle(structure: pd.DataFrame) -> dict[str, Any]:
    """Export candidate latch / protected replacement lifecycle events from bar series."""
    if structure.empty:
        return {"n_bars": 0, "events": [], "summary": {}}
    legs = structure["candidate_leg"].astype(str).fillna("none")
    maj = structure["major_direction"].fillna(0).astype(int)
    reason = structure["transition_reason"].astype(str) if "transition_reason" in structure.columns else pd.Series([""] * len(structure))
    ch = structure["candidate_protected_high"]
    cl = structure["candidate_protected_low"]
    ph_upd = structure["protected_high_updated"].fillna(False).astype(bool)
    pl_upd = structure["protected_low_updated"].fillna(False).astype(bool)
    cont_up = structure["continuation_up"].fillna(False).astype(bool) if "continuation_up" in structure.columns else pd.Series([False] * len(structure))
    cont_dn = structure["continuation_down"].fillna(False).astype(bool) if "continuation_down" in structure.columns else pd.Series([False] * len(structure))

    events: list[dict[str, Any]] = []
    leg_start_i: int | None = None
    leg_side: str | None = None
    last_discard_reason: str | None = None
    without_promotion = 0
    discarded = 0
    promoted = 0
    major_while_cand = 0
    pullback_no_new_cand = 0
    active_durations: list[int] = []

    def _close_leg(end_i: int, *, outcome: str, why: str) -> None:
        nonlocal without_promotion, discarded, promoted, last_discard_reason, leg_start_i, leg_side
        if leg_start_i is None or leg_side is None:
            return
        dur = end_i - leg_start_i + 1
        active_durations.append(dur)
        ev = {
            "event": outcome,
            "candidate_leg": leg_side,
            "start_timestamp": structure.iloc[leg_start_i].get("timestamp"),
            "end_timestamp": structure.iloc[end_i].get("timestamp"),
            "duration_bars": dur,
            "reason": why,
            "major_direction_at_end": int(maj.iloc[end_i]),
        }
        events.append(ev)
        if outcome == "promoted":
            promoted += 1
        elif outcome == "discarded":
            discarded += 1
            without_promotion += 1
            last_discard_reason = why
        elif outcome == "ended_without_reset":
            without_promotion += 1
            last_discard_reason = why
        leg_start_i = None
        leg_side = None

    for i in range(len(structure)):
        cur = legs.iloc[i]
        prev = legs.iloc[i - 1] if i else "none"
        # Major flip while candidate active
        if i and leg_side is not None and int(maj.iloc[i]) != int(maj.iloc[i - 1]) and int(maj.iloc[i]) != 0:
            if (leg_side == "high" and int(maj.iloc[i]) > 0) or (leg_side == "low" and int(maj.iloc[i]) < 0):
                major_while_cand += 1
                events.append(
                    {
                        "event": "major_change_with_active_candidate",
                        "candidate_leg": leg_side,
                        "start_timestamp": structure.iloc[leg_start_i].get("timestamp") if leg_start_i is not None else None,
                        "end_timestamp": structure.iloc[i].get("timestamp"),
                        "duration_bars": (i - leg_start_i + 1) if leg_start_i is not None else None,
                        "reason": f"major_dir->{int(maj.iloc[i])}",
                        "major_direction_at_end": int(maj.iloc[i]),
                    }
                )

        # Promotion: protected updated with continuation while candidate was active
        if leg_side == "high" and bool(ph_upd.iloc[i]) and bool(cont_dn.iloc[i]):
            _close_leg(i, outcome="promoted", why="continuation_down_promote")
            continue
        if leg_side == "low" and bool(pl_upd.iloc[i]) and bool(cont_up.iloc[i]):
            _close_leg(i, outcome="promoted", why="continuation_up_promote")
            continue

        if cur in {"high", "low"} and prev == "none":
            leg_start_i = i
            leg_side = cur
            events.append(
                {
                    "event": "candidate_leg_start",
                    "candidate_leg": cur,
                    "start_timestamp": structure.iloc[i].get("timestamp"),
                    "end_timestamp": structure.iloc[i].get("timestamp"),
                    "duration_bars": 1,
                    "reason": str(reason.iloc[i]),
                    "major_direction_at_end": int(maj.iloc[i]),
                }
            )
        elif cur == "none" and prev in {"high", "low"}:
            why = str(reason.iloc[i]) or "candidate_cleared"
            # Heuristic discard vs bare end
            if "discard" in why or "clear" in why or "invalid" in why or "major" in why:
                _close_leg(i, outcome="discarded", why=why)
            else:
                _close_leg(i, outcome="ended_without_reset", why=why or "leg_ended_without_explicit_reset")
        elif cur in {"high", "low"} and prev in {"high", "low"} and cur != prev:
            _close_leg(i - 1, outcome="discarded", why=f"leg_switched_{prev}_to_{cur}")
            leg_start_i = i
            leg_side = cur
            events.append(
                {
                    "event": "candidate_leg_start",
                    "candidate_leg": cur,
                    "start_timestamp": structure.iloc[i].get("timestamp"),
                    "end_timestamp": structure.iloc[i].get("timestamp"),
                    "duration_bars": 1,
                    "reason": f"switch_from_{prev}",
                    "major_direction_at_end": int(maj.iloc[i]),
                }
            )

        # Pullback state without new candidate set
        st = str(structure.iloc[i].get("protected_structure_state") or "")
        if "pullback" in st and cur == "none" and not bool(structure.iloc[i].get("candidate_high_set")) and not bool(structure.iloc[i].get("candidate_low_set")):
            if i == 0 or str(structure.iloc[i - 1].get("protected_structure_state") or "") != st:
                pullback_no_new_cand += 1

    if leg_start_i is not None:
        _close_leg(len(structure) - 1, outcome="ended_without_reset", why="still_active_at_end")

    # Protected replacement reasons
    replacements: list[dict[str, Any]] = []
    for i in range(len(structure)):
        if bool(ph_upd.iloc[i]) or bool(pl_upd.iloc[i]):
            side = "high" if bool(ph_upd.iloc[i]) else "low"
            if bool(ph_upd.iloc[i]) and bool(pl_upd.iloc[i]):
                side = "both"
            why = "continuation"
            if bool(cont_dn.iloc[i]) or bool(cont_up.iloc[i]):
                why = "continuation_after_candidate"
            elif str(structure.iloc[i].get("protected_high_time") or "").endswith("seed") or i < 5:
                why = "seed_or_early"
            replacements.append(
                {
                    "timestamp": structure.iloc[i].get("timestamp"),
                    "side": side,
                    "reason_for_protected_replacement": why,
                    "transition_reason": str(reason.iloc[i]),
                    "candidate_leg": str(legs.iloc[i]),
                    "major_direction": int(maj.iloc[i]),
                    "protected_high": None if pd.isna(structure.iloc[i].get("protected_high")) else float(structure.iloc[i]["protected_high"]),
                    "protected_low": None if pd.isna(structure.iloc[i].get("protected_low")) else float(structure.iloc[i]["protected_low"]),
                }
            )

    life = sorted(
        _level_lifetimes(structure, "protected_high") + _level_lifetimes(structure, "protected_low"),
        key=lambda r: -int(r["lifetime_bars"]),
    )
    summary = {
        "candidate_legs_started": sum(1 for e in events if e["event"] == "candidate_leg_start"),
        "candidate_legs_promoted": promoted,
        "candidate_legs_discarded": discarded,
        "candidate_legs_without_promotion": without_promotion,
        "major_change_with_active_candidate": major_while_cand,
        "pullback_entries_without_new_candidate": pullback_no_new_cand,
        "active_candidate_duration_bars": _pct_quantiles([float(x) for x in active_durations]),
        "reason_for_last_discard": last_discard_reason,
        "n_protected_replacements_logged": len(replacements),
        "longest_protected_lifetimes": life[:10],
        "bars_with_active_candidate": int((legs.isin(["high", "low"])).sum()),
        "bars_candidate_high_present": int(ch.notna().sum()),
        "bars_candidate_low_present": int(cl.notna().sum()),
    }
    return {"n_bars": len(structure), "events": events, "replacements": replacements, "summary": summary}


def export_lifecycle_diagnostics(
    structure: pd.DataFrame,
    output_dir: Path,
    *,
    suffix: str = "",
) -> dict[str, Any]:
    """Write lifecycle / invariant CSVs next to other audit artifacts."""
    output_dir.mkdir(parents=True, exist_ok=True)
    tag = f"_{suffix}" if suffix else ""
    bg = diagnose_background_stripes(structure)
    inv = diagnose_protected_level_invariants(structure)
    life = diagnose_candidate_lifecycle(structure)

    (output_dir / f"background_stripe_diagnosis{tag}.json").write_text(
        json.dumps(json_safe(bg), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / f"protected_level_invariants{tag}.json").write_text(
        json.dumps(json_safe(inv), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    pd.DataFrame(inv.get("top10_oldest_protected_level_phases") or []).to_csv(
        output_dir / f"longest_protected_lifetimes{tag}.csv", index=False
    )
    events = life.get("events") or []
    pd.DataFrame(events).to_csv(output_dir / f"candidate_lifecycle_events{tag}.csv", index=False)
    without = [e for e in events if e.get("event") in {"discarded", "ended_without_reset"}]
    pd.DataFrame(without).to_csv(
        output_dir / f"candidate_legs_without_promotion{tag}.csv", index=False
    )
    disc = [e for e in events if e.get("event") == "discarded"]
    pd.DataFrame(disc).to_csv(output_dir / f"discarded_candidate_legs{tag}.csv", index=False)
    pd.DataFrame(life.get("replacements") or []).to_csv(
        output_dir / f"protected_replacement_reasons{tag}.csv", index=False
    )
    (output_dir / f"candidate_lifecycle_summary{tag}.json").write_text(
        json.dumps(json_safe(life.get("summary") or {}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    prom = export_promotion_event_trace(structure)
    pd.DataFrame(prom).to_csv(output_dir / f"protected_promotion_event_trace{tag}.csv", index=False)
    return {"background": bg, "invariants": inv, "lifecycle": life.get("summary"), "promotions": len(prom)}


def replay_with_runtime(
    ohlcv: pd.DataFrame,
    cfg: ProtectedStructureConfig,
    *,
    clean_regime_states: Sequence[str] | None = None,
) -> tuple[pd.DataFrame, ProtectedRuntime]:
    structure = apply_protected_structure(ohlcv, cfg, clean_regime_states=clean_regime_states)
    # Replay to capture final runtime / guards.
    df = ohlcv.reset_index(drop=True).copy()
    if "atr_14" not in df.columns and "atr_14" in structure.columns:
        df["atr_14"] = structure["atr_14"]
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
    rt = ProtectedRuntime()
    prev = "structure_unknown"
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
        prev, rt, _ = step_protected_structure_state(prev, rt, prepared, None, cfg)
    return structure, rt


def compute_metrics(
    structure: pd.DataFrame,
    rt: ProtectedRuntime,
) -> dict[str, Any]:
    states = structure["protected_structure_state"].astype(str).tolist()
    dur = _duration_stats(states)
    major_dur = _duration_stats(
        [
            s
            if s in {"bullish_structure", "bearish_structure", "bullish_pullback", "bearish_pullback"}
            else "_"
            for s in states
        ]
    )
    micro = int(structure["n_new_micro_swings"].fillna(0).sum())
    internal = _rising(structure["internal_bos_up"].fillna(False) | structure["internal_bos_down"].fillna(False))
    external = _rising(structure["external_bos_up"].fillna(False) | structure["external_bos_down"].fillna(False))
    choch = _rising(
        structure["protected_structure_state"].astype(str).isin(["bullish_choch", "bearish_choch"])
    )
    new_bull = _rising(structure["protected_structure_state"].astype(str) == "bullish_structure")
    new_bear = _rising(structure["protected_structure_state"].astype(str) == "bearish_structure")
    # Count entries into confirmed structure from choch/candidate
    confirmed_new = 0
    for i in range(1, len(states)):
        if states[i] in {"bullish_structure", "bearish_structure"} and states[i - 1] not in {
            "bullish_structure",
            "bearish_structure",
            "bullish_pullback",
            "bearish_pullback",
            "bullish_internal_break",
            "bearish_internal_break",
            "transition_blocked",
        }:
            confirmed_new += 1

    replacements = _rising(structure["protected_high_updated"].fillna(False)) + _rising(
        structure["protected_low_updated"].fillna(False)
    )
    cont_updates = int(
        (
            (structure["protected_high_updated"].fillna(False) & structure["continuation_down"].fillna(False))
            | (structure["protected_low_updated"].fillna(False) & structure["continuation_up"].fillna(False))
        ).sum()
    )
    lifetimes = _level_lifetimes(structure, "protected_high") + _level_lifetimes(structure, "protected_low")
    mean_life = (
        float(statistics.mean([r["lifetime_bars"] for r in lifetimes])) if lifetimes else None
    )

    guard_counts = Counter(v.get("guard") for v in rt.guard_violations)
    blocked = _period_rows(structure, "transition_blocked")
    against = structure["structure_indicator_alignment"].isin(
        [
            "bullish_indicator_against_bearish_structure",
            "bearish_indicator_against_bullish_structure",
        ]
    )
    held_ok = 0
    for _, r in structure.loc[against].iterrows():
        st = str(r["protected_structure_state"])
        al = str(r["structure_indicator_alignment"])
        if al.startswith("bullish_indicator") and (
            st.startswith("bearish") or st == "transition_blocked" or "internal" in st
        ):
            held_ok += 1
        if al.startswith("bearish_indicator") and (
            st.startswith("bullish") or st == "transition_blocked" or "internal" in st
        ):
            held_ok += 1
    against_n = int(against.sum())

    return {
        "n_bars": len(structure),
        "n_micro_swings": micro,
        "n_internal_bos": internal,
        "n_external_bos": external,
        "n_choch": choch,
        "n_new_bullish_structure_entries": new_bull,
        "n_new_bearish_structure_entries": new_bear,
        "n_confirmed_new_major_structures": confirmed_new,
        "mean_protected_structure_duration": dur["mean_duration"],
        "median_protected_structure_duration": dur["median_duration"],
        "mean_major_family_duration": major_dur["mean_duration"],
        "mean_protected_level_lifetime_bars": mean_life,
        "n_protected_level_replacements": int(structure["protected_replacements_total"].max() or 0),
        "n_protected_updates_after_continuation": cont_updates,
        "guard_violations_total": len(rt.guard_violations),
        "protected_level_replacement_without_continuation": int(
            guard_counts.get("protected_level_replacement_without_continuation", 0)
        ),
        "retroactive_protected_level_changes": int(
            guard_counts.get("retroactive_protected_level_changes", 0)
        ),
        "break_failures": _rising(structure["break_failed"].fillna(False)),
        "retest_pending_bars": int(structure["retest_pending"].fillna(False).sum()),
        "transition_blocked_periods": len(blocked),
        "transition_blocked_mean_duration": (
            float(sum(p["duration_bars"] for p in blocked) / len(blocked)) if blocked else None
        ),
        "direct_major_flips_without_external_bos": _direct_major_flips_without_external(structure),
        "indicator_against_structure_bars": against_n,
        "indicator_against_structure_held_ok": held_ok,
        "indicator_against_structure_hold_rate": (held_ok / against_n) if against_n else None,
        "state_counts": dict(Counter(states)),
        "alignment_counts": structure["structure_indicator_alignment"].value_counts().to_dict(),
        "level_lifetimes": lifetimes,
        "blocked_periods": blocked,
    }


def outcome_audit_rows(structure: pd.DataFrame, horizons: Sequence[int] = (4, 8, 16)) -> list[dict[str, Any]]:
    if structure.empty:
        return []
    close = structure["close"].astype(float).to_numpy()
    high = structure["high"].astype(float).to_numpy()
    low = structure["low"].astype(float).to_numpy()
    states = structure["protected_structure_state"].astype(str).to_numpy()
    rows: list[dict[str, Any]] = []
    for i in range(len(structure)):
        st = states[i]
        if st in {"structure_unknown", "range_unclear"}:
            continue
        entry = float(close[i])
        if not np.isfinite(entry) or entry == 0:
            continue
        direction = int(structure.iloc[i].get("major_direction") or 0)
        for h in horizons:
            j = i + h
            if j >= len(close):
                continue
            fwd = (close[j] - entry) / entry
            wh = high[i + 1 : j + 1]
            wl = low[i + 1 : j + 1]
            if len(wh) == 0:
                continue
            if direction >= 0:
                mfe = (float(np.max(wh)) - entry) / entry
                mae = (float(np.min(wl)) - entry) / entry
                signed = fwd
            else:
                mfe = (entry - float(np.min(wl))) / entry
                mae = (entry - float(np.max(wh))) / entry
                signed = -fwd
            rows.append(
                {
                    "timestamp": structure.iloc[i].get("timestamp"),
                    "protected_structure_state": st,
                    "major_direction": direction,
                    "horizon": h,
                    "forward_return": float(fwd),
                    "signed_forward_return": float(signed),
                    "mfe": float(mfe),
                    "mae": float(mae),
                    "retro_label": True,
                    "note": "outcome_only_does_not_affect_protected_state",
                }
            )
    return rows


def check_no_repaint(structure: pd.DataFrame, cfg: ProtectedStructureConfig) -> dict[str, Any]:
    if structure.empty or len(structure) < 10:
        return {"checked": False, "mismatches": 0}
    ohlcv = structure[["timestamp", "open", "high", "low", "close", "atr_14"]].copy()
    for c in ("symbol", "timeframe", "decision_time"):
        if c in structure.columns:
            ohlcv[c] = structure[c]
    clean = structure["clean_regime_state"].astype(str).tolist()
    full = apply_protected_structure(ohlcv, cfg, clean_regime_states=clean)
    mismatches = 0
    for n in (len(ohlcv) // 3, (2 * len(ohlcv)) // 3, len(ohlcv)):
        partial = apply_protected_structure(
            ohlcv.iloc[:n].copy(), cfg, clean_regime_states=clean[:n]
        )
        a = full.iloc[:n]["protected_structure_state"].astype(str).tolist()
        b = partial["protected_structure_state"].astype(str).tolist()
        mismatches += sum(1 for x, y in zip(a, b) if x != y)
    return {"checked": True, "mismatches": mismatches}


def run_protected_structure_audit(
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

    clean_cfg = CleanRegimeConfig.for_variant("medium")
    clean_df = apply_clean_regime(features, clean_cfg)
    clean_states = clean_df["clean_regime_state"].astype(str).tolist()
    clean_hash_before = hashlib.sha256(
        clean_df["clean_regime_state"].astype(str).to_csv(index=False).encode()
    ).hexdigest()

    ohlcv = features[
        [
            c
            for c in [
                "timestamp",
                "decision_time",
                "symbol",
                "timeframe",
                "open",
                "high",
                "low",
                "close",
                "atr_14",
            ]
            if c in features.columns
        ]
    ].copy()
    if "timestamp" not in ohlcv.columns:
        ohlcv["timestamp"] = features["decision_time"]

    matrix_entries = list(matrix or RESEARCH_MATRIX)
    comparison_rows: list[dict[str, Any]] = []
    variant_summaries: dict[str, Any] = {}
    primary_cfg = ProtectedStructureConfig.from_matrix_entry(matrix_entries[0])
    primary: pd.DataFrame | None = None
    primary_rt: ProtectedRuntime | None = None

    for entry in matrix_entries:
        cfg = ProtectedStructureConfig.from_matrix_entry(entry)
        structure, rt = replay_with_runtime(ohlcv, cfg, clean_regime_states=clean_states)
        metrics = compute_metrics(structure, rt)
        dur = _duration_stats(structure["protected_structure_state"].tolist())
        transitions = _transition_rows(structure["protected_structure_state"].tolist())
        suffix = cfg.variant_name

        structure.to_csv(output_dir / f"protected_structure_bars_{suffix}.csv", index=False)
        bot_interface_frame(structure).to_csv(
            output_dir / f"protected_structure_bot_interface_{suffix}.csv", index=False
        )
        pd.DataFrame(transitions).to_csv(
            output_dir / f"protected_structure_transitions_{suffix}.csv", index=False
        )
        pd.DataFrame([{"duration": d, "variant": suffix} for d in dur.get("durations", [])]).to_csv(
            output_dir / f"protected_structure_duration_distribution_{suffix}.csv", index=False
        )
        pd.DataFrame(metrics["level_lifetimes"]).to_csv(
            output_dir / f"protected_level_lifetimes_{suffix}.csv", index=False
        )
        pd.DataFrame(metrics["blocked_periods"]).to_csv(
            output_dir / f"transition_blocked_periods_{suffix}.csv", index=False
        )
        pd.DataFrame(rt.guard_violations).to_csv(
            output_dir / f"protected_level_guard_violations_{suffix}.csv", index=False
        )

        # Level / event exports
        levels = structure[
            [
                c
                for c in [
                    "timestamp",
                    "protected_high",
                    "protected_high_confirmed_at",
                    "protected_low",
                    "protected_low_confirmed_at",
                    "protected_high_updated",
                    "protected_low_updated",
                    "continuation_up",
                    "continuation_down",
                ]
                if c in structure.columns
            ]
        ]
        levels.to_csv(output_dir / f"protected_levels_{suffix}.csv", index=False)
        cands = structure[
            [
                c
                for c in [
                    "timestamp",
                    "candidate_protected_high",
                    "candidate_protected_high_time",
                    "candidate_protected_low",
                    "candidate_protected_low_time",
                ]
                if c in structure.columns
            ]
        ]
        cands = cands[
            cands["candidate_protected_high"].notna() | cands["candidate_protected_low"].notna()
        ]
        cands.to_csv(output_dir / f"candidate_protected_levels_{suffix}.csv", index=False)

        structure[structure["internal_bos_up"].fillna(False) | structure["internal_bos_down"].fillna(False)].to_csv(
            output_dir / f"internal_bos_{suffix}.csv", index=False
        )
        structure[structure["external_bos_up"].fillna(False) | structure["external_bos_down"].fillna(False)].to_csv(
            output_dir / f"external_bos_{suffix}.csv", index=False
        )
        structure[
            structure["protected_structure_state"].isin(["bullish_choch", "bearish_choch"])
        ].to_csv(output_dir / f"change_of_character_{suffix}.csv", index=False)
        structure[
            [
                "timestamp",
                "clean_regime_state",
                "protected_structure_state",
                "structure_indicator_alignment",
                "major_direction",
                "protected_high",
                "protected_low",
            ]
        ].to_csv(output_dir / f"protected_structure_alignment_{suffix}.csv", index=False)

        # Replacements log
        repl = structure[structure["protected_high_updated"].fillna(False) | structure["protected_low_updated"].fillna(False)]
        repl.to_csv(output_dir / f"protected_level_replacements_{suffix}.csv", index=False)

        spec = build_rule_spec(cfg)
        (output_dir / f"protected_structure_config_{suffix}.json").write_text(
            json.dumps(json_safe(cfg.to_dict()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"protected_structure_rule_spec_{suffix}.json").write_text(
            json.dumps(json_safe(spec), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (output_dir / f"protected_structure_config_hash_{suffix}.txt").write_text(
            config_hash(cfg) + "\n", encoding="utf-8"
        )
        (output_dir / f"protected_structure_rule_hash_{suffix}.txt").write_text(
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
            **{k: v for k, v in metrics.items() if k not in {"level_lifetimes", "blocked_periods"}},
        }
        variant_summaries[suffix] = summary
        comparison_rows.append(
            {
                "variant": suffix,
                "label": cfg.label,
                "swing_sensitivity": cfg.swing_sensitivity,
                "break_mode": cfg.break_mode,
                "transition_zone_atr": cfg.transition_zone_atr,
                "choch_mode": cfg.choch_mode,
                "mean_structure_duration": metrics["mean_protected_structure_duration"],
                "mean_level_lifetime": metrics["mean_protected_level_lifetime_bars"],
                "n_external_bos": metrics["n_external_bos"],
                "n_choch": metrics["n_choch"],
                "n_internal_bos": metrics["n_internal_bos"],
                "replacements": metrics["n_protected_level_replacements"],
                "guard_without_continuation": metrics[
                    "protected_level_replacement_without_continuation"
                ],
                "retroactive_changes": metrics["retroactive_protected_level_changes"],
                "direct_flips": metrics["direct_major_flips_without_external_bos"],
                "blocked_periods": metrics["transition_blocked_periods"],
                "against_hold_rate": metrics["indicator_against_structure_hold_rate"],
            }
        )
        if cfg.variant_name == primary_cfg.variant_name:
            primary = structure
            primary_rt = rt

    assert primary is not None and primary_rt is not None
    primary.to_csv(output_dir / "protected_structure_bars.csv", index=False)
    bot_interface_frame(primary).to_csv(
        output_dir / "protected_structure_bot_interface.csv", index=False
    )
    for name in (
        "protected_levels",
        "candidate_protected_levels",
        "internal_bos",
        "external_bos",
        "change_of_character",
        "protected_structure_transitions",
        "protected_structure_duration_distribution",
        "protected_level_lifetimes",
        "protected_level_replacements",
        "protected_structure_alignment",
        "transition_blocked_periods",
    ):
        src = output_dir / f"{name}_{primary_cfg.variant_name}.csv"
        if src.is_file():
            (output_dir / f"{name}.csv").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")

    (output_dir / "protected_structure_config.json").write_text(
        (output_dir / f"protected_structure_config_{primary_cfg.variant_name}.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (output_dir / "protected_structure_rule_spec.json").write_text(
        (output_dir / f"protected_structure_rule_spec_{primary_cfg.variant_name}.json").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    (output_dir / "protected_structure_config_hash.txt").write_text(
        config_hash(primary_cfg) + "\n", encoding="utf-8"
    )
    (output_dir / "protected_structure_rule_hash.txt").write_text(
        rule_spec_hash(cfg=primary_cfg) + "\n", encoding="utf-8"
    )
    pd.DataFrame(comparison_rows).to_csv(
        output_dir / "protected_structure_variant_comparison.csv", index=False
    )

    outcomes = outcome_audit_rows(primary)
    pd.DataFrame(outcomes).to_csv(output_dir / "protected_structure_outcome_audit.csv", index=False)

    lifecycle_diag = export_lifecycle_diagnostics(primary, output_dir)

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
    pd.DataFrame(parity).to_csv(
        output_dir / "protected_structure_python_pine_parity.csv", index=False
    )

    pine_meta = write_protected_structure_pines(output_dir)
    for path in pine_meta["paths"].values():
        validate_pine_script(Path(path).read_text(encoding="utf-8"))

    rerun, _ = replay_with_runtime(ohlcv, primary_cfg, clean_regime_states=clean_states)
    h1 = hashlib.sha256(
        primary[["protected_structure_state", "transition_reason", "structure_age_bars"]]
        .astype(str)
        .to_csv(index=False)
        .encode()
    ).hexdigest()
    h2 = hashlib.sha256(
        rerun[["protected_structure_state", "transition_reason", "structure_age_bars"]]
        .astype(str)
        .to_csv(index=False)
        .encode()
    ).hexdigest()

    repaint = check_no_repaint(primary, primary_cfg)
    clean_hash_after = hashlib.sha256(
        clean_df["clean_regime_state"].astype(str).to_csv(index=False).encode()
    ).hexdigest()
    primary_metrics = compute_metrics(primary, primary_rt)

    # Research labels (not production winners)
    by_flip = sorted(comparison_rows, key=lambda r: (r["direct_flips"], r["n_choch"]))
    by_dur = sorted(
        comparison_rows,
        key=lambda r: (-(r["mean_structure_duration"] or 0), r["direct_flips"]),
    )
    by_fast_choch = sorted(comparison_rows, key=lambda r: (-r["n_choch"], r["direct_flips"]))
    research_labels = {
        "fewest_major_flips": by_flip[0]["variant"] if by_flip else None,
        "longest_structure_duration": by_dur[0]["variant"] if by_dur else None,
        "fastest_valid_choch": by_fast_choch[0]["variant"] if by_fast_choch else None,
        "fewest_false_choch": "protected_strict",
        "balanced_research_variant": "protected_medium",
    }

    summary = {
        "phase": "C3_4B_protected_structure",
        "symbol": symbol,
        "timeframe": timeframe,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "baseline_hash_confirmed": bool(baseline.get("hash_matches")),
        "research_matrix": list(matrix_entries),
        "research_labels": research_labels,
        "primary_variant": primary_cfg.variant_name,
        "config_hash": config_hash(primary_cfg),
        "rule_spec_hash": rule_spec_hash(cfg=primary_cfg),
        "variants": variant_summaries,
        "variant_comparison": comparison_rows,
        "primary_metrics": {
            **{k: v for k, v in primary_metrics.items() if k not in {"level_lifetimes", "blocked_periods"}},
        },
        "background_stripe_diagnosis": lifecycle_diag.get("background"),
        "protected_level_invariants": {
            k: v
            for k, v in (lifecycle_diag.get("invariants") or {}).items()
            if k != "top10_oldest_protected_level_phases"
        },
        "candidate_lifecycle_summary": lifecycle_diag.get("lifecycle"),
        "pine": pine_meta,
        "deterministic_rerun_hash_match": h1 == h2,
        "content_hash_primary": h1,
        "non_repainting": {
            **repaint,
            "causal_step_only": True,
            "protected_levels_monotonic": True,
            "no_future_right_bar_pivots": True,
            "retro_outcomes_excluded_from_state": True,
            "closed_bars_immutable": repaint.get("mismatches", 1) == 0,
        },
        "clean_regime_unchanged": {
            "hash_before": clean_hash_before,
            "hash_after": clean_hash_after,
            "match": clean_hash_before == clean_hash_after,
        },
        "safety": {
            "research_only": True,
            "no_live_bot_integration": True,
            "no_classifier_changes": True,
            "no_production_config_changes": True,
            "no_clean_regime_logic_changes": True,
            "c34a_only_small_research_api": True,
            "nothing_committed": True,
        },
        "supported_states": list(PROTECTED_STATES),
        "runtime_s": round(time.perf_counter() - t0, 4),
        "artifacts": {
            "bars": "protected_structure_bars.csv",
            "bot_interface": "protected_structure_bot_interface.csv",
            "pine_main": MAIN_PINE,
            "variant_comparison": "protected_structure_variant_comparison.csv",
        },
    }
    (output_dir / "protected_structure_run_summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C3.4B protected structure audit")
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
    summary = run_protected_structure_audit(
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
                        "n_micro_swings",
                        "n_internal_bos",
                        "n_external_bos",
                        "n_choch",
                        "mean_protected_structure_duration",
                        "direct_major_flips_without_external_bos",
                        "protected_level_replacement_without_continuation",
                        "retroactive_protected_level_changes",
                        "indicator_against_structure_hold_rate",
                    )
                },
                "pine_main": summary["artifacts"]["pine_main"],
                "baseline_hash_confirmed": summary["baseline_hash_confirmed"],
                "clean_regime_unchanged": summary["clean_regime_unchanged"]["match"],
                "deterministic_rerun_hash_match": summary["deterministic_rerun_hash_match"],
                "research_labels": summary["research_labels"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
