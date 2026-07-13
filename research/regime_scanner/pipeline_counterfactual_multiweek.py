"""Multi-week helpers for C3 / M0–M3 pipeline counterfactual validation.

Research-only. Reuses ``pipeline_counterfactual`` / direction_gate / risk_off
without changing thresholds, live strategy, or productive pipeline logic.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Mapping, Sequence

import pandas as pd

from research.regime_scanner.direction_gate import DirectionGateConfig
from research.regime_scanner.pipeline_counterfactual import (
    classify_entry_quality,
    compute_forward_outcome,
    variant_config,
)
from research.regime_scanner.risk_off import RiskOffConfig

MultiVariant = Literal["M0", "M1", "M2", "M3"]
M_TO_C: dict[str, str] = {"M0": "C0", "M1": "C1", "M2": "C2", "M3": "C3"}
C_TO_M: dict[str, str] = {v: k for k, v in M_TO_C.items()}
MAIN_VARIANTS: tuple[MultiVariant, ...] = ("M0", "M1", "M2", "M3")
BLOCK_STATES = frozenset({"BLOCKED_AT_SETUP", "ABORTED_AT_PA", "ABORTED_DURING_CONFIRMATION"})
ENTRY_STATES = frozenset({"ENTRY_ALLOWED_AFTER_2", "ENTRY_ALLOWED_AFTER_3"})
MARCH_WEEK_START = pd.Timestamp("2026-03-01T00:00:00+00:00")
MARCH_WEEK_END = pd.Timestamp("2026-03-08T00:00:00+00:00")
EXPECTED_5M_PER_WEEK = 7 * 24 * 12  # 2016
COMPLETE_WEEK_COVERAGE = 0.98

# Fixed outcome labels (set before inspecting multi-week results).
QUALITY_GOOD = "good"
QUALITY_WEAK = "weak"
QUALITY_AMBIGUOUS = "ambiguous"

# B3 / R2 configs must remain exactly as previously tested (gates stay disabled).
B3_CONFIG = DirectionGateConfig(variant="B3", enabled=False)
R2_CONFIG = RiskOffConfig(variant="R2", enabled=False)


@dataclass(frozen=True)
class WeekWindow:
    week_id: str
    start: pd.Timestamp
    end: pd.Timestamp
    is_complete: bool
    n_5m_candles: int
    expected_5m_candles: int
    coverage_ratio: float
    is_known_march_week: bool
    is_out_of_sample: bool
    skip_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "week_id": self.week_id,
            "week_start": self.start.isoformat(),
            "week_end": self.end.isoformat(),
            "is_complete": self.is_complete,
            "n_5m_candles": self.n_5m_candles,
            "expected_5m_candles": self.expected_5m_candles,
            "coverage_ratio": self.coverage_ratio,
            "is_known_march_week": self.is_known_march_week,
            "is_out_of_sample": self.is_out_of_sample,
            "skip_reason": self.skip_reason,
        }


def to_utc(ts: object) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def multi_variant_config(variant: MultiVariant | str):
    key = str(variant).upper()
    if key not in M_TO_C:
        raise ValueError(f"unknown multiweek variant: {variant!r}")
    return variant_config(M_TO_C[key])


def assert_gate_configs_unchanged() -> dict[str, Any]:
    """Document and assert research gates stay disabled with prior variants."""
    assert B3_CONFIG.enabled is False
    assert B3_CONFIG.variant == "B3"
    assert R2_CONFIG.enabled is False
    assert R2_CONFIG.variant == "R2"
    cfg_m3 = multi_variant_config("M3")
    assert cfg_m3.enabled is False
    assert cfg_m3.use_b3 is True and cfg_m3.use_r2 is True
    assert cfg_m3.confirm_candles_normal == 2
    assert cfg_m3.confirm_candles_elevated == 3
    return {
        "b3": {"variant": B3_CONFIG.variant, "enabled": B3_CONFIG.enabled},
        "r2": {"variant": R2_CONFIG.variant, "enabled": R2_CONFIG.enabled},
        "m3_confirm_normal": cfg_m3.confirm_candles_normal,
        "m3_confirm_elevated": cfg_m3.confirm_candles_elevated,
    }


def map_quality_label(raw: object) -> str:
    q = str(raw or "").strip().lower()
    if q == "good":
        return QUALITY_GOOD
    if q == "weak":
        return QUALITY_WEAK
    if q in {"mixed", "unknown", "ambiguous", ""}:
        return QUALITY_AMBIGUOUS
    return QUALITY_AMBIGUOUS


def enrich_forward_outcome(
    candles_5m: pd.DataFrame,
    entry_ts: object,
    entry_price: float,
    side: str,
    *,
    horizon_bars: int = 72,
) -> dict[str, Any]:
    """Extend fixed forward metrics with 120m and +0.50% without changing decisions."""
    base = compute_forward_outcome(
        candles_5m, entry_ts, entry_price, side, horizon_bars=horizon_bars
    )
    out = dict(base)
    out["entry_quality_raw"] = out.get("entry_quality")
    out["entry_quality"] = map_quality_label(out.get("entry_quality"))
    out["reached_plus_050"] = None
    out["minutes_to_050"] = None
    out["adverse_120m"] = None
    out["favorable_120m"] = None
    out["max_adverse_before_025"] = None
    out["max_favorable_before_strong_adverse"] = None
    out["outcome_confidence"] = "low"
    if not out.get("evaluable"):
        # Data end / insufficient future must not invent a weak label.
        if out.get("reason") == "INSUFFICIENT_FUTURE_CANDLES":
            out["entry_quality"] = QUALITY_AMBIGUOUS
            out["outcome_confidence"] = "data_end"
        return out

    try:
        et = to_utc(entry_ts)
    except (TypeError, ValueError):
        return out

    frame = candles_5m.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    dec = (
        pd.to_datetime(frame["decision_time"], utc=True)
        if "decision_time" in frame.columns
        else frame["timestamp"] + pd.Timedelta(minutes=5)
    )
    future = frame.loc[dec > et].head(int(horizon_bars))
    side_l = str(side or "").strip().lower()
    reached_025 = False
    reached_050 = False
    min_050: float | None = None
    max_adv_before_025 = 0.0
    max_fav_before_strong_adv = 0.0
    strong_adv_hit = False
    adv_120 = fav_120 = None

    for offset, (_, row) in enumerate(future.iterrows(), start=1):
        hi = float(row["high"]) if pd.notna(row.get("high")) else None
        lo = float(row["low"]) if pd.notna(row.get("low")) else None
        if hi is None or lo is None:
            continue
        if side_l == "long":
            fav = max(0.0, (hi - entry_price) / abs(entry_price) * 100.0)
            adv = max(0.0, (entry_price - lo) / abs(entry_price) * 100.0)
        else:
            fav = max(0.0, (entry_price - lo) / abs(entry_price) * 100.0)
            adv = max(0.0, (hi - entry_price) / abs(entry_price) * 100.0)
        if not reached_025:
            max_adv_before_025 = max(max_adv_before_025, adv)
            if fav >= 0.25:
                reached_025 = True
        if not reached_050 and fav >= 0.50:
            reached_050 = True
            min_050 = float(offset * 5)
        if not strong_adv_hit:
            max_fav_before_strong_adv = max(max_fav_before_strong_adv, fav)
            if adv >= 1.0:
                strong_adv_hit = True
        if offset == 24:
            adv_120, fav_120 = adv, fav

    out["reached_plus_050"] = bool(reached_050)
    out["minutes_to_050"] = min_050
    out["adverse_120m"] = adv_120
    out["favorable_120m"] = fav_120
    out["max_adverse_before_025"] = float(max_adv_before_025)
    out["max_favorable_before_strong_adverse"] = float(max_fav_before_strong_adv)
    avail = int(out.get("available_bars") or 0)
    if avail >= 24 and out.get("reached_plus_025") is not None:
        out["outcome_confidence"] = "high"
    elif avail >= 12:
        out["outcome_confidence"] = "medium"
    else:
        out["outcome_confidence"] = "low"
        if avail < 6:
            out["entry_quality"] = QUALITY_AMBIGUOUS
    return out


def slice_weeks(
    candle_timestamps: Sequence[pd.Timestamp] | pd.Series,
    *,
    range_start: object,
    range_end: object,
    march_start: object = MARCH_WEEK_START,
    march_end: object = MARCH_WEEK_END,
    expected_bars: int = EXPECTED_5M_PER_WEEK,
    complete_ratio: float = COMPLETE_WEEK_COVERAGE,
) -> list[WeekWindow]:
    """Non-overlapping 7-day windows from ``range_start`` (exclusive end)."""
    start = to_utc(range_start).floor("D")
    end = to_utc(range_end)
    march_s = to_utc(march_start)
    march_e = to_utc(march_end)
    ts = pd.to_datetime(pd.Series(list(candle_timestamps)), utc=True)
    weeks: list[WeekWindow] = []
    cur = start
    while cur < end:
        we = min(cur + pd.Timedelta(days=7), end)
        n = int(((ts >= cur) & (ts < we)).sum())
        expected = int(round(expected_bars * ((we - cur) / pd.Timedelta(days=7))))
        expected = max(expected, 1)
        coverage = n / float(expected)
        full_seven = (we - cur) == pd.Timedelta(days=7)
        is_complete = bool(full_seven and coverage >= complete_ratio)
        # Exact Mar 1–8 window OR any research week overlapping that prior audit span.
        overlaps_march = (cur < march_e) and (we > march_s)
        is_exact_march = cur == march_s and we == march_e
        is_march = bool(is_exact_march or overlaps_march)
        skip = None
        if not full_seven:
            skip = "partial_tail_window"
        elif not is_complete:
            skip = "insufficient_candle_coverage"
        weeks.append(
            WeekWindow(
                week_id=f"W_{cur.date().isoformat()}",
                start=cur,
                end=we,
                is_complete=is_complete,
                n_5m_candles=n,
                expected_5m_candles=expected,
                coverage_ratio=float(coverage),
                is_known_march_week=is_march,
                is_out_of_sample=bool(is_complete and not is_march),
                skip_reason=skip if not is_complete else None,
            )
        )
        cur = we
    return weeks


def assign_week_id(ts: object, weeks: Sequence[WeekWindow]) -> str | None:
    if ts is None or (isinstance(ts, float) and math.isnan(ts)):
        return None
    try:
        t = to_utc(ts)
    except (TypeError, ValueError):
        return None
    for w in weeks:
        if w.start <= t < w.end:
            return w.week_id
    return None


def classify_market_phase(
    candles_5m: pd.DataFrame,
    week_start: object,
    week_end: object,
) -> dict[str, Any]:
    """Retrospective weekly phase label for grouping only (never feeds gates)."""
    start = to_utc(week_start)
    end = to_utc(week_end)
    c = candles_5m.copy()
    c["timestamp"] = pd.to_datetime(c["timestamp"], utc=True)
    w = c[(c["timestamp"] >= start) & (c["timestamp"] < end)].sort_values("timestamp")
    if w.empty or len(w) < 48:
        return {
            "market_phase": "insufficient_data",
            "net_return_pct": None,
            "range_pct": None,
            "realized_vol_pct": None,
        }
    o = float(w.iloc[0]["open"])
    cl = float(w.iloc[-1]["close"])
    hi = float(w["high"].max())
    lo = float(w["low"].min())
    net = (cl - o) / abs(o) * 100.0 if o else 0.0
    rng = (hi - lo) / abs(o) * 100.0 if o else 0.0
    rets = w["close"].pct_change().dropna() * 100.0
    vol = float(rets.std()) if len(rets) else 0.0
    mid = w.iloc[len(w) // 2]
    mid_close = float(mid["close"])
    first_half = (mid_close - o) / abs(o) * 100.0 if o else 0.0
    second_half = (cl - mid_close) / abs(mid_close) * 100.0 if mid_close else 0.0

    # Order matters: specific structures before generic trends.
    if first_half <= -2.0 and second_half >= 2.0 and net > -1.0:
        phase = "v_recovery"
    elif first_half >= 1.5 and second_half <= -1.5 and abs(net) < 1.5 and rng >= 4.0:
        phase = "failed_breakout_distribution"
    elif first_half <= -1.5 and second_half >= 1.5 and abs(net) < 1.5 and rng >= 4.0:
        phase = "failed_breakdown_accumulation"
    elif abs(net) < 1.0 and rng >= 5.0 and vol >= 0.35:
        phase = "volatile_range"
    elif abs(net) < 1.0 and rng < 3.0:
        phase = "sideways"
    elif net >= 4.0 and vol >= 0.30:
        phase = "strong_uptrend"
    elif net <= -4.0 and vol >= 0.30:
        phase = "strong_downtrend"
    elif abs(net) >= 2.0 and vol < 0.25:
        phase = "quiet_trend"
    elif vol >= 0.35 and abs(net) < 2.5:
        phase = "choppy_transition"
    elif net >= 2.0:
        phase = "strong_uptrend"
    elif net <= -2.0:
        phase = "strong_downtrend"
    else:
        phase = "choppy_transition"

    return {
        "market_phase": phase,
        "net_return_pct": float(net),
        "range_pct": float(rng),
        "realized_vol_pct": float(vol),
        "first_half_return_pct": float(first_half),
        "second_half_return_pct": float(second_half),
    }


def timeline_state_shares(
    timeline: pd.DataFrame,
    *,
    state_col: str,
    states: Mapping[str, str],
    week_start: object,
    week_end: object,
    time_col: str = "decision_time",
) -> dict[str, Any]:
    """Share of bars in named states plus change count / mean duration."""
    out: dict[str, Any] = {f"share_{alias}": 0.0 for alias in states}
    out["n_state_changes"] = 0
    out["avg_state_duration_bars"] = None
    if timeline is None or timeline.empty or state_col not in timeline.columns:
        return out
    t = timeline.copy()
    t[time_col] = pd.to_datetime(t[time_col], utc=True)
    start, end = to_utc(week_start), to_utc(week_end)
    t = t[(t[time_col] >= start) & (t[time_col] < end)].sort_values(time_col)
    if t.empty:
        return out
    n = len(t)
    for alias, value in states.items():
        out[f"share_{alias}"] = float((t[state_col] == value).mean())
    changes = int((t[state_col] != t[state_col].shift(1)).sum()) - 1
    out["n_state_changes"] = max(changes, 0)
    # Mean run length
    run_id = (t[state_col] != t[state_col].shift(1)).cumsum()
    durations = t.groupby(run_id).size()
    out["avg_state_duration_bars"] = float(durations.mean()) if len(durations) else None
    out["n_bars"] = n
    return out


def classify_block_verdict(
    *,
    baseline_quality: object,
    blocked: bool,
    later_new_setup: bool = False,
    later_recovered_entry: bool = False,
) -> str:
    if not blocked:
        return "NOT_BLOCKED"
    q = map_quality_label(baseline_quality)
    if later_recovered_entry:
        return "BLOCKED_ENTRY_LATER_RECOVERED"
    if later_new_setup:
        base = "BLOCKED_ENTRY_REPLACED_BY_NEW_SETUP"
    else:
        base = None
    if q == QUALITY_WEAK:
        verdict = "TRUE_POSITIVE_BLOCK"
    elif q == QUALITY_GOOD:
        verdict = "FALSE_POSITIVE_BLOCK"
    else:
        verdict = "AMBIGUOUS_BLOCK"
    return base or verdict


def block_stage(final_state: object) -> str | None:
    st = str(final_state or "")
    if st == "BLOCKED_AT_SETUP":
        return "setup"
    if st == "ABORTED_AT_PA":
        return "pa"
    if st == "ABORTED_DURING_CONFIRMATION":
        return "confirmation"
    return None


def primary_gate_family(abort_reason: object) -> str | None:
    r = str(abort_reason or "")
    if r.startswith("B3_"):
        return "B3"
    if r.startswith("R2_"):
        return "R2"
    return None


def precision_recall_false_block(
    *,
    weak_prevented: int,
    good_prevented: int,
    good_allowed: int,
    n_weak_baseline: int,
) -> dict[str, float | None]:
    precision = (
        weak_prevented / (weak_prevented + good_prevented)
        if (weak_prevented + good_prevented)
        else None
    )
    recall = weak_prevented / n_weak_baseline if n_weak_baseline else None
    false_block = (
        good_prevented / (good_prevented + good_allowed)
        if (good_prevented + good_allowed)
        else None
    )
    return {
        "precision": precision,
        "recall": recall,
        "false_block_rate": false_block,
    }


def leave_one_week_out(
    weekly_rows: Sequence[Mapping[str, Any]],
    *,
    metric_keys: Sequence[str] = (
        "n_m0_entries",
        "n_m3_blocks_on_m0_entries",
        "n_good_blocked_m3",
        "n_weak_blocked_m3",
        "false_block_rate_m3",
        "precision_m3",
    ),
) -> list[dict[str, Any]]:
    """Aggregate metrics excluding each complete week once."""
    rows = [dict(r) for r in weekly_rows if r.get("is_complete")]
    out: list[dict[str, Any]] = []
    for i, held in enumerate(rows):
        kept = [r for j, r in enumerate(rows) if j != i]
        agg: dict[str, Any] = {
            "held_out_week_id": held.get("week_id"),
            "n_weeks_kept": len(kept),
        }
        for key in metric_keys:
            vals = [r.get(key) for r in kept if r.get(key) is not None]
            if not vals:
                agg[f"{key}_mean"] = None
                agg[f"{key}_sum"] = None
                continue
            numeric = [float(v) for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if not numeric:
                agg[f"{key}_mean"] = None
                agg[f"{key}_sum"] = None
                continue
            agg[f"{key}_mean"] = float(sum(numeric) / len(numeric))
            agg[f"{key}_sum"] = float(sum(numeric))
        # Recompute false-block from sums when possible
        good_b = sum(float(r.get("n_good_blocked_m3") or 0) for r in kept)
        weak_b = sum(float(r.get("n_weak_blocked_m3") or 0) for r in kept)
        good_a = sum(float(r.get("n_good_allowed_m3") or 0) for r in kept)
        pr = precision_recall_false_block(
            weak_prevented=int(weak_b),
            good_prevented=int(good_b),
            good_allowed=int(good_a),
            n_weak_baseline=int(sum(float(r.get("n_weak_m0") or 0) for r in kept)),
        )
        agg["precision_m3_recomputed"] = pr["precision"]
        agg["false_block_rate_m3_recomputed"] = pr["false_block_rate"]
        out.append(agg)
    return out


def weekly_stability(
    weekly_rows: Sequence[Mapping[str, Any]],
    *,
    value_key: str,
) -> dict[str, Any]:
    vals = [
        float(r[value_key])
        for r in weekly_rows
        if r.get("is_complete") and r.get(value_key) is not None
    ]
    if not vals:
        return {
            "metric": value_key,
            "n": 0,
            "median": None,
            "min": None,
            "max": None,
            "std": None,
            "n_positive": 0,
            "n_negative": 0,
            "n_zero": 0,
        }
    s = pd.Series(vals, dtype=float)
    return {
        "metric": value_key,
        "n": int(len(vals)),
        "median": float(s.median()),
        "min": float(s.min()),
        "max": float(s.max()),
        "std": float(s.std(ddof=0)),
        "n_positive": int((s > 0).sum()),
        "n_negative": int((s < 0).sum()),
        "n_zero": int((s == 0).sum()),
    }


def no_double_count(ids: Iterable[object]) -> bool:
    xs = [str(x) for x in ids]
    return len(xs) == len(set(xs))


def decision_thresholds_scenarios(
    *,
    false_block_rate: float | None,
    n_weeks_with_weak_prevented: int,
    n_complete_weeks: int,
    b3_entry_blocks: int,
    r2_entry_blocks: int,
    third_candle_net_benefit: float | None,
    oos_false_block_rate: float | None,
    long_short_asymmetry: float | None,
) -> dict[str, Any]:
    """Report conservative / moderate / permissive pass-fail without fitting."""

    def evaluate(name: str, max_fbr: float, min_weak_weeks: int, require_b3: bool, require_3c: bool) -> dict[str, Any]:
        checks = {
            "false_block_rate_ok": (
                false_block_rate is not None and false_block_rate <= max_fbr
            ),
            "weak_prevention_spread_ok": n_weeks_with_weak_prevented >= min_weak_weeks,
            "b3_utility_ok": (b3_entry_blocks > 0) if require_b3 else True,
            "r2_present_ok": r2_entry_blocks > 0,
            "third_candle_ok": (
                (third_candle_net_benefit is not None and third_candle_net_benefit > 0)
                if require_3c
                else True
            ),
            "oos_false_block_ok": (
                oos_false_block_rate is not None and oos_false_block_rate <= max_fbr + 0.05
            ),
            "parity_ok": (
                long_short_asymmetry is None or long_short_asymmetry < 0.5
            ),
        }
        return {
            "scenario": name,
            "max_false_block_rate": max_fbr,
            "min_weeks_with_weak_prevented": min_weak_weeks,
            "require_b3_entry_blocks": require_b3,
            "require_third_candle_benefit": require_3c,
            "checks": checks,
            "passes": all(checks.values()),
        }

    return {
        "conservative": evaluate("conservative", 0.10, max(3, n_complete_weeks // 3), True, True),
        "moderate": evaluate("moderate", 0.20, max(2, n_complete_weeks // 4), True, False),
        "permissive": evaluate("permissive", 0.35, 1, False, False),
    }


def choose_recommendation(
    *,
    scenarios: Mapping[str, Any],
    b3_entry_blocks: int,
    r2_entry_blocks: int,
    r2_false_block_rate: float | None,
    b3_false_block_rate: float | None,
    third_candle_benefit: float | None,
    stable_without_march: bool,
    m0_reproduced: bool,
) -> dict[str, Any]:
    """Pick exactly one of A–E using qualitative rules (not post-hoc fitted cutoffs)."""
    if not m0_reproduced:
        return {
            "decision": "E",
            "label": "Stack verwerfen oder neu analysieren",
            "reason": "M0/C0 reproduction failed; do not proceed.",
        }

    moderate = scenarios.get("moderate") or {}
    permissive = scenarios.get("permissive") or {}
    r2_harmful = r2_false_block_rate is not None and r2_false_block_rate >= 0.25
    b3_useful = b3_entry_blocks > 0 and (
        b3_false_block_rate is None or b3_false_block_rate <= 0.25
    )
    r2_useful = r2_entry_blocks > 0 and not r2_harmful
    third_useful = third_candle_benefit is not None and third_candle_benefit > 0

    if (
        b3_useful
        and r2_useful
        and stable_without_march
        and moderate.get("passes")
    ):
        return {
            "decision": "A",
            "label": "M3 weiter validieren",
            "reason": "B3 and R2 both show measurable utility with moderate scenario pass and OOS stability.",
        }
    if b3_useful and (r2_harmful or not r2_useful):
        return {
            "decision": "B",
            "label": "Nur B3 weiterführen",
            "reason": "B3 useful on entry paths; R2 false-blocks too high or inconsistent.",
        }
    if r2_useful and not b3_useful and not r2_harmful:
        return {
            "decision": "C",
            "label": "Nur R2 weiterführen",
            "reason": "R2 selective utility; B3 still shows negligible entry-path benefit.",
        }
    if third_useful and r2_harmful:
        return {
            "decision": "D",
            "label": "Adaptive Confirmation ohne R2",
            "reason": "Third candle elevated path helps, but R2 hard-blocks are harmful.",
        }
    if permissive.get("passes") and (b3_useful or r2_useful) and stable_without_march:
        # Soft path still prefers narrowing rather than full M3.
        if b3_useful and not r2_useful:
            return {
                "decision": "B",
                "label": "Nur B3 weiterführen",
                "reason": "Only permissive scenario passes; retain B3 only.",
            }
        if r2_useful and not b3_useful:
            return {
                "decision": "C",
                "label": "Nur R2 weiterführen",
                "reason": "Only permissive scenario passes; retain R2 only.",
            }
    return {
        "decision": "E",
        "label": "Stack verwerfen oder neu analysieren",
        "reason": "Benefit unstable, false-blocks elevated, or baseline already filters most weak paths.",
    }
