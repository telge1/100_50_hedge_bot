"""Research-only multi-price second-leg staging planner (Long-Primary SHORT_REDUCE).

Default / absent config ≡ enabled=False → callers must leave the live strategy path alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal


ApplyTo = Literal["long_primary_short_reduce", "short_primary_long_reduce"]
StagingMode = Literal["price_and_qty", "price_only", "qty_only", "same_trigger"]
PriceDistMode = Literal["same_trigger", "linear_to_full_trigger", "custom_fractions"]
QtyDistMode = Literal["equal", "fixed_fractions", "coverage_weighted", "residual_last"]
LastStageMode = Literal["residual_qty", "residual_coverage", "fixed_fraction"]
FallbackMode = Literal[
    "reduce_stage_count",
    "merge_small_stages",
    "full_qty_at_full_trigger",
    "disable_staging",
]


@dataclass(frozen=True)
class PriceDistribution:
    mode: PriceDistMode = "custom_fractions"
    fractions: tuple[float, ...] = (0.25, 0.50, 0.75, 1.00)


@dataclass(frozen=True)
class QtyDistribution:
    mode: QtyDistMode = "fixed_fractions"
    fractions: tuple[float, ...] = (0.25, 0.25, 0.25, 0.25)


@dataclass(frozen=True)
class SecondLegPriceStagingConfig:
    """Backtest-only config. Never loaded from live JSON by default."""

    enabled: bool = False
    profile_name: str = "legacy"
    apply_to: tuple[ApplyTo, ...] = ("long_primary_short_reduce",)
    mode: StagingMode = "price_and_qty"
    stage_count: int = 4
    price_distribution: PriceDistribution = field(default_factory=PriceDistribution)
    qty_distribution: QtyDistribution = field(default_factory=QtyDistribution)
    last_stage_mode: LastStageMode = "residual_coverage"
    min_stage_notional_usdt: float = 5.0
    insufficient_size_fallback: FallbackMode = "reduce_stage_count"
    allow_deeper_than_full_trigger: bool = False
    # None ⇒ all cycles; e.g. (4,) isolates APT T3 Cycle-4 lab without earlier-path drift.
    only_cycles: tuple[int, ...] | None = None
    # Research adaptive distance staging: fractions chosen at plan time by distance bucket.
    adaptive: bool = False
    # Research fixed absolute %-step staging: fractions chosen at plan time from distance.
    fixed_step: bool = False
    # Research FULL_DYNAMIC: replan residual SHORT_REDUCE stages after each confirmed fill.
    # Partial-dynamic profiles keep this False (frozen residuals).
    full_dynamic: bool = False


@dataclass(frozen=True)
class StageSpec:
    stage_index: int
    trigger_price: float
    qty: float
    expected_net: float
    notional: float
    price_fraction: float
    qty_fraction: float


@dataclass(frozen=True)
class StagePlan:
    accepted: bool
    cycle_index: int
    purpose: str
    first_leg_fill_price: float
    full_trigger_price: float
    total_qty: float
    required_net: float
    stage_count: int
    stages: tuple[StageSpec, ...]
    rejection_reason: str | None = None
    fallback_used: str | None = None
    identity_keys: tuple[tuple[Any, ...], ...] = ()


def legacy_config() -> SecondLegPriceStagingConfig:
    return SecondLegPriceStagingConfig(enabled=False, profile_name="legacy")


def profile_linear4() -> SecondLegPriceStagingConfig:
    return SecondLegPriceStagingConfig(
        enabled=True,
        profile_name="linear4",
        stage_count=4,
        price_distribution=PriceDistribution(
            mode="custom_fractions", fractions=(0.25, 0.50, 0.75, 1.00)
        ),
        qty_distribution=QtyDistribution(
            mode="fixed_fractions", fractions=(0.25, 0.25, 0.25, 0.25)
        ),
        last_stage_mode="residual_coverage",
        only_cycles=(4,),
    )


def profile_conservative3() -> SecondLegPriceStagingConfig:
    return SecondLegPriceStagingConfig(
        enabled=True,
        profile_name="conservative3",
        stage_count=3,
        price_distribution=PriceDistribution(
            mode="custom_fractions", fractions=(0.30, 0.60, 1.00)
        ),
        qty_distribution=QtyDistribution(
            mode="fixed_fractions", fractions=(0.15, 0.25, 0.60)
        ),
        last_stage_mode="residual_coverage",
        only_cycles=(4,),
    )


def profile_small_early4() -> SecondLegPriceStagingConfig:
    return SecondLegPriceStagingConfig(
        enabled=True,
        profile_name="small_early4",
        stage_count=4,
        price_distribution=PriceDistribution(
            mode="custom_fractions", fractions=(0.20, 0.45, 0.70, 1.00)
        ),
        qty_distribution=QtyDistribution(
            mode="fixed_fractions", fractions=(0.10, 0.15, 0.25, 0.50)
        ),
        last_stage_mode="residual_coverage",
        only_cycles=(4,),
    )


PROFILE_BUILDERS = {
    "legacy": legacy_config,
    "linear4": profile_linear4,
    "conservative3": profile_conservative3,
    "small_early4": profile_small_early4,
}


# Grid-test profile table (research-only). Qty lists ending in residual are
# expanded so the last fraction absorbs the remainder (sum == 1.0).
GRID_PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "legacy": {"enabled": False},
    "two_equal": {
        "price_fractions": (0.50, 1.00),
        "qty_fractions": (0.50, 0.50),
    },
    "two_early_small": {
        "price_fractions": (0.35, 1.00),
        "qty_fractions": (0.25, 0.75),
    },
    "two_early_medium": {
        "price_fractions": (0.40, 1.00),
        "qty_fractions": (0.35, 0.65),
    },
    "three_equal": {
        "price_fractions": (0.33, 0.67, 1.00),
        "qty_fractions": (0.33, 0.33, 0.34),
    },
    "three_conservative": {
        "price_fractions": (0.30, 0.60, 1.00),
        "qty_fractions": (0.15, 0.25, 0.60),
    },
    "three_balanced": {
        "price_fractions": (0.25, 0.60, 1.00),
        "qty_fractions": (0.20, 0.30, 0.50),
    },
    "three_frontloaded": {
        "price_fractions": (0.25, 0.55, 1.00),
        "qty_fractions": (0.35, 0.30, 0.35),
    },
    "four_equal": {
        "price_fractions": (0.25, 0.50, 0.75, 1.00),
        "qty_fractions": (0.25, 0.25, 0.25, 0.25),
    },
    "four_small_early": {
        "price_fractions": (0.20, 0.45, 0.70, 1.00),
        "qty_fractions": (0.10, 0.15, 0.25, 0.50),
    },
    "four_conservative": {
        "price_fractions": (0.25, 0.50, 0.75, 1.00),
        "qty_fractions": (0.10, 0.20, 0.25, 0.45),
    },
    "four_frontloaded": {
        "price_fractions": (0.20, 0.40, 0.70, 1.00),
        "qty_fractions": (0.30, 0.25, 0.20, 0.25),
    },
    # Adaptive distance families — fractions selected at plan time (see adaptive_distance_staging).
    "adaptive_equal": {"enabled": True, "adaptive": "equal"},
    "adaptive_backloaded": {"enabled": True, "adaptive": "backloaded"},
    # Fixed absolute %-step grids — fractions selected at plan time (see fixed_step_distance_staging).
    "fixed_step_1pct_equal": {"enabled": True, "fixed_step": True},
    "fixed_step_2pct_equal": {"enabled": True, "fixed_step": True},
    "fixed_step_2pct_backloaded": {"enabled": True, "fixed_step": True},
    "fixed_step_1pct_backloaded": {"enabled": True, "fixed_step": True},
}

# Aliases: prior lab names map onto grid table where equivalent.
GRID_PROFILE_ALIASES = {
    "linear4": "four_equal",
    "conservative3": "three_conservative",
    "small_early4": "four_small_early",
}


def _build_grid_profile(name: str, spec: dict[str, Any]) -> SecondLegPriceStagingConfig:
    if not spec.get("enabled", True):
        return SecondLegPriceStagingConfig(enabled=False, profile_name=name)
    if spec.get("adaptive"):
        from research.backtests.adaptive_distance_staging import adaptive_base_config

        return adaptive_base_config(name)
    if spec.get("fixed_step"):
        from research.backtests.fixed_step_distance_staging import fixed_step_base_config

        return fixed_step_base_config(name)
    price = tuple(float(x) for x in spec["price_fractions"])
    qty = tuple(float(x) for x in spec["qty_fractions"])
    if len(price) != len(qty):
        raise ValueError(f"profile {name}: price/qty fraction length mismatch")
    return SecondLegPriceStagingConfig(
        enabled=True,
        profile_name=name,
        stage_count=len(price),
        price_distribution=PriceDistribution(mode="custom_fractions", fractions=price),
        qty_distribution=QtyDistribution(mode="fixed_fractions", fractions=qty),
        last_stage_mode="residual_coverage",
        only_cycles=(4,),
        adaptive=False,
        fixed_step=False,
    )


def list_grid_profile_names() -> list[str]:
    return list(GRID_PROFILE_SPECS.keys())


def resolve_grid_profile(name: str) -> SecondLegPriceStagingConfig:
    key = str(name or "legacy").strip().lower()
    # FULL_DYNAMIC variants resolve via dedicated helper (do not alter base specs).
    if key.endswith("_full_dynamic"):
        from research.backtests.full_dynamic_second_leg_restaging import (
            FULL_DYNAMIC_BASE_PROFILE,
            resolve_full_dynamic_profile,
        )

        if key in FULL_DYNAMIC_BASE_PROFILE:
            return resolve_full_dynamic_profile(key)
    key = GRID_PROFILE_ALIASES.get(key, key)
    if key not in GRID_PROFILE_SPECS:
        raise ValueError(f"unknown grid price-staging profile: {name!r}")
    cfg = _build_grid_profile(key, GRID_PROFILE_SPECS[key])
    errors = validate_config(cfg)
    if errors:
        raise ValueError(f"invalid grid profile {key}: {errors}")
    return cfg


def resolve_profile(name: str) -> SecondLegPriceStagingConfig:
    """Resolve lab builders first, then grid table (aliases map onto grid names)."""
    key = str(name or "legacy").strip().lower()
    builder = PROFILE_BUILDERS.get(key)
    if builder is not None:
        return builder()
    if key.endswith("_full_dynamic"):
        return resolve_grid_profile(key)
    if key in GRID_PROFILE_SPECS or key in GRID_PROFILE_ALIASES:
        return resolve_grid_profile(key)
    raise ValueError(f"unknown second_leg_price_staging profile: {name!r}")


def parse_profile_selection(spec: str) -> list[SecondLegPriceStagingConfig]:
    """Parse ``all`` or comma-separated profile names into configs (legacy first)."""
    raw = str(spec or "").strip()
    if not raw or raw.lower() == "all":
        names = list_grid_profile_names()
    else:
        names = [p.strip() for p in raw.split(",") if p.strip()]
    if not names:
        raise ValueError("empty profile selection")
    # Deduplicate preserving order; put legacy first when present.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        k = GRID_PROFILE_ALIASES.get(n.lower(), n.lower())
        if k in seen:
            continue
        seen.add(k)
        ordered.append(k)
    if "legacy" in ordered:
        ordered = ["legacy"] + [n for n in ordered if n != "legacy"]
    out: list[SecondLegPriceStagingConfig] = []
    for n in ordered:
        if n in PROFILE_BUILDERS and n not in GRID_PROFILE_SPECS:
            out.append(PROFILE_BUILDERS[n]())
        else:
            out.append(resolve_grid_profile(n))
    return out


def profile_definitions_payload() -> dict[str, Any]:
    """Serializable profile table for ``profile_definitions.yaml``."""
    payload: dict[str, Any] = {}
    for name, spec in GRID_PROFILE_SPECS.items():
        if not spec.get("enabled", True):
            payload[name] = {"enabled": False}
            continue
        if spec.get("adaptive"):
            payload[name] = {
                "enabled": True,
                "adaptive": spec["adaptive"],
                "last_stage_mode": "residual_coverage",
                "only_cycles": [4],
            }
            continue
        if spec.get("fixed_step"):
            from research.backtests.fixed_step_distance_staging import FIXED_STEP_PROFILE_SPECS

            fs = FIXED_STEP_PROFILE_SPECS.get(name, {})
            payload[name] = {
                "enabled": True,
                "fixed_step": True,
                "step_pct": fs.get("step_pct"),
                "qty": fs.get("qty"),
                "last_stage_mode": "residual_coverage",
                "only_cycles": [4],
            }
            continue
        payload[name] = {
            "enabled": True,
            "price_fractions": list(spec["price_fractions"]),
            "qty_fractions": list(spec["qty_fractions"]),
            "last_stage_mode": "residual_coverage",
            "only_cycles": [4],
        }
    return payload


def validate_config(config: SecondLegPriceStagingConfig) -> list[str]:
    """Return validation errors (empty ⇒ ok). Legacy/disabled skips nested checks."""
    if not config.enabled:
        return []
    errors: list[str] = []
    if config.stage_count < 1:
        errors.append("stage_count must be >= 1")
    if config.mode not in ("price_and_qty", "price_only", "qty_only", "same_trigger"):
        errors.append(f"unknown mode: {config.mode}")
    if config.last_stage_mode not in ("residual_qty", "residual_coverage", "fixed_fraction"):
        errors.append(f"unknown last_stage_mode: {config.last_stage_mode}")
    if config.insufficient_size_fallback not in (
        "reduce_stage_count",
        "merge_small_stages",
        "full_qty_at_full_trigger",
        "disable_staging",
    ):
        errors.append(f"unknown insufficient_size_fallback: {config.insufficient_size_fallback}")

    n = int(config.stage_count)
    price = config.price_distribution
    qty = config.qty_distribution
    if price.mode == "custom_fractions":
        if len(price.fractions) != n:
            errors.append("price fractions length must equal stage_count")
        if price.fractions:
            if any(f <= 0 or f > 1 for f in price.fractions):
                errors.append("price fractions must be in (0, 1]")
            if any(price.fractions[i] >= price.fractions[i + 1] for i in range(len(price.fractions) - 1)):
                errors.append("price fractions must be strictly increasing")
            if abs(price.fractions[-1] - 1.0) > 1e-9:
                errors.append("last price fraction must be 1.0")
    if qty.mode == "fixed_fractions":
        if len(qty.fractions) != n:
            errors.append("qty fractions length must equal stage_count")
        if qty.fractions:
            if any(f <= 0 or f > 1 for f in qty.fractions):
                errors.append("qty fractions must be in (0, 1]")
            total = sum(qty.fractions)
            if config.last_stage_mode == "fixed_fraction" and abs(total - 1.0) > 1e-9:
                errors.append("fixed qty fractions must sum to 1.0")
            if config.last_stage_mode in ("residual_qty", "residual_coverage") and total > 1.0 + 1e-9:
                errors.append("qty fractions sum must be <= 1.0 when last stage is residual")
    return errors


def stage_identity_key(*, cycle_index: int, purpose: str, stage_index: int) -> tuple[Any, ...]:
    return (int(cycle_index), str(purpose), int(stage_index))


def dedupe_staged_intents_by_identity(
    intents: list[Any],
    *,
    cycle_index: int,
    purpose: str,
) -> list[Any]:
    """Collapse only exact stage_index duplicates; keep distinct stages."""
    seen: set[int] = set()
    out: list[Any] = []
    for intent in intents:
        meta = dict(getattr(intent, "metadata", None) or {})
        if not meta.get("is_staged_second_leg_tp") and not meta.get("research_price_staging"):
            out.append(intent)
            continue
        idx = int(meta.get("stage_index") or 0)
        if idx in seen:
            continue
        seen.add(idx)
        out.append(intent)
    return out


def price_at_fraction(
    *,
    first_leg_fill: float,
    full_trigger: float,
    fraction: float,
    direction: Literal["long_primary_short_reduce", "short_primary_long_reduce"],
) -> float:
    """Interpolate along first→full path.

    Long-primary SHORT_REDUCE: P = P_first - f * (P_first - P_full)
    Short-primary LONG_REDUCE: P = P_first + f * (P_full - P_first)
    """
    f = float(fraction)
    p0 = float(first_leg_fill)
    p_full = float(full_trigger)
    if direction == "long_primary_short_reduce":
        return p0 - f * (p0 - p_full)
    return p0 + f * (p_full - p0)


def short_reduce_expected_net(
    *,
    short_entry: float,
    trigger: float,
    qty: float,
    fee_rate: float,
) -> float:
    entry = float(short_entry)
    tp = float(trigger)
    q = float(qty)
    fee = float(fee_rate)
    return (entry - tp) * q - (entry * q * fee) - (tp * q * fee)


def qty_for_target_net(
    *,
    target_net: float,
    short_entry: float,
    trigger: float,
    fee_rate: float,
) -> float:
    """Invert short_reduce_expected_net for qty (clamped at 0)."""
    entry = float(short_entry)
    tp = float(trigger)
    fee = float(fee_rate)
    denom = (entry - tp) - (entry * fee) - (tp * fee)
    if denom <= 1e-12 or target_net <= 0:
        return 0.0
    return float(target_net) / denom


def _normalize_qty(qty: float, qty_step: float) -> float:
    q = float(qty)
    step = float(qty_step or 0.0)
    if step <= 0:
        return max(q, 0.0)
    # floor to step
    units = int(q / step + 1e-12)
    return max(units * step, 0.0)


def _normalize_price(price: float, tick: float) -> float:
    p = float(price)
    t = float(tick or 0.0)
    if t <= 0:
        return p
    # For short-reduce triggers (prices going down), floor is conservative for coverage.
    units = int(p / t + 1e-12)
    return max(units * t, t)


def _price_fractions_for_count(config: SecondLegPriceStagingConfig, n: int) -> list[float]:
    if n <= 1:
        return [1.0]
    price = config.price_distribution
    if price.mode == "same_trigger":
        return [1.0] * n
    if price.mode == "linear_to_full_trigger" or len(price.fractions) != n:
        return [float(i + 1) / float(n) for i in range(n)]
    return [float(x) for x in price.fractions]


def _qty_early_fractions(config: SecondLegPriceStagingConfig, n: int) -> list[float]:
    """Fractions for stages 0..n-2; last is residual."""
    if n <= 1:
        return []
    qty = config.qty_distribution
    if qty.mode == "equal" or len(qty.fractions) != n:
        early = 1.0 / float(n)
        return [early] * (n - 1)
    # Use first n-1 fractions; leave residual for last regardless of last listed fraction.
    return [float(x) for x in qty.fractions[: n - 1]]


def _ensure_monotone_prices(
    prices: list[float],
    *,
    full_trigger: float,
    tick: float,
    direction: ApplyTo,
) -> list[float] | None:
    if not prices:
        return None
    out = list(prices)
    out[-1] = _normalize_price(full_trigger, tick)
    for i in range(len(out)):
        out[i] = _normalize_price(out[i], tick)
    for i in range(1, len(out)):
        if direction == "long_primary_short_reduce":
            if out[i] >= out[i - 1] - 1e-12:
                out[i] = _normalize_price(out[i - 1] - tick, tick)
            if out[i] <= 0:
                return None
        else:
            if out[i] <= out[i - 1] + 1e-12:
                out[i] = _normalize_price(out[i - 1] + tick, tick)
            if out[i] <= 0:
                return None
    out[-1] = _normalize_price(full_trigger, tick)
    if len(out) > 1 and direction == "long_primary_short_reduce":
        # full trigger must be deepest (lowest)
        if out[-1] >= out[-2]:
            return None
    return out


def build_stage_plan(
    *,
    config: SecondLegPriceStagingConfig,
    cycle_index: int,
    purpose: str,
    first_leg_fill_price: float,
    full_trigger_price: float,
    total_qty: float,
    required_net: float,
    short_entry_price: float,
    fee_rate: float,
    price_tick: float,
    qty_step: float,
    min_order_qty: float,
    direction: ApplyTo = "long_primary_short_reduce",
) -> StagePlan:
    """Build a multi-price stage plan with min-notional fallback."""
    base_kwargs = dict(
        cycle_index=int(cycle_index),
        purpose=str(purpose),
        first_leg_fill_price=float(first_leg_fill_price),
        full_trigger_price=float(full_trigger_price),
        total_qty=float(total_qty),
        required_net=float(required_net),
    )
    if not config.enabled:
        return StagePlan(accepted=False, stage_count=0, stages=(), rejection_reason="disabled", **base_kwargs)

    errors = validate_config(config)
    if errors:
        return StagePlan(
            accepted=False,
            stage_count=0,
            stages=(),
            rejection_reason="invalid_config:" + ",".join(errors),
            **base_kwargs,
        )

    if direction not in config.apply_to:
        return StagePlan(
            accepted=False,
            stage_count=0,
            stages=(),
            rejection_reason="apply_to_mismatch",
            **base_kwargs,
        )

    p0 = float(first_leg_fill_price)
    p_full = float(full_trigger_price)
    q_total = _normalize_qty(total_qty, qty_step)
    if p0 <= 0 or p_full <= 0 or q_total <= 0:
        return StagePlan(
            accepted=False,
            stage_count=0,
            stages=(),
            rejection_reason="invalid_inputs",
            **base_kwargs,
        )

    min_notional = float(config.min_stage_notional_usdt or 0.0)
    min_qty = float(min_order_qty or 0.0)
    tick = float(price_tick or 0.0)
    fee = float(fee_rate or 0.0)
    entry = float(short_entry_price or 0.0) or p0

    def try_count(n: int) -> StagePlan | None:
        price_fracs = _price_fractions_for_count(config, n)
        raw_prices = [
            price_at_fraction(
                first_leg_fill=p0,
                full_trigger=p_full,
                fraction=f,
                direction=direction,
            )
            for f in price_fracs
        ]
        if config.mode in ("same_trigger", "qty_only"):
            raw_prices = [p_full] * n
        prices = _ensure_monotone_prices(
            raw_prices, full_trigger=p_full, tick=tick, direction=direction
        )
        if prices is None:
            return None

        early_fracs = _qty_early_fractions(config, n)
        stages_qty = [0.0] * n
        if n == 1:
            stages_qty[0] = q_total
        elif config.last_stage_mode == "residual_coverage" and required_net > 0:
            # Reserve last-stage qty for remaining coverage after provisional early nets.
            provisional_early = []
            remaining_pool = q_total
            for frac in early_fracs:
                q = _normalize_qty(q_total * frac, qty_step)
                q = min(q, remaining_pool)
                provisional_early.append(q)
                remaining_pool = max(remaining_pool - q, 0.0)
            early_expected = sum(
                short_reduce_expected_net(
                    short_entry=entry, trigger=prices[i], qty=provisional_early[i], fee_rate=fee
                )
                for i in range(n - 1)
            )
            remain_net = max(float(required_net) - early_expected, 0.0)
            last_needed = qty_for_target_net(
                target_net=remain_net,
                short_entry=entry,
                trigger=prices[-1],
                fee_rate=fee,
            )
            last_needed = _normalize_qty(last_needed, qty_step)
            # Clamp: last gets at least residual of equal-ish split, at most remaining inventory.
            residual_min = _normalize_qty(remaining_pool, qty_step)
            last_qty = max(last_needed, residual_min)
            if last_qty > q_total:
                last_qty = q_total
            early_budget = max(q_total - last_qty, 0.0)
            # Redistribute early_budget by relative early fractions.
            frac_sum = sum(early_fracs) or 1.0
            used = 0.0
            for i, frac in enumerate(early_fracs):
                if i == len(early_fracs) - 1:
                    q = _normalize_qty(early_budget - used, qty_step)
                else:
                    q = _normalize_qty(early_budget * (frac / frac_sum), qty_step)
                stages_qty[i] = max(q, 0.0)
                used += stages_qty[i]
            # Fix rounding: ensure early sum <= early_budget
            early_sum = sum(stages_qty[: n - 1])
            if early_sum > early_budget + 1e-12:
                overflow = early_sum - early_budget
                for i in range(n - 2, -1, -1):
                    take = min(stages_qty[i], overflow)
                    stages_qty[i] = _normalize_qty(stages_qty[i] - take, qty_step)
                    overflow -= take
                    if overflow <= 1e-12:
                        break
            stages_qty[-1] = _normalize_qty(q_total - sum(stages_qty[: n - 1]), qty_step)
        else:
            # residual_qty / fixed: early fixed fractions of total, last residual.
            used = 0.0
            for i, frac in enumerate(early_fracs):
                q = _normalize_qty(q_total * frac, qty_step)
                stages_qty[i] = q
                used += q
            stages_qty[-1] = _normalize_qty(q_total - used, qty_step)
            if stages_qty[-1] < 0:
                return None

        # Reconcile exact total
        diff = q_total - sum(stages_qty)
        if abs(diff) > 1e-12:
            stages_qty[-1] = _normalize_qty(stages_qty[-1] + diff, qty_step)
        if abs(sum(stages_qty) - q_total) > max(qty_step, 1e-8) + 1e-9:
            return None
        if any(q <= 0 for q in stages_qty):
            return None

        specs: list[StageSpec] = []
        for i, (px, q) in enumerate(zip(prices, stages_qty)):
            if min_qty > 0 and q + 1e-12 < min_qty:
                return None
            notional = q * px
            if min_notional > 0 and notional + 1e-8 < min_notional:
                return None
            expected = short_reduce_expected_net(
                short_entry=entry, trigger=px, qty=q, fee_rate=fee
            )
            qty_frac = (q / q_total) if q_total > 0 else 0.0
            specs.append(
                StageSpec(
                    stage_index=i,
                    trigger_price=px,
                    qty=q,
                    expected_net=expected,
                    notional=notional,
                    price_fraction=float(price_fracs[i]),
                    qty_fraction=float(qty_frac),
                )
            )

        # No over-close
        if sum(s.qty for s in specs) > q_total + 1e-9:
            return None

        keys = tuple(
            stage_identity_key(cycle_index=cycle_index, purpose=purpose, stage_index=s.stage_index)
            for s in specs
        )
        return StagePlan(
            accepted=True,
            stage_count=n,
            stages=tuple(specs),
            rejection_reason=None,
            fallback_used=None if n == config.stage_count else "reduce_stage_count",
            identity_keys=keys,
            **base_kwargs,
        )

    n0 = int(config.stage_count)
    if config.insufficient_size_fallback == "reduce_stage_count":
        for n in range(n0, 0, -1):
            plan = try_count(n)
            if plan is not None:
                return plan
        return StagePlan(
            accepted=False,
            stage_count=0,
            stages=(),
            rejection_reason="min_notional_or_qty",
            fallback_used="full_qty_legacy",
            **base_kwargs,
        )

    if config.insufficient_size_fallback == "full_qty_at_full_trigger":
        plan = try_count(1)
        if plan is not None:
            return replace(plan, fallback_used="full_qty_at_full_trigger")
        return StagePlan(
            accepted=False,
            stage_count=0,
            stages=(),
            rejection_reason="full_qty_failed",
            **base_kwargs,
        )

    plan = try_count(n0)
    if plan is not None:
        return plan
    return StagePlan(
        accepted=False,
        stage_count=0,
        stages=(),
        rejection_reason="plan_rejected",
        fallback_used="disable_staging",
        **base_kwargs,
    )
