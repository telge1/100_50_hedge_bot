"""Research-only adaptive second-leg staging by distance to full TP.

Distance is the causal price-path length used by ``price_at_fraction``:
``abs(full_trigger - first_leg_fill) / first_leg_fill * 100``.

Does not invent coverage/PnL economics — only selects price/qty fractions for
``build_stage_plan`` (residual_coverage remains the qty residual rule).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from research.backtests.second_leg_price_staging import (
    PriceDistribution,
    QtyDistribution,
    SecondLegPriceStagingConfig,
)


AdaptiveFamily = Literal["equal", "backloaded"]

ADAPTIVE_PROFILE_NAMES: tuple[str, ...] = ("adaptive_equal", "adaptive_backloaded")
ADAPTIVE_FAMILY_BY_PROFILE: dict[str, AdaptiveFamily] = {
    "adaptive_equal": "equal",
    "adaptive_backloaded": "backloaded",
}


class DistanceBucket(str, Enum):
    INVALID = "invalid"
    NON_POSITIVE = "non_positive"
    D_0_2 = "0_2"
    D_2_4 = "2_4"
    D_4_7 = "4_7"
    D_GT_7 = "gt_7"


# Explicit export statuses (replace blanket summary "unknown" in new outputs).
DISTANCE_STATUS_NOT_APPLICABLE_BEFORE_CYCLE4 = "not_applicable_before_cycle4"
DISTANCE_STATUS_CYCLE4_PENDING_NO_FOLLOWUP = "cycle4_pending_no_followup"
DISTANCE_STATUS_FIXED_PROFILE_NO_ADAPTIVE_BUCKET = "fixed_profile_no_adaptive_bucket"
DISTANCE_STATUS_INVALID_INPUTS = "invalid_inputs"
DISTANCE_STATUS_NON_POSITIVE = "non_positive_distance"
DISTANCE_STATUS_PLAN_REJECTED = "plan_rejected"
DISTANCE_STATUS_LEGACY_NONE = "none"

REAL_DISTANCE_BUCKETS: tuple[str, ...] = (
    DistanceBucket.D_0_2.value,
    DistanceBucket.D_2_4.value,
    DistanceBucket.D_4_7.value,
    DistanceBucket.D_GT_7.value,
)


def theoretical_bucket_label(bucket: DistanceBucket) -> str | None:
    """Map planner bucket enum to export label; invalid/non_positive → None."""
    if bucket in (DistanceBucket.INVALID, DistanceBucket.NON_POSITIVE):
        return None
    return bucket.value


def classify_distance_status(
    *,
    profile: str | None,
    max_cycle: int | None,
    distance_pct: float | None,
    bucket: DistanceBucket | str | None,
    has_c4_followup_plan: bool,
    plan_accepted: bool | None = None,
    adaptive: bool | None = None,
) -> str:
    """Resolve distance_status for export / audit (no blanket unknown)."""
    prof = str(profile or "").strip().lower()
    if prof == "legacy":
        return DISTANCE_STATUS_LEGACY_NONE

    is_adaptive = adaptive if adaptive is not None else is_adaptive_profile(prof)
    bucket_enum: DistanceBucket | None
    if isinstance(bucket, DistanceBucket):
        bucket_enum = bucket
    elif bucket is None or bucket == "":
        bucket_enum = None
    else:
        try:
            bucket_enum = DistanceBucket(str(bucket))
        except ValueError:
            bucket_enum = None

    if distance_pct is not None:
        b = bucket_enum if bucket_enum is not None else select_distance_bucket(distance_pct)
        if b == DistanceBucket.INVALID:
            return DISTANCE_STATUS_INVALID_INPUTS
        if b == DistanceBucket.NON_POSITIVE:
            return DISTANCE_STATUS_NON_POSITIVE
        if plan_accepted is False and is_adaptive:
            # Keep plan_rejected only when adaptive multi-stage was attempted but refused.
            # Bucket label remains available via theoretical_distance_bucket.
            return DISTANCE_STATUS_PLAN_REJECTED
        return b.value

    # No distance available
    if not is_adaptive:
        return DISTANCE_STATUS_FIXED_PROFILE_NO_ADAPTIVE_BUCKET

    mc = int(max_cycle or 0)
    if mc < 4:
        return DISTANCE_STATUS_NOT_APPLICABLE_BEFORE_CYCLE4
    if not has_c4_followup_plan:
        return DISTANCE_STATUS_CYCLE4_PENDING_NO_FOLLOWUP
    return DISTANCE_STATUS_INVALID_INPUTS


def summarize_bucket_key(row: dict[str, Any], *, profile: str | None = None) -> str:
    """Preferred grouping key for new summaries (never invents unknown)."""
    prof = str(profile or row.get("profile") or "").strip().lower()
    if prof == "legacy":
        return DISTANCE_STATUS_LEGACY_NONE
    for key in ("distance_status", "theoretical_distance_bucket", "distance_bucket"):
        val = row.get(key)
        if val is not None and str(val).strip() and str(val).strip().lower() != "unknown":
            return str(val)
    return classify_distance_status(
        profile=prof,
        max_cycle=int(row.get("max_cycle") or 0) if row.get("max_cycle") not in (None, "") else None,
        distance_pct=None,
        bucket=None,
        has_c4_followup_plan=False,
        adaptive=is_adaptive_profile(prof) if prof else None,
    )


@dataclass(frozen=True)
class AdaptiveStagePolicy:
    family: AdaptiveFamily
    bucket: DistanceBucket
    original_distance_pct: float
    price_fractions: tuple[float, ...]
    qty_fractions: tuple[float, ...]

    @property
    def stage_count(self) -> int:
        return len(self.price_fractions)


# (price_fractions, qty_fractions) per family × bucket
_EQUAL_POLICIES: dict[DistanceBucket, tuple[tuple[float, ...], tuple[float, ...]]] = {
    DistanceBucket.D_0_2: ((0.50, 1.00), (0.50, 0.50)),
    DistanceBucket.D_2_4: ((0.33, 0.66, 1.00), (0.33, 0.33, 0.34)),
    DistanceBucket.D_4_7: ((0.25, 0.50, 0.75, 1.00), (0.25, 0.25, 0.25, 0.25)),
    DistanceBucket.D_GT_7: ((0.25, 0.50, 0.75, 1.00), (0.25, 0.25, 0.25, 0.25)),
}

_BACKLOADED_POLICIES: dict[DistanceBucket, tuple[tuple[float, ...], tuple[float, ...]]] = {
    DistanceBucket.D_0_2: ((0.50, 1.00), (0.35, 0.65)),
    DistanceBucket.D_2_4: ((0.25, 0.55, 1.00), (0.20, 0.30, 0.50)),
    DistanceBucket.D_4_7: ((0.20, 0.45, 0.70, 1.00), (0.15, 0.20, 0.25, 0.40)),
    DistanceBucket.D_GT_7: ((0.15, 0.35, 0.65, 1.00), (0.15, 0.20, 0.25, 0.40)),
}

_POLICIES: dict[AdaptiveFamily, dict[DistanceBucket, tuple[tuple[float, ...], tuple[float, ...]]]] = {
    "equal": _EQUAL_POLICIES,
    "backloaded": _BACKLOADED_POLICIES,
}


def is_adaptive_profile(name: str | None) -> bool:
    key = str(name or "").strip().lower()
    return key in ADAPTIVE_FAMILY_BY_PROFILE


def adaptive_family_for_profile(name: str) -> AdaptiveFamily:
    key = str(name or "").strip().lower()
    if key not in ADAPTIVE_FAMILY_BY_PROFILE:
        raise ValueError(f"not an adaptive profile: {name!r}")
    return ADAPTIVE_FAMILY_BY_PROFILE[key]


def compute_original_distance_pct(
    first_leg_fill: float,
    full_trigger: float,
) -> float | None:
    """Return path distance in percent, or None if inputs are invalid."""
    try:
        p0 = float(first_leg_fill)
        p_full = float(full_trigger)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p0) or not math.isfinite(p_full):
        return None
    if p0 <= 0.0 or p_full <= 0.0:
        return None
    return abs(p_full - p0) / p0 * 100.0


def select_distance_bucket(distance_pct: float | None) -> DistanceBucket:
    """Bucket edges: 0 < d <= 2, 2 < d <= 4, 4 < d <= 7, d > 7."""
    if distance_pct is None:
        return DistanceBucket.INVALID
    try:
        d = float(distance_pct)
    except (TypeError, ValueError):
        return DistanceBucket.INVALID
    if not math.isfinite(d):
        return DistanceBucket.INVALID
    if d <= 0.0:
        return DistanceBucket.NON_POSITIVE
    if d <= 2.0:
        return DistanceBucket.D_0_2
    if d <= 4.0:
        return DistanceBucket.D_2_4
    if d <= 7.0:
        return DistanceBucket.D_4_7
    return DistanceBucket.D_GT_7


def select_adaptive_policy(
    profile_name: str,
    distance_pct: float | None,
) -> AdaptiveStagePolicy | None:
    """Return policy for a positive finite distance; None ⇒ no adaptive multi-stage."""
    family = adaptive_family_for_profile(profile_name)
    bucket = select_distance_bucket(distance_pct)
    if bucket in (DistanceBucket.INVALID, DistanceBucket.NON_POSITIVE):
        return None
    if distance_pct is None:
        return None
    price, qty = _POLICIES[family][bucket]
    return AdaptiveStagePolicy(
        family=family,
        bucket=bucket,
        original_distance_pct=float(distance_pct),
        price_fractions=price,
        qty_fractions=qty,
    )


def adaptive_base_config(profile_name: str) -> SecondLegPriceStagingConfig:
    """Enabled adaptive stub config; fractions are replaced at plan time."""
    key = str(profile_name or "").strip().lower()
    if key not in ADAPTIVE_FAMILY_BY_PROFILE:
        raise ValueError(f"unknown adaptive profile: {profile_name!r}")
    # Placeholder 2-stage fractions — overwritten by shim before build_stage_plan.
    return SecondLegPriceStagingConfig(
        enabled=True,
        profile_name=key,
        apply_to=("long_primary_short_reduce",),
        mode="price_and_qty",
        stage_count=2,
        price_distribution=PriceDistribution(
            mode="custom_fractions", fractions=(0.50, 1.00)
        ),
        qty_distribution=QtyDistribution(mode="fixed_fractions", fractions=(0.50, 0.50)),
        last_stage_mode="residual_coverage",
        min_stage_notional_usdt=5.0,
        insufficient_size_fallback="reduce_stage_count",
        only_cycles=(4,),
        adaptive=True,
    )


def config_with_adaptive_policy(
    base: SecondLegPriceStagingConfig,
    policy: AdaptiveStagePolicy,
) -> SecondLegPriceStagingConfig:
    from dataclasses import replace

    return replace(
        base,
        stage_count=policy.stage_count,
        price_distribution=PriceDistribution(
            mode="custom_fractions", fractions=policy.price_fractions
        ),
        qty_distribution=QtyDistribution(
            mode="fixed_fractions", fractions=policy.qty_fractions
        ),
        last_stage_mode="residual_coverage",
        adaptive=True,
    )


def adaptive_diagnostics_payload(
    *,
    policy: AdaptiveStagePolicy | None,
    distance_pct: float | None,
    bucket: DistanceBucket,
    plan_accepted: bool,
    plan_stage_count: int,
    fallback_used: str | None,
    residual_qty: float | None,
    stages: list[dict[str, Any]] | tuple[Any, ...] = (),
    diagnostic_only: bool = False,
    profile_name: str | None = None,
) -> dict[str, Any]:
    selected_n = int(policy.stage_count) if policy is not None else 0
    effective_n = int(plan_stage_count) if plan_accepted else 0
    skipped = max(selected_n - effective_n, 0) if selected_n and plan_accepted else (
        selected_n if policy is not None and not plan_accepted else 0
    )
    theoretical = theoretical_bucket_label(bucket)
    # Adaptive: distance_bucket mirrors theoretical when valid.
    # Fixed profiles (TEM): keep distance_bucket empty for policy semantics; export theoretical.
    if diagnostic_only:
        export_bucket = None
    else:
        export_bucket = theoretical if theoretical is not None else bucket.value
    status = classify_distance_status(
        profile=profile_name,
        max_cycle=4,
        distance_pct=distance_pct,
        bucket=bucket,
        has_c4_followup_plan=True,
        plan_accepted=plan_accepted if not diagnostic_only else True,
        adaptive=not diagnostic_only,
    )
    if diagnostic_only and theoretical is not None:
        # TEM with causal distance: status = theoretical bucket (not fixed_profile_no_*).
        status = theoretical
    return {
        "original_distance_pct": distance_pct,
        "distance_bucket": export_bucket,
        "theoretical_distance_bucket": theoretical,
        "distance_status": status,
        "selected_stage_count": selected_n if policy is not None else None,
        "selected_price_fractions": list(policy.price_fractions) if policy else None,
        "selected_qty_fractions": list(policy.qty_fractions) if policy else None,
        "effective_stage_count_after_rounding": effective_n,
        "skipped_small_stages": int(skipped),
        "merged_stage_count": 0,
        "residual_qty": residual_qty,
        "fallback_used": fallback_used,
        "adaptive_family": policy.family if policy else None,
        "diagnostic_only": bool(diagnostic_only),
        "stage_specs": [
            {
                "stage_index": getattr(s, "stage_index", s.get("stage_index") if isinstance(s, dict) else None),
                "trigger_price": getattr(s, "trigger_price", s.get("trigger_price") if isinstance(s, dict) else None),
                "qty": getattr(s, "qty", s.get("qty") if isinstance(s, dict) else None),
            }
            for s in stages
        ],
    }
