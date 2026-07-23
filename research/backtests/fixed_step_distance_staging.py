"""Research-only fixed absolute %-step second-leg price staging.

Absolute stage distances are measured from ``first_leg_fill_price`` toward
``full_trigger``. Internally they become ``price_fraction = step / distance``.

Does not invent coverage/PnL economics — only selects price/qty fractions for
``build_stage_plan`` (``residual_coverage`` remains the qty residual rule).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Any, Literal

from research.backtests.adaptive_distance_staging import (
    DistanceBucket,
    classify_distance_status,
    select_distance_bucket,
    theoretical_bucket_label,
)
from research.backtests.second_leg_price_staging import (
    PriceDistribution,
    QtyDistribution,
    SecondLegPriceStagingConfig,
)

QtyFamily = Literal["equal", "backloaded"]

DEFAULT_MAX_STAGES = 8
# Float tolerance for "distance lands exactly on a grid step"
_EPS_PCT = 1e-9
_EPS_FRAC = 1e-12

FIXED_STEP_PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "fixed_step_1pct_equal": {"step_pct": 1.0, "qty": "equal"},
    "fixed_step_2pct_equal": {"step_pct": 2.0, "qty": "equal"},
    "fixed_step_2pct_backloaded": {"step_pct": 2.0, "qty": "backloaded"},
    # Optional contrast profile — not in default validation set.
    "fixed_step_1pct_backloaded": {"step_pct": 1.0, "qty": "backloaded"},
}

FIXED_STEP_PROFILE_NAMES: tuple[str, ...] = tuple(FIXED_STEP_PROFILE_SPECS.keys())


@dataclass(frozen=True)
class FixedStepPlan:
    step_pct: float
    qty_family: QtyFamily
    original_distance_pct: float
    requested_absolute_stage_distances_pct: tuple[float, ...]
    price_fractions: tuple[float, ...]
    qty_fractions: tuple[float, ...]
    requested_stage_count: int
    capped_stage_count: int
    stage_cap_applied: bool
    max_stages: int

    @property
    def stage_count(self) -> int:
        return len(self.price_fractions)


def is_fixed_step_profile(name: str | None) -> bool:
    key = str(name or "").strip().lower()
    return key in FIXED_STEP_PROFILE_SPECS


def fixed_step_spec(name: str) -> dict[str, Any]:
    key = str(name or "").strip().lower()
    if key not in FIXED_STEP_PROFILE_SPECS:
        raise ValueError(f"not a fixed-step profile: {name!r}")
    return dict(FIXED_STEP_PROFILE_SPECS[key])


def build_fixed_step_percentages(
    original_distance_pct: float,
    step_pct: float,
    *,
    max_stages: int = DEFAULT_MAX_STAGES,
    include_full_trigger: bool = True,
) -> tuple[float, ...]:
    """Absolute %-distances from first-leg fill, ending at full distance.

    Cap semantics (``max_stages`` includes the full-trigger stage):
    keep the first ``max_stages - 1`` intermediate grid steps that are strictly
    below ``original_distance_pct``, then always append the full distance.
    Never silently drop the full trigger.
    """
    d = float(original_distance_pct)
    step = float(step_pct)
    cap = int(max_stages)
    if not math.isfinite(d) or d <= 0.0:
        raise ValueError(f"original_distance_pct must be > 0, got {original_distance_pct!r}")
    if not math.isfinite(step) or step <= 0.0:
        raise ValueError(f"step_pct must be > 0, got {step_pct!r}")
    if cap < 1:
        raise ValueError(f"max_stages must be >= 1, got {max_stages!r}")

    intermediates: list[float] = []
    k = 1
    while True:
        abs_pct = k * step
        if abs_pct >= d - _EPS_PCT:
            break
        intermediates.append(abs_pct)
        k += 1
        if k > 10_000:
            raise RuntimeError("fixed-step grid explosion")

    # Cap: keep earliest intermediates + full trigger (interpretable, low bias).
    max_inter = max(0, cap - (1 if include_full_trigger else 0))
    capped = intermediates[:max_inter]

    out = list(capped)
    if include_full_trigger:
        if out and abs(out[-1] - d) <= _EPS_PCT:
            out[-1] = d
        else:
            out.append(d)
    if not out:
        out = [d]
    # Deduplicate exact duplicates (float-safe)
    cleaned: list[float] = []
    for x in out:
        if cleaned and abs(cleaned[-1] - x) <= _EPS_PCT:
            cleaned[-1] = max(cleaned[-1], x)
        else:
            cleaned.append(x)
    # Ensure last is exactly d when include_full_trigger
    if include_full_trigger:
        cleaned[-1] = d
    return tuple(cleaned)


def absolute_distances_to_price_fractions(
    absolute_distances_pct: tuple[float, ...] | list[float],
    original_distance_pct: float,
) -> tuple[float, ...]:
    d = float(original_distance_pct)
    if d <= 0:
        raise ValueError("original_distance_pct must be > 0")
    fracs: list[float] = []
    for abs_pct in absolute_distances_pct:
        f = float(abs_pct) / d
        if f <= 0:
            continue
        fracs.append(min(f, 1.0))
    if not fracs:
        return (1.0,)
    # Enforce strict increase + last exactly 1.0
    mono: list[float] = []
    for f in fracs:
        if mono and f <= mono[-1] + _EPS_FRAC:
            continue
        mono.append(f)
    mono[-1] = 1.0
    if len(mono) >= 2 and mono[-2] >= 1.0 - _EPS_FRAC:
        mono = mono[:-1]
        mono[-1] = 1.0
    return tuple(mono)


def equal_qty_fractions(n: int) -> tuple[float, ...]:
    if n < 1:
        raise ValueError("n must be >= 1")
    if n == 1:
        return (1.0,)
    base = 1.0 / n
    early = [base] * (n - 1)
    last = 1.0 - sum(early)
    return tuple(early + [last])


def backloaded_qty_fractions(
    n: int,
    *,
    min_early_share: float = 0.05,
) -> tuple[float, ...]:
    """Linear weights 1..n, with a floor on each early-stage share.

    Last config share absorbs the remainder for ``residual_coverage`` planning.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if n == 1:
        return (1.0,)
    weights = [float(i) for i in range(1, n + 1)]
    total_w = sum(weights)
    raw = [w / total_w for w in weights]
    # Floor early stages so 1%-backloaded does not instantly vanish under min-notional
    early_n = n - 1
    floor = float(min_early_share)
    max_early_budget = 1.0 - floor  # leave at least ``floor`` for last config share
    adjusted = list(raw)
    for i in range(early_n):
        if adjusted[i] < floor:
            adjusted[i] = floor
    early_sum = sum(adjusted[:early_n])
    if early_sum > max_early_budget:
        scale = max_early_budget / early_sum
        for i in range(early_n):
            adjusted[i] *= scale
        early_sum = sum(adjusted[:early_n])
    adjusted[-1] = max(1.0 - early_sum, floor)
    # Renormalize tiny float drift
    s = sum(adjusted)
    adjusted = [x / s for x in adjusted]
    return tuple(adjusted)


def select_fixed_step_plan(
    profile_name: str,
    distance_pct: float | None,
    *,
    max_stages: int = DEFAULT_MAX_STAGES,
) -> FixedStepPlan | None:
    """Return a multi-stage fixed-step plan, or None if staging should not activate."""
    if distance_pct is None:
        return None
    try:
        d = float(distance_pct)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(d) or d <= 0.0:
        return None
    spec = fixed_step_spec(profile_name)
    step = float(spec["step_pct"])
    qty_family: QtyFamily = spec["qty"]  # type: ignore[assignment]

    abs_uncapped = build_fixed_step_percentages(
        d, step, max_stages=10_000, include_full_trigger=True
    )
    requested_n = len(abs_uncapped)
    abs_capped = build_fixed_step_percentages(
        d, step, max_stages=max_stages, include_full_trigger=True
    )
    capped_n = len(abs_capped)
    cap_applied = capped_n < requested_n
    fracs = absolute_distances_to_price_fractions(abs_capped, d)
    n = len(fracs)
    if n <= 1:
        # Single-stage ≡ legacy fallback; still return plan for diagnostics with n=1
        return FixedStepPlan(
            step_pct=step,
            qty_family=qty_family,
            original_distance_pct=d,
            requested_absolute_stage_distances_pct=abs_uncapped,
            price_fractions=(1.0,),
            qty_fractions=(1.0,),
            requested_stage_count=requested_n,
            capped_stage_count=1,
            stage_cap_applied=cap_applied or requested_n > 1,
            max_stages=max_stages,
        )
    qty = (
        equal_qty_fractions(n)
        if qty_family == "equal"
        else backloaded_qty_fractions(n)
    )
    return FixedStepPlan(
        step_pct=step,
        qty_family=qty_family,
        original_distance_pct=d,
        requested_absolute_stage_distances_pct=abs_uncapped,
        price_fractions=fracs,
        qty_fractions=qty,
        requested_stage_count=requested_n,
        capped_stage_count=capped_n,
        stage_cap_applied=cap_applied,
        max_stages=max_stages,
    )


def fixed_step_base_config(profile_name: str) -> SecondLegPriceStagingConfig:
    key = str(profile_name or "").strip().lower()
    if key not in FIXED_STEP_PROFILE_SPECS:
        raise ValueError(f"unknown fixed-step profile: {profile_name!r}")
    # Placeholder fractions — overwritten at plan time by the shim.
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
        adaptive=False,
        fixed_step=True,
    )


def config_with_fixed_step_plan(
    base: SecondLegPriceStagingConfig,
    plan: FixedStepPlan,
) -> SecondLegPriceStagingConfig:
    return replace(
        base,
        stage_count=plan.stage_count,
        price_distribution=PriceDistribution(
            mode="custom_fractions", fractions=plan.price_fractions
        ),
        qty_distribution=QtyDistribution(
            mode="fixed_fractions", fractions=plan.qty_fractions
        ),
        last_stage_mode="residual_coverage",
        fixed_step=True,
    )


def effective_absolute_distances_from_plan_stages(
    *,
    first_leg_fill: float,
    stages: list[Any] | tuple[Any, ...],
) -> list[float]:
    out: list[float] = []
    p0 = float(first_leg_fill)
    if p0 <= 0:
        return out
    for s in stages:
        px = float(getattr(s, "trigger_price", s.get("trigger_price") if isinstance(s, dict) else 0.0) or 0.0)
        if px <= 0:
            continue
        out.append(abs(px - p0) / p0 * 100.0)
    return out


def fixed_step_diagnostics_payload(
    *,
    plan: FixedStepPlan | None,
    distance_pct: float | None,
    bucket: DistanceBucket,
    plan_accepted: bool,
    plan_stage_count: int,
    fallback_used: str | None,
    residual_qty: float | None,
    stages: list[Any] | tuple[Any, ...] = (),
    first_leg_fill: float | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    theoretical = theoretical_bucket_label(bucket)
    status = classify_distance_status(
        profile=profile_name,
        max_cycle=4,
        distance_pct=distance_pct,
        bucket=bucket,
        has_c4_followup_plan=True,
        plan_accepted=plan_accepted,
        adaptive=False,
    )
    if theoretical is not None and distance_pct is not None and distance_pct > 0:
        status = theoretical

    selected_n = int(plan.stage_count) if plan is not None else 0
    effective_n = int(plan_stage_count) if plan_accepted else 0
    skipped = max(selected_n - effective_n, 0) if selected_n and plan_accepted else (
        selected_n if plan is not None and not plan_accepted else 0
    )
    eff_abs: list[float] = []
    if first_leg_fill is not None and stages:
        eff_abs = effective_absolute_distances_from_plan_stages(
            first_leg_fill=first_leg_fill, stages=stages
        )
    eff_fracs = [
        float(getattr(s, "price_fraction", s.get("price_fraction") if isinstance(s, dict) else None) or 0.0)
        for s in stages
    ] if stages else None
    eff_qty = [
        float(getattr(s, "qty_fraction", s.get("qty_fraction") if isinstance(s, dict) else None) or 0.0)
        for s in stages
    ] if stages else None

    return {
        "grid_step_pct": plan.step_pct if plan else None,
        "original_distance_pct": distance_pct,
        "distance_bucket": theoretical,
        "theoretical_distance_bucket": theoretical,
        "distance_status": status,
        "requested_absolute_stage_distances_pct": (
            list(plan.requested_absolute_stage_distances_pct) if plan else None
        ),
        "effective_absolute_stage_distances_pct": eff_abs or None,
        "requested_price_fractions": list(plan.price_fractions) if plan else None,
        "effective_price_fractions": eff_fracs,
        "selected_price_fractions": list(plan.price_fractions) if plan else None,
        "requested_stage_count": plan.requested_stage_count if plan else None,
        "selected_stage_count": selected_n if plan is not None else None,
        "capped_stage_count": plan.capped_stage_count if plan else None,
        "effective_stage_count_after_rounding": effective_n,
        "stage_cap_applied": bool(plan.stage_cap_applied) if plan else False,
        "skipped_small_stages": int(skipped),
        "merged_stage_count": 0,
        "residual_qty": residual_qty,
        "fallback_used": fallback_used,
        "requested_qty_fractions": list(plan.qty_fractions) if plan else None,
        "selected_qty_fractions": list(plan.qty_fractions) if plan else None,
        "effective_qty_fractions": eff_qty,
        "adaptive_family": None,
        "fixed_step_qty_family": plan.qty_family if plan else None,
        "diagnostic_only": False,
    }
