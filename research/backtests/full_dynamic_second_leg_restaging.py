"""Research-only FULL_DYNAMIC residual restaging for LONG-primary SHORT_REDUCE.

Canonical economics (single source of truth per cycle):

  required_net_total  = initial_pending_cycle_loss_usdt + target_profit_usdt
                        (+ any explicit coverage buffer stored at plan time)
  confirmed_stage_realized_net = sum of confirmed SHORT_REDUCE stage nets
  remaining_required_net = max(required_net_total - confirmed_stage_realized_net, 0)

Derived (must not diverge):
  pending_cycle_loss_usdt := max(initial_pending - confirmed_stage_realized_net, 0)
  ≡ max(remaining_required_net - target_profit_usdt, 0)

Partial-dynamic profiles are untouched; only ``*_full_dynamic`` configs enable
the on_fill replan path installed by ``second_leg_price_staging_shim``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from research.backtests.adaptive_distance_staging import (
    compute_original_distance_pct,
    config_with_adaptive_policy,
    select_adaptive_policy,
    select_distance_bucket,
)
from research.backtests.fixed_step_distance_staging import (
    config_with_fixed_step_plan,
    select_fixed_step_plan,
)
from research.backtests.second_leg_price_staging import (
    SecondLegPriceStagingConfig,
    StagePlan,
    build_stage_plan,
    qty_for_target_net,
    resolve_grid_profile,
    short_reduce_expected_net,
)

ECONOMIC_TOLERANCE_USDT = 1e-6

FULL_DYNAMIC_BASE_PROFILE: dict[str, str] = {
    "two_early_medium_full_dynamic": "two_early_medium",
    "adaptive_equal_full_dynamic": "adaptive_equal",
    "fixed_step_1pct_equal_full_dynamic": "fixed_step_1pct_equal",
}

FULL_DYNAMIC_PROFILE_NAMES: tuple[str, ...] = tuple(FULL_DYNAMIC_BASE_PROFILE.keys())

# State keys (research-only)
FD_REQUIRED_TOTAL = "research_fd_required_net_total"
FD_INITIAL_PENDING = "research_fd_initial_pending"
FD_TARGET_PROFIT = "research_fd_target_profit"
FD_PLAN_REVISION = "research_fd_plan_revision"
FD_ANCHOR_PRICE = "research_fd_anchor_price"
FD_ORIGINAL_FULL_TRIGGER = "research_fd_original_full_trigger"
FD_EVENTS = "research_fd_replan_events"
FD_COVERED = "research_fd_cycle_covered"
FD_REPLAN_ACTIVE = "research_fd_replan_active"


def is_full_dynamic_profile(name: str | None) -> bool:
    key = str(name or "").strip().lower()
    return key in FULL_DYNAMIC_BASE_PROFILE or key.endswith("_full_dynamic")


def base_profile_name(full_dynamic_name: str) -> str:
    key = str(full_dynamic_name or "").strip().lower()
    if key in FULL_DYNAMIC_BASE_PROFILE:
        return FULL_DYNAMIC_BASE_PROFILE[key]
    if key.endswith("_full_dynamic"):
        return key[: -len("_full_dynamic")]
    return key


def resolve_full_dynamic_profile(name: str) -> SecondLegPriceStagingConfig:
    key = str(name or "").strip().lower()
    if key not in FULL_DYNAMIC_BASE_PROFILE:
        raise ValueError(f"unknown full-dynamic profile: {name!r}")
    base = resolve_grid_profile(FULL_DYNAMIC_BASE_PROFILE[key])
    return replace(base, profile_name=key, full_dynamic=True)


@dataclass(frozen=True)
class CanonicalCycleEconomics:
    """Immutable snapshot of the cycle's economic truth."""

    required_net_total: float
    confirmed_stage_realized_net: float
    remaining_required_net: float
    initial_pending_cycle_loss_usdt: float
    target_profit_usdt: float
    pending_cycle_loss_usdt: float

    @property
    def is_covered(self) -> bool:
        return self.remaining_required_net <= ECONOMIC_TOLERANCE_USDT


def compute_canonical_economics(
    *,
    required_net_total: float,
    confirmed_stage_realized_net: float,
    initial_pending_cycle_loss_usdt: float,
    target_profit_usdt: float,
) -> CanonicalCycleEconomics:
    req = max(float(required_net_total), 0.0)
    realized = max(float(confirmed_stage_realized_net), 0.0)
    remaining = max(req - realized, 0.0)
    initial_pending = max(float(initial_pending_cycle_loss_usdt), 0.0)
    target = max(float(target_profit_usdt), 0.0)
    pending = max(initial_pending - realized, 0.0)
    # Consistency: pending ≡ max(remaining - target, 0) when req = initial + target
    return CanonicalCycleEconomics(
        required_net_total=req,
        confirmed_stage_realized_net=realized,
        remaining_required_net=remaining,
        initial_pending_cycle_loss_usdt=initial_pending,
        target_profit_usdt=target,
        pending_cycle_loss_usdt=pending,
    )


def trigger_for_target_net(
    *,
    target_net: float,
    short_entry: float,
    qty: float,
    fee_rate: float,
) -> float:
    """Invert short_reduce_expected_net for trigger price."""
    q = float(qty)
    entry = float(short_entry)
    fee = float(fee_rate)
    rem = float(target_net)
    if q <= 1e-12:
        return entry
    denom = q * (1.0 + fee)
    if denom <= 1e-12:
        return entry
    return (entry * q * (1.0 - fee) - rem) / denom


def _normalize_qty(qty: float, qty_step: float) -> float:
    q = float(qty)
    step = float(qty_step or 0.0)
    if step <= 0:
        return max(q, 0.0)
    units = int(q / step + 1e-12)
    return max(units * step, 0.0)


def _normalize_price(price: float, tick: float) -> float:
    p = float(price)
    t = float(tick or 0.0)
    if t <= 0:
        return p
    units = int(p / t + 1e-12)
    return max(units * t, t)


def recompute_required_qty(
    *,
    remaining_required_net: float,
    short_entry: float,
    full_trigger: float,
    fee_rate: float,
    actual_short_qty: float,
    prior_remaining_stage_qty: float,
    qty_step: float,
) -> tuple[float, float]:
    """Return (recomputed_required_qty, rounding_residual_qty).

    Never exceeds actual short or prior residual sum (no unexplained growth).
    """
    raw = qty_for_target_net(
        target_net=remaining_required_net,
        short_entry=short_entry,
        trigger=full_trigger,
        fee_rate=fee_rate,
    )
    capped = min(float(raw), float(actual_short_qty), float(prior_remaining_stage_qty))
    if capped < 0:
        capped = 0.0
    rounded = _normalize_qty(capped, qty_step)
    # If rounding dropped below coverage need but inventory allows one more step, do not
    # inflate beyond prior residual / actual short.
    residual = max(capped - rounded, 0.0)
    return rounded, residual


def select_replan_config(
    *,
    profile_name: str,
    anchor_price: float,
    full_trigger: float,
    base_cfg: SecondLegPriceStagingConfig,
) -> SecondLegPriceStagingConfig:
    """Build a stage-plan config for the remaining distance after a fill."""
    key = str(profile_name or "").strip().lower()
    base_key = base_profile_name(key)
    distance_pct = compute_original_distance_pct(anchor_price, full_trigger)

    if base_key == "two_early_medium" or key.startswith("two_early_medium"):
        # Collapse to single stage when remaining distance cannot host two distinct prices.
        if distance_pct <= 0.05:
            return replace(
                base_cfg,
                stage_count=1,
                price_distribution=replace(
                    base_cfg.price_distribution, fractions=(1.0,)
                ),
                qty_distribution=replace(base_cfg.qty_distribution, fractions=(1.0,)),
                adaptive=False,
                fixed_step=False,
            )
        # Keep TEM fractions on the remaining path.
        from research.backtests.second_leg_price_staging import PriceDistribution, QtyDistribution

        return replace(
            base_cfg,
            stage_count=2,
            price_distribution=PriceDistribution(
                mode="custom_fractions", fractions=(0.40, 1.00)
            ),
            qty_distribution=QtyDistribution(
                mode="fixed_fractions", fractions=(0.35, 0.65)
            ),
            adaptive=False,
            fixed_step=False,
        )

    if "adaptive_equal" in base_key or "adaptive_equal" in key:
        policy = select_adaptive_policy("adaptive_equal", distance_pct)
        if policy is None:
            from research.backtests.second_leg_price_staging import PriceDistribution, QtyDistribution

            return replace(
                base_cfg,
                stage_count=1,
                price_distribution=PriceDistribution(
                    mode="custom_fractions", fractions=(1.0,)
                ),
                qty_distribution=QtyDistribution(
                    mode="fixed_fractions", fractions=(1.0,)
                ),
                adaptive=False,
                fixed_step=False,
            )
        return config_with_adaptive_policy(base_cfg, policy)

    if "fixed_step_1pct" in base_key or "fixed_step_1pct" in key:
        fs = select_fixed_step_plan("fixed_step_1pct_equal", distance_pct)
        if fs is None or fs.stage_count <= 1:
            from research.backtests.second_leg_price_staging import PriceDistribution, QtyDistribution

            return replace(
                base_cfg,
                stage_count=1,
                price_distribution=PriceDistribution(
                    mode="custom_fractions", fractions=(1.0,)
                ),
                qty_distribution=QtyDistribution(
                    mode="fixed_fractions", fractions=(1.0,)
                ),
                adaptive=False,
                fixed_step=False,
            )
        return config_with_fixed_step_plan(base_cfg, fs)

    return base_cfg


def build_residual_stage_plan(
    *,
    config: SecondLegPriceStagingConfig,
    cycle_index: int,
    purpose: str,
    anchor_price: float,
    remaining_required_net: float,
    remaining_qty: float,
    short_entry: float,
    fee_rate: float,
    price_tick: float,
    qty_step: float,
    min_order_qty: float,
    prior_full_trigger: float,
) -> tuple[StagePlan | None, float, str | None]:
    """Plan residual stages from anchor→new full trigger.

    Returns (plan_or_none, new_full_trigger, fallback_reason).
    """
    if remaining_required_net <= ECONOMIC_TOLERANCE_USDT or remaining_qty <= 0:
        return None, float(prior_full_trigger), "covered_or_zero_qty"

    # Seed trigger from prior full trigger / inventory, then re-solve exactly.
    seed_trigger = min(float(prior_full_trigger), float(anchor_price) - float(price_tick or 0.0) or float(prior_full_trigger))
    if seed_trigger <= 0 or seed_trigger >= float(anchor_price):
        seed_trigger = float(prior_full_trigger)

    qty = _normalize_qty(remaining_qty, qty_step)
    if qty <= 0:
        return None, seed_trigger, "qty_rounded_to_zero"

    new_full = trigger_for_target_net(
        target_net=remaining_required_net,
        short_entry=short_entry,
        qty=qty,
        fee_rate=fee_rate,
    )
    new_full = _normalize_price(new_full, price_tick)
    # Must be strictly deeper (lower) than anchor for long-primary short reduce.
    tick = float(price_tick or 0.0) or 1e-6
    if new_full >= float(anchor_price) - 1e-12:
        new_full = _normalize_price(float(anchor_price) - tick, price_tick)
    if new_full <= 0:
        return None, seed_trigger, "invalid_full_trigger"

    plan_cfg = select_replan_config(
        profile_name=config.profile_name,
        anchor_price=anchor_price,
        full_trigger=new_full,
        base_cfg=config,
    )
    # Disable adaptive/fixed_step flags on the frozen plan_cfg so build_stage_plan
    # uses the already-selected fractions (select_replan_config baked them in).
    plan_cfg = replace(plan_cfg, adaptive=False, fixed_step=False, enabled=True)

    plan = build_stage_plan(
        config=plan_cfg,
        cycle_index=cycle_index,
        purpose=purpose,
        first_leg_fill_price=float(anchor_price),
        full_trigger_price=float(new_full),
        total_qty=float(qty),
        required_net=float(remaining_required_net),
        short_entry_price=float(short_entry),
        fee_rate=float(fee_rate),
        price_tick=float(price_tick),
        qty_step=float(qty_step),
        min_order_qty=float(min_order_qty),
        direction="long_primary_short_reduce",
    )
    if not plan.accepted or not plan.stages:
        # Collapse to single full-cover stage.
        from research.backtests.second_leg_price_staging import PriceDistribution, QtyDistribution

        single = replace(
            plan_cfg,
            stage_count=1,
            price_distribution=PriceDistribution(
                mode="custom_fractions", fractions=(1.0,)
            ),
            qty_distribution=QtyDistribution(mode="fixed_fractions", fractions=(1.0,)),
        )
        plan = build_stage_plan(
            config=single,
            cycle_index=cycle_index,
            purpose=purpose,
            first_leg_fill_price=float(anchor_price),
            full_trigger_price=float(new_full),
            total_qty=float(qty),
            required_net=float(remaining_required_net),
            short_entry_price=float(short_entry),
            fee_rate=float(fee_rate),
            price_tick=float(price_tick),
            qty_step=float(qty_step),
            min_order_qty=float(min_order_qty),
            direction="long_primary_short_reduce",
        )
        if not plan.accepted or not plan.stages:
            return None, new_full, plan.rejection_reason or "plan_rejected"
        return plan, new_full, "collapsed_single_stage"

    # Drop any stage not strictly deeper than anchor (should not happen).
    filtered = tuple(
        s for s in plan.stages if float(s.trigger_price) < float(anchor_price) - 1e-12
    )
    if not filtered:
        return None, new_full, "no_deeper_stages"
    if len(filtered) != len(plan.stages):
        # Rebuild identity with contiguous indices via single-stage if broken.
        if len(filtered) == 1:
            from research.backtests.second_leg_price_staging import StageSpec

            only = filtered[0]
            plan = StagePlan(
                accepted=True,
                cycle_index=plan.cycle_index,
                purpose=plan.purpose,
                first_leg_fill_price=plan.first_leg_fill_price,
                full_trigger_price=plan.full_trigger_price,
                total_qty=only.qty,
                required_net=plan.required_net,
                stage_count=1,
                stages=(
                    StageSpec(
                        stage_index=0,
                        trigger_price=only.trigger_price,
                        qty=only.qty,
                        expected_net=only.expected_net,
                        notional=only.notional,
                        price_fraction=1.0,
                        qty_fraction=1.0,
                    ),
                ),
                fallback_used="filtered_deeper_only",
            )
        else:
            plan = replace(plan, stages=filtered, stage_count=len(filtered))
    return plan, new_full, plan.fallback_used


def _iter_runtime_active_orders(runtime_state: Any) -> list[Any]:
    raw = getattr(runtime_state, "active_orders", None)
    if raw is None:
        return []
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def collect_open_residual_staged_orders(
    snapshot: Any,
    runtime_state: Any,
    *,
    cycle_index: int,
    purpose: str,
) -> list[Any]:
    orders: list[Any] = []
    seen: set[int] = set()
    for o in _iter_runtime_active_orders(runtime_state):
        oid = id(o)
        if oid in seen:
            continue
        seen.add(oid)
        if str(getattr(o, "status", "") or "").upper() in {
            "FILLED",
            "CANCELLED",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
        }:
            continue
        pur = str(getattr(o, "purpose", "") or "")
        if pur != purpose:
            continue
        meta = getattr(o, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        if not (meta.get("is_staged_second_leg_tp") or meta.get("research_price_staging")):
            continue
        try:
            if int(meta.get("cycle_index") or 0) != int(cycle_index):
                continue
        except (TypeError, ValueError):
            pass
        orders.append(o)
    if snapshot is not None:
        for o in list(getattr(snapshot, "active_orders", []) or []):
            oid = id(o)
            if oid in seen:
                continue
            seen.add(oid)
            pur = str(getattr(o, "purpose", "") or "")
            if pur != purpose:
                continue
            meta = getattr(o, "metadata", None) or {}
            if not isinstance(meta, dict):
                meta = {}
            if meta.get("is_staged_second_leg_tp") or meta.get("research_price_staging"):
                orders.append(o)
    return orders


def init_cycle_economics_state(
    runtime_state: Any,
    *,
    cycle_index: int,
    required_net_total: float,
    initial_pending: float,
    target_profit: float,
    full_trigger: float,
) -> None:
    state = runtime_state.strategy_state
    ck = str(cycle_index)
    state.setdefault(FD_REQUIRED_TOTAL, {})[ck] = float(required_net_total)
    state.setdefault(FD_INITIAL_PENDING, {})[ck] = float(initial_pending)
    state.setdefault(FD_TARGET_PROFIT, {})[ck] = float(target_profit)
    state.setdefault(FD_PLAN_REVISION, {})[ck] = 0
    state.setdefault(FD_ORIGINAL_FULL_TRIGGER, {})[ck] = float(full_trigger)
    state.setdefault(FD_COVERED, {})[ck] = False
    state.setdefault(FD_EVENTS, [])


def sync_pending_from_canonical(
    runtime_state: Any, economics: CanonicalCycleEconomics
) -> None:
    """Keep pending_cycle_loss_usdt aligned with canonical remaining economics."""
    runtime_state.strategy_state["pending_cycle_loss_usdt"] = float(
        economics.pending_cycle_loss_usdt
    )


def read_canonical_economics(runtime_state: Any, cycle_index: int) -> CanonicalCycleEconomics | None:
    state = runtime_state.strategy_state
    ck = str(cycle_index)
    req_map = state.get(FD_REQUIRED_TOTAL) or {}
    if ck not in req_map:
        # Fallback to staged required map from initial plan.
        req = float((state.get("staged_second_leg_tp_required_net_total") or {}).get(ck) or 0.0)
        if req <= 0:
            return None
        initial_pending = float(state.get("pending_cycle_loss_usdt") or 0.0)
        target = float(
            (state.get(FD_TARGET_PROFIT) or {}).get(ck)
            or getattr(getattr(runtime_state, "config", None), "target_profit_usdt", 0.0)
            or 0.0
        )
        # Best-effort bootstrap
        if abs(req - (initial_pending + target)) > 1e-4:
            initial_pending = max(req - target, 0.0)
    else:
        req = float(req_map[ck])
        initial_pending = float((state.get(FD_INITIAL_PENDING) or {}).get(ck) or 0.0)
        target = float((state.get(FD_TARGET_PROFIT) or {}).get(ck) or 0.0)
    realized = float(
        (state.get("staged_second_leg_tp_realized_net") or {}).get(ck) or 0.0
    )
    return compute_canonical_economics(
        required_net_total=req,
        confirmed_stage_realized_net=realized,
        initial_pending_cycle_loss_usdt=initial_pending,
        target_profit_usdt=target,
    )


def append_replan_event(runtime_state: Any, event: dict[str, Any]) -> None:
    state = runtime_state.strategy_state
    events = state.setdefault(FD_EVENTS, [])
    events.append(dict(event))


__all__ = [
    "ECONOMIC_TOLERANCE_USDT",
    "FULL_DYNAMIC_BASE_PROFILE",
    "FULL_DYNAMIC_PROFILE_NAMES",
    "CanonicalCycleEconomics",
    "append_replan_event",
    "base_profile_name",
    "build_residual_stage_plan",
    "collect_open_residual_staged_orders",
    "compute_canonical_economics",
    "init_cycle_economics_state",
    "is_full_dynamic_profile",
    "qty_for_target_net",
    "read_canonical_economics",
    "recompute_required_qty",
    "resolve_full_dynamic_profile",
    "select_replan_config",
    "short_reduce_expected_net",
    "sync_pending_from_canonical",
    "trigger_for_target_net",
]
