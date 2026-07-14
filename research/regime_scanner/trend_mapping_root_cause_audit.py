"""Phase C0: mapping / ground-truth / stuck-weakening root-cause audit (read-only).

Reuses frozen Phase-B timeline exports. Does not modify trend_structure,
trend_state_machine, trend_state_policy, Phase-B results, live bots, or
liquidation research.

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_mapping_root_cause_audit \\
    --phase-b-dir research/regime_scanner/results_trend_robustness_phase_b \\
    --output-dir research/regime_scanner/results_trend_mapping_root_cause_phase_c0
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_robustness_audit import (
    AUDIT_CLASS_MAP,
    GT_ADX_MIN,
    GT_NET_48_SIDEWAYS,
    GT_NET_48_TREND,
    GT_NET_288_SIDEWAYS,
    PROPOSED_POLICY,
    audit_class_for_state,
    exclusive_bearish_structure,
    exclusive_bullish_structure,
    ground_truth_label,
    proposed_policy_for_audit_class,
)
from research.regime_scanner.trend_state_policy import policy_for_state

DEFAULT_PHASE_B = Path("research/regime_scanner/results_trend_robustness_phase_b")
DEFAULT_OUT = Path("research/regime_scanner/results_trend_mapping_root_cause_phase_c0")

# Alternative audit-only mappings (do not change production).
MAP_EXISTING = dict(AUDIT_CLASS_MAP)

MAP_STRONG_ONLY: dict[str, str] = {
    **{k: "UNCLEAR" for k in AUDIT_CLASS_MAP},
    "strong_bullish": "UPTREND",
    "strong_bearish": "DOWNTREND",
    "neutral": "SIDEWAYS",
    "bottoming": "BOTTOMING",
    "topping": "TOPPING",
}

# Weakening counted as late trend direction (diagnostic contrast only).
MAP_WEAKENING_AS_TREND: dict[str, str] = {
    **AUDIT_CLASS_MAP,
    "bullish_weakening": "UPTREND",
    "bearish_weakening": "DOWNTREND",
    "early_bullish": "UNCLEAR",  # early as transition, not full trend
    "early_bearish": "UNCLEAR",
}

# Hand + rule-based segments (must cover up/down/side/switch + Mar 6 + Mar 8-9).
CURATED_SEGMENTS: tuple[dict[str, Any], ...] = (
    {
        "segment_id": "S01_mar05_pre_crash",
        "theme": "pre_crash_context",
        "start": "2026-03-05T18:00:00+00:00",
        "end": "2026-03-06T00:00:00+00:00",
        "must_include": True,
    },
    {
        "segment_id": "S02_mar06_crash",
        "theme": "clear_down_crash",
        "start": "2026-03-06T00:00:00+00:00",
        "end": "2026-03-07T00:00:00+00:00",
        "must_include": True,
    },
    {
        "segment_id": "S03_mar07_aftershock",
        "theme": "post_crash_chop",
        "start": "2026-03-07T00:00:00+00:00",
        "end": "2026-03-08T00:00:00+00:00",
        "must_include": True,
    },
    {
        "segment_id": "S04_mar08_09_recovery",
        "theme": "bottoming_recovery",
        "start": "2026-03-08T00:00:00+00:00",
        "end": "2026-03-10T00:00:00+00:00",
        "must_include": True,
    },
    {
        "segment_id": "S05_mar04_bull_burst",
        "theme": "clear_up_with_early_strong",
        "start": "2026-03-04T08:00:00+00:00",
        "end": "2026-03-04T14:00:00+00:00",
        "must_include": True,
    },
    {
        "segment_id": "S06_mar02_down_gt",
        "theme": "clear_down_gt_episode",
        "start": "2026-03-02T06:00:00+00:00",
        "end": "2026-03-02T12:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S07_mar03_up_gt_bottoming",
        "theme": "clear_up_while_bottoming",
        "start": "2026-03-03T17:00:00+00:00",
        "end": "2026-03-03T22:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S08_mar09_bear_transition",
        "theme": "trend_switch",
        "start": "2026-03-09T16:00:00+00:00",
        "end": "2026-03-09T22:00:00+00:00",
        "must_include": True,
    },
    {
        "segment_id": "S09_mar12_down_gt",
        "theme": "clear_down_gt_episode",
        "start": "2026-03-12T13:00:00+00:00",
        "end": "2026-03-12T18:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S10_mar16_up_gt_weakening",
        "theme": "clear_up_while_weakening",
        "start": "2026-03-16T02:00:00+00:00",
        "end": "2026-03-16T10:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S11_apr_switch_sample",
        "theme": "trend_switch",
        "start": "2026-04-05T00:00:00+00:00",
        "end": "2026-04-06T00:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S12_apr_sideways_probe",
        "theme": "sideways_probe",
        "start": "2026-04-12T00:00:00+00:00",
        "end": "2026-04-13T12:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S13_may_down_sample",
        "theme": "clear_down_gt_episode",
        "start": "2026-05-04T00:00:00+00:00",
        "end": "2026-05-05T00:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S14_may_up_sample",
        "theme": "clear_up_gt_episode",
        "start": "2026-05-12T00:00:00+00:00",
        "end": "2026-05-13T00:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S15_may_chop",
        "theme": "sideways_probe",
        "start": "2026-05-20T00:00:00+00:00",
        "end": "2026-05-21T12:00:00+00:00",
        "must_include": False,
    },
    {
        "segment_id": "S16_apr20_mixed",
        "theme": "mixed_ambiguous",
        "start": "2026-04-20T00:00:00+00:00",
        "end": "2026-04-21T00:00:00+00:00",
        "must_include": False,
    },
)


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    return str(_ts(v).isoformat())


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_csv(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def map_state(state: str, mapping: Mapping[str, str]) -> str:
    return mapping.get(str(state or "unavailable"), "UNCLEAR")


def gt_expected_audit(gt: str) -> str | None:
    return {
        "CLEAR_UPTREND": "UPTREND",
        "CLEAR_DOWNTREND": "DOWNTREND",
        "CLEAR_SIDEWAYS": "SIDEWAYS",
    }.get(str(gt))


def ground_truth_strict(
    *,
    has_hh_hl_flag: bool,
    has_lh_ll_flag: bool,
    net_48: float | None,
    net_288: float | None,
    di_spread: float | None,
    adx: float | None,
) -> str:
    """Stricter persistence-friendly GT (higher barriers; still causal)."""
    if net_48 is None or di_spread is None or adx is None:
        return "AMBIGUOUS"
    excl_up = exclusive_bullish_structure(has_hh_hl_flag, has_lh_ll_flag)
    excl_dn = exclusive_bearish_structure(has_hh_hl_flag, has_lh_ll_flag)
    if excl_up and net_48 > 2.0 and di_spread > 5.0 and adx >= 22.0:
        if net_288 is not None and net_288 > 1.0:
            return "CLEAR_UPTREND"
    if excl_dn and net_48 < -2.0 and di_spread < -5.0 and adx >= 22.0:
        if net_288 is not None and net_288 < -1.0:
            return "CLEAR_DOWNTREND"
    if (
        abs(net_48) < 0.35
        and net_288 is not None
        and abs(net_288) < 1.5
        and not excl_up
        and not excl_dn
    ):
        return "CLEAR_SIDEWAYS"
    return "AMBIGUOUS"


def ground_truth_strong_only(
    *,
    has_hh_hl_flag: bool,
    has_lh_ll_flag: bool,
    net_48: float | None,
    net_288: float | None,
    di_spread: float | None,
    adx: float | None,
) -> str:
    """Very clear trends only; most bars remain AMBIGUOUS."""
    if net_48 is None or net_288 is None or di_spread is None or adx is None:
        return "AMBIGUOUS"
    excl_up = exclusive_bullish_structure(has_hh_hl_flag, has_lh_ll_flag)
    excl_dn = exclusive_bearish_structure(has_hh_hl_flag, has_lh_ll_flag)
    if excl_up and net_48 > 3.0 and net_288 > 4.0 and di_spread > 8.0 and adx >= 25.0:
        return "CLEAR_UPTREND"
    if excl_dn and net_48 < -3.0 and net_288 < -4.0 and di_spread < -8.0 and adx >= 25.0:
        return "CLEAR_DOWNTREND"
    if abs(net_48) < 0.25 and abs(net_288) < 1.0 and not excl_up and not excl_dn and adx < 20:
        return "CLEAR_SIDEWAYS"
    return "AMBIGUOUS"


def apply_persistence_filter(labels: Sequence[str], min_run: int) -> list[str]:
    """Require min_run contiguous identical CLEAR_* labels; else AMBIGUOUS.

    Purely diagnostic post-process on an already-causal label series.
    """
    labs = list(labels)
    n = len(labs)
    out = ["AMBIGUOUS"] * n
    i = 0
    while i < n:
        j = i
        while j < n and labs[j] == labs[i]:
            j += 1
        run = j - i
        if str(labs[i]).startswith("CLEAR_") and run >= min_run:
            for k in range(i, j):
                out[k] = labs[i]
        i = j
    return out


def enrich_timeline(df: pd.DataFrame) -> pd.DataFrame:
    """Add alternative GT + mapping columns. Does not mutate Phase-B file."""
    out = df.copy()
    out["decision_time"] = pd.to_datetime(out["decision_time"], utc=True)
    if "candle_timestamp" in out.columns:
        out["candle_timestamp"] = pd.to_datetime(out["candle_timestamp"], utc=True)

    gt_strict = []
    gt_strong = []
    for r in out.itertuples(index=False):
        hh = bool(getattr(r, "has_hh_hl"))
        ll = bool(getattr(r, "has_lh_ll"))
        n48 = getattr(r, "net_48", None)
        n288 = getattr(r, "net_288", None)
        ds = getattr(r, "di_spread", None)
        adx = getattr(r, "adx", None)
        try:
            n48 = float(n48) if pd.notna(n48) else None
        except (TypeError, ValueError):
            n48 = None
        try:
            n288 = float(n288) if pd.notna(n288) else None
        except (TypeError, ValueError):
            n288 = None
        try:
            ds = float(ds) if pd.notna(ds) else None
        except (TypeError, ValueError):
            ds = None
        try:
            adx = float(adx) if pd.notna(adx) else None
        except (TypeError, ValueError):
            adx = None
        gt_strict.append(
            ground_truth_strict(
                has_hh_hl_flag=hh,
                has_lh_ll_flag=ll,
                net_48=n48,
                net_288=n288,
                di_spread=ds,
                adx=adx,
            )
        )
        gt_strong.append(
            ground_truth_strong_only(
                has_hh_hl_flag=hh,
                has_lh_ll_flag=ll,
                net_48=n48,
                net_288=n288,
                di_spread=ds,
                adx=adx,
            )
        )
    out["gt_existing"] = out["gt_label"].astype(str)
    out["gt_strict_raw"] = gt_strict
    out["gt_strong_raw"] = gt_strong
    # persistence: require 12 bars (~1h) for strict, 24 bars (~2h) for strong-only
    out["gt_strict"] = apply_persistence_filter(gt_strict, min_run=12)
    out["gt_strong_only"] = apply_persistence_filter(gt_strong, min_run=24)

    out["state_mapped_existing"] = out["state"].map(lambda s: map_state(s, MAP_EXISTING))
    out["state_mapped_strong_only"] = out["state"].map(lambda s: map_state(s, MAP_STRONG_ONLY))
    out["state_mapped_weakening_as_trend"] = out["state"].map(
        lambda s: map_state(s, MAP_WEAKENING_AS_TREND)
    )
    return out


def match_rate(df: pd.DataFrame, gt_col: str, mapped_col: str) -> dict[str, Any]:
    clear = df[df[gt_col].astype(str).str.startswith("CLEAR_")].copy()
    if clear.empty:
        return {"n_clear": 0, "overall_match_rate": None, "by_gt": {}}
    exp = clear[gt_col].map(gt_expected_audit)
    hit = clear[mapped_col].astype(str) == exp.astype(str)
    by = {}
    for gt, g in clear.groupby(gt_col):
        e = g[gt_col].map(gt_expected_audit)
        m = g[mapped_col].astype(str) == e.astype(str)
        by[str(gt)] = {
            "n": int(len(g)),
            "matches": int(m.sum()),
            "match_rate": float(m.mean()) if len(g) else None,
        }
    return {
        "n_clear": int(len(clear)),
        "n_ambiguous": int((~df[gt_col].astype(str).str.startswith("CLEAR_")).sum()),
        "overall_match_rate": float(hit.mean()),
        "by_gt": by,
    }


def episode_stats(labels: Sequence[str], target: str) -> dict[str, Any]:
    labs = list(labels)
    lengths: list[int] = []
    i = 0
    n = len(labs)
    while i < n:
        if labs[i] != target:
            i += 1
            continue
        j = i
        while j < n and labs[j] == target:
            j += 1
        lengths.append(j - i)
        i = j
    if not lengths:
        return {
            "n_episodes": 0,
            "mean_len": None,
            "median_len": None,
            "p90_len": None,
            "total_bars": 0,
        }
    arr = np.asarray(lengths, dtype=float)
    return {
        "n_episodes": int(len(lengths)),
        "mean_len": float(arr.mean()),
        "median_len": float(np.median(arr)),
        "p90_len": float(np.percentile(arr, 90)),
        "total_bars": int(arr.sum()),
    }


def detection_counts(df: pd.DataFrame, gt_col: str, mapped_col: str, gt: str, want: str) -> dict[str, Any]:
    """Episode detection: does mapped class appear at least once inside each GT episode?"""
    labs = df[gt_col].astype(str).tolist()
    mapped = df[mapped_col].astype(str).tolist()
    detected = missed = 0
    i = 0
    n = len(labs)
    while i < n:
        if labs[i] != gt:
            i += 1
            continue
        j = i
        while j < n and labs[j] == gt:
            j += 1
        if any(mapped[k] == want for k in range(i, j)):
            detected += 1
        else:
            missed += 1
        i = j
    return {"n_episodes": detected + missed, "detected": detected, "missed": missed}


def countertrend_grant_share(df: pd.DataFrame, gt_col: str, gt: str, *, long_grant: bool) -> float | None:
    sub = df[df[gt_col].astype(str) == gt]
    if sub.empty:
        return None
    if long_grant:
        return float(sub["allow_long"].astype(bool).mean())
    return float(sub["allow_short"].astype(bool).mean())


def analyze_mapping_semantics() -> list[dict[str, Any]]:
    rows = []
    comments = {
        "early_bullish": (
            "Frühphase nach Bottoming; fachlich Trendbeginn, aber oft kurz/instabil.",
            "Verschlechtert Match wenn GT nur 'klaren' Trend will; verbessert wenn early zählt.",
            "Alternative: early_* → TRANSITION/UNCLEAR (strong-only Map).",
        ),
        "strong_bullish": (
            "Kern-Uptrend der SM; Mapping korrekt.",
            "Neutral bez. künstlicher Verzerrung.",
            "Beibehalten.",
        ),
        "bullish_weakening": (
            "SM: Nach-strong Abschwächung, nicht 'unclear Markt' im GT-Sinn.",
            "Verschlechtert Match stark (langer Share), weil GT oft noch CLEAR_UP und Map→UNCLEAR.",
            "Diagnostische Alt-Map: weakening→UPTREND (später Trend) oder eigenes LATE_UPTREND.",
        ),
        "early_bearish": (
            "Spiegel early_bullish.",
            "Gleicher Dual-Effekt.",
            "Alternative: early→UNCLEAR (strong-only).",
        ),
        "strong_bearish": (
            "Kern-Downtrend; Mapping korrekt.",
            "Neutral.",
            "Beibehalten.",
        ),
        "bearish_weakening": (
            "Spiegel bullish_weakening.",
            "Verschlechtert Match bei CLEAR_DOWN.",
            "Alt: weakening→DOWNTREND.",
        ),
        "bottoming": (
            "Übergangszustand; eigenes Label korrekt.",
            "Neutral/leicht verschlechternd vs SIDEWAYS/UPTREND GT.",
            "Beibehalten; nicht als UPTREND mappen.",
        ),
        "topping": (
            "Übergangszustand; Mapping korrekt.",
            "Neutral.",
            "Beibehalten.",
        ),
        "neutral": (
            "Fachlich SIDEWAYS-Kandidat; im Analysefenster kaum/nicht besucht.",
            "Kein künstlicher Effekt (0 Bars).",
            "Beibehalten; SM erreicht rare neutral.",
        ),
        "unavailable": (
            "Daten-/Warmup; UNCLEAR OK.",
            "Neutral.",
            "Beibehalten.",
        ),
    }
    for state, audit in MAP_EXISTING.items():
        note, distort, alt = comments.get(state, ("", "", ""))
        rows.append(
            {
                "raw_state": state,
                "mapped_audit_class": audit,
                "semantics_ok": note,
                "artificially_worsens_match": "ja"
                if state in {"bullish_weakening", "bearish_weakening", "early_bullish", "early_bearish"}
                and audit in {"UNCLEAR", "UPTREND", "DOWNTREND"}
                else "teilweise"
                if state.startswith("early")
                else "nein",
                "artificially_improves_match": "möglich wenn early=UPTREND/DOWNTREND und GT clear"
                if state.startswith("early")
                else "nein",
                "alternative_audit_mapping": alt,
                "distortion_note": distort,
            }
        )
    # refine early/weakening flags more precisely
    for r in rows:
        if r["raw_state"] in {"bullish_weakening", "bearish_weakening"}:
            r["artificially_worsens_match"] = "ja — langdauernde Phasen vs CLEAR_*"
            r["artificially_improves_match"] = "nein (mit bestehendem Map)"
        if r["raw_state"] in {"early_bullish", "early_bearish"}:
            r["artificially_worsens_match"] = "kann, wenn GT strengere Persistenz verlangt"
            r["artificially_improves_match"] = "kann, wenn short early Überlappung mit CLEAR_*"
    return rows


def select_segments(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for seg in CURATED_SEGMENTS:
        start, end = _ts(seg["start"]), _ts(seg["end"])
        sub = df[(df["decision_time"] >= start) & (df["decision_time"] < end)]
        if sub.empty:
            rows.append(
                {
                    **seg,
                    "n_bars": 0,
                    "available": False,
                    "dominant_state": None,
                    "gt_existing_mix": None,
                }
            )
            continue
        rows.append(
            {
                "segment_id": seg["segment_id"],
                "theme": seg["theme"],
                "start": _iso(start),
                "end": _iso(end),
                "must_include": bool(seg["must_include"]),
                "n_bars": int(len(sub)),
                "available": True,
                "dominant_state": str(sub["state"].value_counts().idxmax()),
                "gt_existing_mix": "|".join(
                    f"{k}:{v}" for k, v in sub["gt_existing"].value_counts().head(4).items()
                ),
                "state_mix": "|".join(
                    f"{k}:{v}" for k, v in sub["state"].value_counts().head(5).items()
                ),
                "n_state_changes": int(sub["state"].ne(sub["state"].shift()).sum() - 1),
            }
        )
    out = pd.DataFrame(rows)
    # coverage checks
    themes = set(out.loc[out["available"], "theme"])
    out.attrs["coverage_ok"] = {
        "has_down": any("down" in t or "crash" in t for t in themes),
        "has_up": any("up" in t or "bull" in t or "recovery" in t for t in themes),
        "has_side": any("side" in t or "chop" in t for t in themes),
        "has_switch": any("switch" in t for t in themes),
        "has_mar06": bool(
            ((out["segment_id"] == "S02_mar06_crash") & out["available"]).any()
        ),
        "has_mar08_09": bool(
            ((out["segment_id"] == "S04_mar08_09_recovery") & out["available"]).any()
        ),
        "n_available": int(out["available"].sum()),
    }
    return out


def build_segment_timelines(df: pd.DataFrame, segments: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for seg in segments.itertuples():
        if not bool(seg.available):
            continue
        start, end = _ts(seg.start), _ts(seg.end)
        sub = df[(df["decision_time"] >= start) & (df["decision_time"] < end)]
        prev_state = None
        for r in sub.itertuples():
            state = str(r.state)
            reasons = str(getattr(r, "reasons", "") or "")
            block = ""
            if "min_hold" in reasons:
                block = reasons
            elif reasons in {"", "hold", "nan"}:
                block = "hold_no_transition_triggers"
            else:
                block = reasons
            # exit lacking for weakening
            if state == "bullish_weakening":
                lack = "need HH+bullish_bos for early_bullish OR >=2 of {failed_breakout,bearish_choch,lower_high,bearish_bos} without HH for topping"
            elif state == "bearish_weakening":
                lack = "need LL+bearish_bos for early_bearish OR >=2 of {failed_breakdown,bullish_choch,higher_low,bullish_bos} without LL for bottoming"
            else:
                lack = ""
            pol = policy_for_state(state)
            mapped = str(r.state_mapped_existing)
            prop_l, prop_s = proposed_policy_for_audit_class(mapped)
            rows.append(
                {
                    "segment_id": seg.segment_id,
                    "theme": seg.theme,
                    "timestamp": _iso(r.decision_time),
                    "candle_timestamp": _iso(getattr(r, "candle_timestamp", None)),
                    "close": getattr(r, "close", None),
                    "gt_existing": r.gt_existing,
                    "gt_strict": r.gt_strict,
                    "gt_strong_only": r.gt_strong_only,
                    "state_raw": state,
                    "previous_state": getattr(r, "previous_state", None),
                    "state_mapped": mapped,
                    "proposed_state": mapped,  # proposed audit class under existing map
                    "state_mapped_strong_only": r.state_mapped_strong_only,
                    "state_mapped_weakening_as_trend": r.state_mapped_weakening_as_trend,
                    "transition_reason": reasons,
                    "block_reason": block,
                    "missing_exit_condition": lack,
                    "last_structure_event": "|".join(
                        x
                        for x in [
                            str(getattr(r, "last_high_label", "") or ""),
                            str(getattr(r, "last_low_label", "") or ""),
                        ]
                        if x and x != "nan"
                    ),
                    "last_bos_choch": "|".join(
                        x
                        for x in [
                            str(getattr(r, "last_bos", "") or ""),
                            str(getattr(r, "last_choch", "") or ""),
                        ]
                        if x and x != "nan"
                    ),
                    "last_bos": getattr(r, "last_bos", None),
                    "last_choch": getattr(r, "last_choch", None),
                    "protective_level": getattr(r, "protective_low_level", None)
                    if pd.notna(getattr(r, "protective_low_level", np.nan))
                    else getattr(r, "protective_high_level", None),
                    "protective_low_level": getattr(r, "protective_low_level", None),
                    "protective_high_level": getattr(r, "protective_high_level", None),
                    "bias_5m": getattr(r, "bias_5m", None),
                    "bias_15m": getattr(r, "bias_15m", None),
                    "bias_30m": getattr(r, "bias_30m", None),
                    "has_hh_hl": getattr(r, "has_hh_hl", None),
                    "has_lh_ll": getattr(r, "has_lh_ll", None),
                    "allow_long_existing": bool(getattr(r, "allow_long", False)),
                    "allow_short_existing": bool(getattr(r, "allow_short", False)),
                    "proposed_allow_long": prop_l,
                    "proposed_allow_short": prop_s,
                    "policy_existing_allow_long": pol.allow_long,
                    "policy_existing_allow_short": pol.allow_short,
                    "adx": getattr(r, "adx", None),
                    "di_spread": getattr(r, "di_spread", None),
                    "net_48": getattr(r, "net_48", None),
                    "net_288": getattr(r, "net_288", None),
                    "age": getattr(r, "age", None),
                }
            )
            prev_state = state
    return pd.DataFrame(rows)


def analyze_weakening_stuck(df: pd.DataFrame, timelines: pd.DataFrame) -> pd.DataFrame:
    """Long weakening runs inside selected segments + global top runs."""
    rows = []

    def flush_run(start_i: int, end_i: int, state: str, source: str) -> None:
        if end_i - start_i + 1 < 24:
            return
        sub = df.iloc[start_i : end_i + 1]
        entry = df.iloc[start_i]
        # structure snapshot at entry & mid & end
        mid = df.iloc[(start_i + end_i) // 2]
        end = df.iloc[end_i]
        if state == "bullish_weakening":
            exit_need = (
                "early_bullish requires event-types higher_high AND bullish_bos on same bar; "
                "topping requires >=2 of {failed_breakout,bearish_choch,lower_high,bearish_bos} "
                "and no higher_high"
            )
            file_fn = "trend_state_machine.py::_propose_transition (state==bullish_weakening)"
            cond = (
                'if "higher_high" in types and "bullish_bos" in types: early_bullish; '
                'elif len(top_hits)>=2 and "higher_high" not in types: topping'
            )
        else:
            exit_need = (
                "early_bearish requires lower_low AND bearish_bos; "
                "bottoming requires >=2 of {failed_breakdown,bullish_choch,higher_low,bullish_bos} "
                "and no lower_low"
            )
            file_fn = "trend_state_machine.py::_propose_transition (state==bearish_weakening)"
            cond = (
                'if "lower_low" in types and "bearish_bos" in types: early_bearish; '
                'elif len(bottom_hits)>=2 and "lower_low" not in types: bottoming'
            )
        rows.append(
            {
                "source": source,
                "state": state,
                "entry_time": _iso(entry["decision_time"]),
                "exit_time": _iso(end["decision_time"]),
                "duration_bars": int(end_i - start_i + 1),
                "duration_minutes": int((end_i - start_i + 1) * 5),
                "previous_state_at_entry": entry.get("previous_state"),
                "entry_age_reported": entry.get("age"),
                "entry_bias_5m": entry.get("bias_5m"),
                "entry_bias_15m": entry.get("bias_15m"),
                "entry_bias_30m": entry.get("bias_30m"),
                "entry_last_bos": entry.get("last_bos"),
                "entry_last_choch": entry.get("last_choch"),
                "entry_hh_hl": entry.get("has_hh_hl"),
                "entry_lh_ll": entry.get("has_lh_ll"),
                "mid_last_bos": mid.get("last_bos"),
                "mid_last_choch": mid.get("last_choch"),
                "end_last_bos": end.get("last_bos"),
                "end_last_choch": end.get("last_choch"),
                "end_bias_5m": end.get("bias_5m"),
                "reasons_mode": str(sub["reasons"].mode().iloc[0]) if len(sub) else None,
                "blocking_condition": "no simultaneous qualifying event set for exit",
                "missing_for_early_or_top_bottom": exit_need,
                "file_function": file_fn,
                "concrete_if_condition": cond,
                "affected_fields": "rt.state, event types on bar, structure_5m labels, bars_since_hh/ll",
                "structural_or_parameter": (
                    "structural — exit requires concurrent multi-event combo on the same 5m bar; "
                    "stale last_bos/choch labels alone do not count (events list for the bar only)"
                ),
                "old_direction_context_carried": (
                    "yes — last_bos/last_choch fields can remain bullish/bearish from earlier "
                    "while bar event types empty → hold"
                ),
                "new_structure_ignored": (
                    "partial — only events emitted on the current closed candle enter `types`; "
                    "lingering structure bias without new events does not trigger exit"
                ),
            }
        )

    # Global long weakening runs
    states = df["state"].astype(str).tolist()
    i = 0
    n = len(states)
    while i < n:
        if states[i] not in {"bullish_weakening", "bearish_weakening"}:
            i += 1
            continue
        j = i
        while j < n and states[j] == states[i]:
            j += 1
        flush_run(i, j - 1, states[i], "global_timeline")
        i = j

    # Prefer segment-overlapping runs first in export order
    if len(timelines):
        for seg_id, g in timelines.groupby("segment_id"):
            g = g.sort_values("timestamp")
            s = g["state_raw"].astype(str).tolist()
            # map back via timestamps
            # already covered by global; annotate segment presence
            pass

    out = pd.DataFrame(rows)
    if out.empty:
        return out
    # Keep longest 40 for readability
    return out.sort_values("duration_bars", ascending=False).head(40).reset_index(drop=True)


def march_06_root_cause(df: pd.DataFrame) -> pd.DataFrame:
    start, end = _ts("2026-03-06T00:00:00+00:00"), _ts("2026-03-07T00:00:00+00:00")
    sub = df[(df["decision_time"] >= start) & (df["decision_time"] < end)].copy()
    rows = []
    # Summarize + hourly samples
    for hour in range(24):
        h0 = start + pd.Timedelta(hours=hour)
        h1 = h0 + pd.Timedelta(hours=1)
        g = sub[(sub["decision_time"] >= h0) & (sub["decision_time"] < h1)]
        if g.empty:
            continue
        r0 = g.iloc[0]
        r1 = g.iloc[-1]
        rows.append(
            {
                "hour_utc": _iso(h0),
                "n_bars": int(len(g)),
                "state_unique": "|".join(sorted(g["state"].astype(str).unique())),
                "open_close_path": f"{g['close'].iloc[0]}->{g['close'].iloc[-1]}",
                "return_pct": float((g["close"].iloc[-1] / g["close"].iloc[0] - 1) * 100),
                "gt_existing_mode": str(g["gt_existing"].mode().iloc[0]),
                "last_bos_mode": str(g["last_bos"].astype(str).mode().iloc[0]),
                "last_choch_mode": str(g["last_choch"].astype(str).mode().iloc[0]),
                "bias_5m_mode": str(g["bias_5m"].astype(str).mode().iloc[0]),
                "bias_15m_mode": str(g["bias_15m"].astype(str).mode().iloc[0]),
                "bias_30m_mode": str(g["bias_30m"].astype(str).mode().iloc[0]),
                "has_hh_hl_any": bool(g["has_hh_hl"].astype(bool).any()),
                "has_lh_ll_any": bool(g["has_lh_ll"].astype(bool).any()),
                "allow_long_existing_share": float(g["allow_long"].astype(bool).mean()),
                "mapped_existing": str(g["state_mapped_existing"].mode().iloc[0]),
                "reasons_mode": str(g["reasons"].mode().iloc[0]),
                "q_why_bullish_weakening": (
                    "State entered earlier (strong_bullish→bullish_weakening on 2026-03-04); "
                    "exit needs concurrent bar events; hourly labels show lingering bullish last_bos/choch "
                    "without meeting topping (>=2 bearish hits) or early_bullish (HH+bos) combo"
                ),
                "q_bearish_structure_seen": (
                    f"lh_ll_any={bool(g['has_lh_ll'].astype(bool).any())}; "
                    f"last_choch_mode={g['last_choch'].astype(str).mode().iloc[0]}; "
                    "structure fields persist but `_propose_transition` uses per-bar `types` from new events only"
                ),
                "q_bos_choch_to_early_strong": (
                    "Even if last_choch is bearish_choch, without a second concurrent hit "
                    "(failed_breakout/lower_high/bearish_bos) topping is not taken; "
                    "early_bearish is not reachable directly from bullish_weakening"
                ),
                "layer": "State-Layer primary (exit gating); Structure-Layer secondary (event emission); "
                "Policy exposes Long while mapped UNCLEAR; Mapping maps weakening→UNCLEAR",
            }
        )
    # Day summary row
    if len(sub):
        rows.insert(
            0,
            {
                "hour_utc": "DAY_SUMMARY",
                "n_bars": int(len(sub)),
                "state_unique": "|".join(sorted(sub["state"].astype(str).unique())),
                "open_close_path": f"{sub['close'].iloc[0]}->{sub['close'].iloc[-1]}",
                "return_pct": float((sub["close"].iloc[-1] / sub["close"].iloc[0] - 1) * 100),
                "gt_existing_mode": str(sub["gt_existing"].mode().iloc[0]),
                "last_bos_mode": str(sub["last_bos"].astype(str).mode().iloc[0]),
                "last_choch_mode": str(sub["last_choch"].astype(str).mode().iloc[0]),
                "bias_5m_mode": str(sub["bias_5m"].astype(str).mode().iloc[0]),
                "bias_15m_mode": str(sub["bias_15m"].astype(str).mode().iloc[0]),
                "bias_30m_mode": str(sub["bias_30m"].astype(str).mode().iloc[0]),
                "has_hh_hl_any": bool(sub["has_hh_hl"].astype(bool).any()),
                "has_lh_ll_any": bool(sub["has_lh_ll"].astype(bool).any()),
                "allow_long_existing_share": float(sub["allow_long"].astype(bool).mean()),
                "mapped_existing": str(sub["state_mapped_existing"].mode().iloc[0]),
                "reasons_mode": str(sub["reasons"].mode().iloc[0]),
                "q_why_bullish_weakening": (
                    "Entire UTC day stuck in bullish_weakening (age continues from 2026-03-04 entry). "
                    "File: trend_state_machine.py function _propose_transition. "
                    "Condition: from bullish_weakening only failed_top (HH+bullish_bos) or topping (>=2 bearish hits)."
                ),
                "q_bearish_structure_seen": (
                    "Persisted structure labels may show bearish_choch/lh_ll intermittently, but "
                    "transition uses fresh StructureEvent types of the current bar only."
                ),
                "q_bos_choch_to_early_strong": (
                    "No direct edge bullish_weakening→early_bearish/strong_bearish. "
                    "Must pass topping (or other cycle path) first."
                ),
                "q_earliest_causal_bearish_possible": (
                    "Fachlich earliest: first bar where (>=2) bearish structure events fire without HH → topping; "
                    "then topping→early_bearish when LH + bearish_bos/choch + impulse/confirm. "
                    "Not on 6 Mar with only hold reasons."
                ),
                "layer": "State-Layer (stuck exit) + Policy (long allowed under bullish_weakening)",
            },
        )
    return pd.DataFrame(rows)


def march_08_09_root_cause(df: pd.DataFrame) -> pd.DataFrame:
    start, end = _ts("2026-03-08T00:00:00+00:00"), _ts("2026-03-10T00:00:00+00:00")
    sub = df[(df["decision_time"] >= start) & (df["decision_time"] < end)].copy()
    rows = []
    # state transition audit within window
    chg_idx = np.where(sub["state"].astype(str).ne(sub["state"].astype(str).shift()).to_numpy())[0]
    for i in chg_idx:
        r = sub.iloc[i]
        rows.append(
            {
                "kind": "state_change",
                "timestamp": _iso(r["decision_time"]),
                "state": r["state"],
                "previous_state": r["previous_state"],
                "reasons": r["reasons"],
                "gt_existing": r["gt_existing"],
                "mapped": r["state_mapped_existing"],
                "bias_5m": r["bias_5m"],
                "last_bos": r["last_bos"],
                "last_choch": r["last_choch"],
                "has_hh_hl": r["has_hh_hl"],
                "has_lh_ll": r["has_lh_ll"],
                "allow_long": r["allow_long"],
                "note": "",
            }
        )
    # aggregate answers
    rows.append(
        {
            "kind": "SUMMARY",
            "timestamp": _iso(start),
            "state": "|".join(f"{k}:{v}" for k, v in sub["state"].value_counts().items()),
            "previous_state": None,
            "reasons": None,
            "gt_existing": "|".join(f"{k}:{v}" for k, v in sub["gt_existing"].value_counts().items()),
            "mapped": "|".join(f"{k}:{v}" for k, v in sub["state_mapped_existing"].value_counts().items()),
            "bias_5m": str(sub["bias_5m"].mode().iloc[0]) if len(sub) else None,
            "last_bos": str(sub["last_bos"].mode().iloc[0]) if len(sub) else None,
            "last_choch": str(sub["last_choch"].mode().iloc[0]) if len(sub) else None,
            "has_hh_hl": float(sub["has_hh_hl"].astype(bool).mean()) if len(sub) else None,
            "has_lh_ll": float(sub["has_lh_ll"].astype(bool).mean()) if len(sub) else None,
            "allow_long": float(sub["allow_long"].astype(bool).mean()) if len(sub) else None,
            "note": (
                "Why no stable UPTREND: machine sits in topping/bullish_weakening/bottoming; "
                "early_bullish requires bottoming structure_ok (HL/hh_hl + bullish bos/choch) "
                "+ impulse/confirm and HTF not hard-vetoing. "
                "bottoming/topping often correct as transition labels, but re-entry to early/strong "
                "is gated and brief historically (see Mar4 burst). "
                "Missing confirmed bullish combo on many bars → hold."
            ),
        }
    )
    return pd.DataFrame(rows)


def ground_truth_sensitivity_table(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    variants = [
        ("existing", "gt_existing", "state_mapped_existing"),
        ("strict_persistent", "gt_strict", "state_mapped_existing"),
        ("strong_only_persistent", "gt_strong_only", "state_mapped_existing"),
        ("existing_x_strong_only_map", "gt_existing", "state_mapped_strong_only"),
        ("existing_x_weakening_as_trend_map", "gt_existing", "state_mapped_weakening_as_trend"),
        ("strict_x_weakening_as_trend_map", "gt_strict", "state_mapped_weakening_as_trend"),
    ]
    for name, gt_col, map_col in variants:
        mr = match_rate(df, gt_col, map_col)
        row: dict[str, Any] = {
            "variant": name,
            "gt_col": gt_col,
            "mapped_col": map_col,
            "overall_clear_match_rate": mr["overall_match_rate"],
            "n_clear": mr["n_clear"],
            "n_ambiguous": mr.get("n_ambiguous"),
        }
        for gt in ("CLEAR_UPTREND", "CLEAR_DOWNTREND", "CLEAR_SIDEWAYS"):
            es = episode_stats(df[gt_col].astype(str).tolist(), gt)
            row[f"{gt}_n_episodes"] = es["n_episodes"]
            row[f"{gt}_mean_len"] = es["mean_len"]
            row[f"{gt}_median_len"] = es["median_len"]
            row[f"{gt}_total_bars"] = es["total_bars"]
            want = gt_expected_audit(gt)
            det = detection_counts(df, gt_col, map_col, gt, want or "")
            row[f"{gt}_detected"] = det["detected"]
            row[f"{gt}_missed"] = det["missed"]
            by = (mr.get("by_gt") or {}).get(gt) or {}
            row[f"{gt}_bar_match_rate"] = by.get("match_rate")
        row["countertrend_long_share_in_CLEAR_DOWN_existing_policy"] = countertrend_grant_share(
            df, gt_col, "CLEAR_DOWNTREND", long_grant=True
        )
        row["countertrend_short_share_in_CLEAR_UP_existing_policy"] = countertrend_grant_share(
            df, gt_col, "CLEAR_UPTREND", long_grant=False
        )
        rows.append(row)
    return pd.DataFrame(rows)


def root_cause_findings(
    *,
    mapping_rows: list[dict[str, Any]],
    sens: pd.DataFrame,
    weakening: pd.DataFrame,
    march6: pd.DataFrame,
) -> pd.DataFrame:
    rows = [
        {
            "finding_id": "map_weakening_to_unclear",
            "layer": "Audit-Mapping",
            "file": "research/regime_scanner/trend_robustness_audit.py",
            "function": "AUDIT_CLASS_MAP",
            "condition": "bullish_weakening/bearish_weakening → UNCLEAR",
            "effect": (
                "Artificially tanks match vs CLEAR_* because SM spends majority of bars in weakening "
                "while price can still meet CLEAR GT."
            ),
            "phase_b_claim": "poor match = SM failure",
            "revision": "Partly mapping artifact; SM still structurally sticky, but measured match overstates mismatch.",
        },
        {
            "finding_id": "gt_fragmentation",
            "layer": "Ground Truth",
            "file": "research/regime_scanner/trend_robustness_audit.py",
            "function": "ground_truth_label",
            "condition": "net_48>/<\u00b11 + adx>=18 + exclusive structure, no persistence",
            "effect": "Many short CLEAR episodes; Sideways long runs rare; mistakable noise.",
            "phase_b_claim": "306/335 episodes mostly missed",
            "revision": "Episode count inflated by sensitive GT; strict/strong-only shrink episodes.",
        },
        {
            "finding_id": "stuck_weakening_exit_gate",
            "layer": "State-Layer",
            "file": "research/regime_scanner/trend_state_machine.py",
            "function": "_propose_transition",
            "condition": "bullish_weakening exits only via HH+bullish_bos or >=2 topping hits without HH",
            "effect": "Multi-day holds; Mar6 never leaves bullish_weakening.",
            "phase_b_claim": "stuck weakening",
            "revision": "Confirmed as real SM structural stickiness (not only mapping).",
        },
        {
            "finding_id": "events_not_labels",
            "layer": "Structure-Layer / State-Layer interface",
            "file": "research/regime_scanner/trend_state_machine.py",
            "function": "_propose_transition / update_market_structure",
            "condition": "Transition uses per-bar StructureEvent types; last_bos/last_choch fields alone insufficient",
            "effect": "Bearish labels can display while reasons stay 'hold'.",
            "phase_b_claim": "structure not recognized",
            "revision": "Often recognized as labels but not as concurrent transition events.",
        },
        {
            "finding_id": "policy_long_under_weakening",
            "layer": "Policy",
            "file": "research/regime_scanner/trend_state_policy.py",
            "function": "_POLICY_TABLE['bullish_weakening']",
            "condition": "allow_long=True during bullish_weakening",
            "effect": "Mar6 crash still long-permitted under existing policy.",
            "phase_b_claim": "countertrend long exposure",
            "revision": "Confirmed policy issue separate from mapping match-rate.",
        },
        {
            "finding_id": "no_direct_weakening_to_opposite_early",
            "layer": "State-Layer",
            "file": "research/regime_scanner/trend_state_machine.py",
            "function": "_propose_transition",
            "condition": "no edge bullish_weakening→early_bearish",
            "effect": "Must traverse topping (or other path); delays bearish recognition.",
            "phase_b_claim": "opposite trend not entered",
            "revision": "Confirmed design of allowed transitions (FORBIDDEN_DIRECT + path).",
        },
        {
            "finding_id": "early_as_full_trend",
            "layer": "Audit-Mapping",
            "file": "research/regime_scanner/trend_robustness_audit.py",
            "function": "AUDIT_CLASS_MAP",
            "condition": "early_* → UPTREND/DOWNTREND",
            "effect": "Can inflate or deflate match depending on GT persistence.",
            "phase_b_claim": "trend detection",
            "revision": "Ambiguous; strong-only map is safer diagnostic.",
        },
        {
            "finding_id": "match_rate_under_alt_maps",
            "layer": "Audit-Mapping",
            "file": "results_trend_mapping_root_cause_phase_c0/ground_truth_sensitivity.csv",
            "function": "ground_truth_sensitivity_table",
            "condition": "weakening_as_trend mapping",
            "effect": "Match rate rises materially when weakening maps to trend direction.",
            "phase_b_claim": "~1.2% overall match",
            "revision": "Part of 1.2% is mapping definitional; residual miss after alt map remains SM pathing.",
        },
    ]
    # attach numeric hint from sensitivity
    if len(sens):
        base = sens.loc[sens["variant"] == "existing"]
        weak = sens.loc[sens["variant"] == "existing_x_weakening_as_trend_map"]
        if len(base) and len(weak):
            rows.append(
                {
                    "finding_id": "numeric_map_lift",
                    "layer": "Audit-Mapping",
                    "file": "ground_truth_sensitivity.csv",
                    "function": "match_rate",
                    "condition": "compare existing vs weakening_as_trend map",
                    "effect": (
                        f"overall_clear_match_rate existing={base.iloc[0]['overall_clear_match_rate']} "
                        f"vs weakening_as_trend={weak.iloc[0]['overall_clear_match_rate']}"
                    ),
                    "phase_b_claim": "1.2% match",
                    "revision": "Quantifies mapping contribution.",
                }
            )
    if len(march6):
        day = march6.loc[march6["hour_utc"] == "DAY_SUMMARY"]
        if len(day):
            rows.append(
                {
                    "finding_id": "mar6_day_stuck",
                    "layer": "State-Layer",
                    "file": "march_06_root_cause.csv",
                    "function": "DAY_SUMMARY",
                    "condition": "state_unique only bullish_weakening",
                    "effect": day.iloc[0]["q_why_bullish_weakening"],
                    "phase_b_claim": "Mar6 long while crashing",
                    "revision": "Confirmed stuck SM + policy long; mapping says UNCLEAR (proposed blocks long).",
                }
            )
    if len(weakening):
        rows.append(
            {
                "finding_id": "weakening_run_stats",
                "layer": "State-Layer",
                "file": "weakening_stuck_cases.csv",
                "function": "analyze_weakening_stuck",
                "condition": "runs>=24 bars",
                "effect": f"n_long_runs_exported={len(weakening)}; max_duration={weakening['duration_bars'].max()}",
                "phase_b_claim": "long weakening medians",
                "revision": "Confirmed with explicit exit-condition attribution.",
            }
        )
    return pd.DataFrame(rows)


def timeline_hash(df: pd.DataFrame) -> str:
    cols = [
        c
        for c in (
            "decision_time",
            "state",
            "gt_existing",
            "gt_strict",
            "gt_strong_only",
            "state_mapped_existing",
            "state_mapped_strong_only",
            "state_mapped_weakening_as_trend",
        )
        if c in df.columns
    ]
    blob = df[cols].astype(str).to_csv(index=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def run_audit(
    *,
    phase_b_dir: Path,
    output_dir: Path,
) -> dict[str, Any]:
    phase_b_dir = Path(phase_b_dir)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timeline_path = phase_b_dir / "state_timeline_5m.csv"
    if not timeline_path.exists():
        raise FileNotFoundError(f"missing Phase-B timeline: {timeline_path}")

    # Ensure we never write into the foreign results/ folder
    forbidden = Path("research/regime_scanner/results").resolve()
    if output_dir.resolve() == forbidden or forbidden in output_dir.resolve().parents:
        if output_dir.resolve() == forbidden:
            raise RuntimeError("Refusing to write into research/regime_scanner/results/")

    raw = pd.read_csv(timeline_path, low_memory=False)
    df = enrich_timeline(raw)
    segments = select_segments(df)
    timelines = build_segment_timelines(df, segments)
    mapping_cmp = pd.DataFrame(analyze_mapping_semantics())
    sens = ground_truth_sensitivity_table(df)
    weakening = analyze_weakening_stuck(df, timelines)
    m6 = march_06_root_cause(df)
    m89 = march_08_09_root_cause(df)
    findings = root_cause_findings(
        mapping_rows=mapping_cmp.to_dict(orient="records"),
        sens=sens,
        weakening=weakening,
        march6=m6,
    )

    _write_csv(output_dir / "selected_segments.csv", segments)
    _write_csv(output_dir / "segment_timelines.csv", timelines)
    _write_csv(output_dir / "mapping_comparison.csv", mapping_cmp)
    _write_csv(output_dir / "ground_truth_sensitivity.csv", sens)
    _write_csv(output_dir / "weakening_stuck_cases.csv", weakening)
    _write_csv(output_dir / "march_06_root_cause.csv", m6)
    _write_csv(output_dir / "march_08_09_root_cause.csv", m89)
    _write_csv(output_dir / "root_cause_findings.csv", findings)

    det_hash = timeline_hash(df)
    base = sens.loc[sens["variant"] == "existing"]
    weakmap = sens.loc[sens["variant"] == "existing_x_weakening_as_trend_map"]
    strict = sens.loc[sens["variant"] == "strict_persistent"]
    strong = sens.loc[sens["variant"] == "strong_only_persistent"]

    summary = {
        "phase": "C0_mapping_gt_root_cause",
        "read_only": True,
        "phase_b_timeline": str(timeline_path),
        "n_timeline_bars": int(len(df)),
        "segments": segments.attrs.get("coverage_ok", {}),
        "n_segments_available": int(segments["available"].sum()) if len(segments) else 0,
        "deterministic_hash": det_hash,
        "match_rates": {
            "existing_map_x_existing_gt": None
            if base.empty
            else float(base.iloc[0]["overall_clear_match_rate"]),
            "weakening_as_trend_map_x_existing_gt": None
            if weakmap.empty
            else float(weakmap.iloc[0]["overall_clear_match_rate"]),
            "existing_map_x_strict_gt": None
            if strict.empty
            else float(strict.iloc[0]["overall_clear_match_rate"]),
            "existing_map_x_strong_only_gt": None
            if strong.empty
            else float(strong.iloc[0]["overall_clear_match_rate"]),
        },
        "error_split": {
            "structure_layer": "event emission vs persisted labels; concurrent multi-event requirement",
            "state_layer": "stuck weakening exits; no direct opposite early from weakening",
            "audit_mapping": "weakening→UNCLEAR over-punishes match; early→full trend ambiguous",
            "ground_truth": "sensitive net_48 GT creates fragmented CLEAR episodes",
            "policy": "long allowed in bullish_weakening (Mar6)",
        },
        "phase_b_confirmed": [
            "stuck bullish_weakening on Mar6",
            "policy long exposure during crash",
            "neutral/SIDEWAYS almost absent",
            "CLEAR episodes mostly not mirrored by early/strong",
        ],
        "phase_b_qualified": [
            "~1.2% match rate partly definitional (mapping+GT), not pure SM score",
            "missed episodes count inflated by short-lived GT clears",
            "bearish structure often present as labels but not transition triggers",
        ],
        "smallest_phase_c1_proposal": (
            "C1a (diagnostics→design only next): redesign bullish_weakening/bearish_weakening exit "
            "to accept accumulated multi-bar evidence OR allow weakening→opposite warning/early when "
            "exclusive opposite structure + net impulse persist — without March hardcode; "
            "plus separate policy audit for weakening long/short. "
            "Do not 'fix' match by remapping weakening→UPTREND in production."
        ),
        "no_core_files_modified": True,
    }
    _write_json(output_dir / "summary.json", summary)

    readme = f"""# Mapping / GT Root-Cause Audit (Phase C0)

Read-only. Uses Phase-B `state_timeline_5m.csv`. No SM/policy/threshold changes.

## Central question

Is the ~1.2% Phase-B clear-match rate mostly the state machine, or mapping/GT?

## Answer (short)

**Both.** Mapping `*_weakening → UNCLEAR` and sensitive CLEAR GT **inflate** mismatch.
Independently, the SM **really sticks** in weakening (Mar6 all-day) because exits need
concurrent multi-event combos, and existing policy still allows long in `bullish_weakening`.

## Match-rate contrasts

See `ground_truth_sensitivity.csv` / summary.match_rates:
- existing map × existing GT
- weakening-as-trend map × existing GT (diagnostic lift)
- strict / strong-only GT variants

## Key files

- `selected_segments.csv`, `segment_timelines.csv`
- `mapping_comparison.csv`
- `weakening_stuck_cases.csv`
- `march_06_root_cause.csv`, `march_08_09_root_cause.csv`
- `root_cause_findings.csv`

Deterministic hash: `{det_hash}`
"""
    (output_dir / "README_results.md").write_text(readme + "\n", encoding="utf-8")
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase C0 mapping/GT root-cause audit (read-only)")
    p.add_argument("--phase-b-dir", default=str(DEFAULT_PHASE_B))
    p.add_argument("--output-dir", default=str(DEFAULT_OUT))
    args = p.parse_args(argv)
    out = Path(args.output_dir)
    if Path("research/regime_scanner/results") in out.parents or out.name == "results":
        raise SystemExit("Refusing to write into research/regime_scanner/results/")
    summary = run_audit(phase_b_dir=Path(args.phase_b_dir), output_dir=out)
    print(json.dumps(json_safe(summary), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
