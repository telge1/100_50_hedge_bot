from __future__ import annotations

import math
import re
from typing import Any

from .config import RecoveryBotConfig


_SHORT_REDUCE_RE = re.compile(r"^CYCLE_(\d+)_SHORT_REDUCE$", re.IGNORECASE)


def is_cycle_short_reduce_purpose(purpose: Any) -> bool:
    """Return True when purpose matches CYCLE_<N>_SHORT_REDUCE."""
    text = str(purpose or "").strip()
    return bool(_SHORT_REDUCE_RE.match(text))


def extract_cycle_index(purpose: Any) -> int:
    """Extract the cycle index N from CYCLE_<N>_SHORT_REDUCE or return 0."""
    text = str(purpose or "").strip()
    match = _SHORT_REDUCE_RE.match(text)
    if not match:
        return 0
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return 0


def matches_configured_trigger(fill_purpose: Any, configured_trigger: str) -> bool:
    """Return True when ``fill_purpose`` equals the configured trigger purpose.

    This is intentionally strict: only an actual filled purpose can trigger
    recovery, never just an active order.
    """
    fill_text = str(fill_purpose or "").strip().upper()
    trigger_text = str(configured_trigger or "").strip().upper()
    return fill_text == trigger_text and is_cycle_short_reduce_purpose(fill_text)


def compute_price_drop_pct(current_price: float, reference_price: float) -> float:
    """Return percentage drop from reference to current (>=0)."""
    if reference_price <= 0:
        return 0.0
    if current_price >= reference_price:
        return 0.0
    drop = (reference_price - current_price) / reference_price * 100.0
    return max(0.0, float(drop))


def compute_signed_price_move_pct(current_price: float, reference_price: float) -> float:
    """Return signed move (current-reference)/reference * 100.

    Positive for up-moves, negative for down-moves. Returns 0.0 when
    reference_price <= 0.
    """
    if reference_price <= 0:
        return 0.0
    move = (current_price - reference_price) / reference_price * 100.0
    return float(move)


def compute_loss_budget_usdt(config: RecoveryBotConfig) -> float:
    """Compute the absolute loss budget in USDT.

    Modes:
    - fixed: use ``fixed_loss_budget_usdt`` (or 0).
    - profit_share: pct of ``available_profit_pool_usdt``.
    - hybrid: profit-share, then clamped by min/max.
    """
    mode = str(config.loss_budget_mode or "fixed").strip()
    base = 0.0

    if mode == "fixed":
        base = float(config.fixed_loss_budget_usdt or 0.0)
    elif mode in {"profit_share", "hybrid"}:
        base = float(config.available_profit_pool_usdt or 0.0) * float(
            config.loss_budget_profit_share_pct or 0.0
        ) / 100.0
    else:
        # Validation should prevent this, but keep a safe fallback.
        base = float(config.fixed_loss_budget_usdt or 0.0)

    minimum = float(config.minimum_loss_budget_usdt or 0.0)
    maximum_raw = config.maximum_loss_budget_usdt
    maximum = float(maximum_raw) if maximum_raw is not None else None

    value = max(base, minimum)
    if maximum is not None and maximum > 0.0:
        value = min(value, maximum)
    return max(0.0, float(value))


def compute_net_long_qty(long_qty: float, short_qty: float) -> float:
    """Return net long exposure as long_qty - short_qty."""
    return float(long_qty) - float(short_qty)


def compute_neutralization_fixed_step_qty(net_long_qty: float, target_steps: int) -> float:
    """Compute the per-step neutralization quantity for fixed_steps mode."""
    if target_steps <= 0 or net_long_qty <= 0:
        return 0.0
    return float(net_long_qty) / float(target_steps)


def compute_neutralization_step_qty(
    current_long_qty: float,
    current_short_qty: float,
    fixed_step_qty: float,
) -> float:
    """Return the neutralization step qty for the current snapshot.

    The last step is clamped to the remaining net-long surplus so that
    long and short can become exactly neutral.
    """
    net = compute_net_long_qty(current_long_qty, current_short_qty)
    if net <= 0 or fixed_step_qty <= 0:
        return 0.0
    return float(min(float(fixed_step_qty), float(net)))


def is_exactly_neutral(
    long_qty: float,
    short_qty: float,
    *,
    tolerance_qty: float = 0.0,
) -> bool:
    """Return True when long and short are equal within a qty tolerance."""
    diff = abs(float(long_qty) - float(short_qty))
    if tolerance_qty <= 0:
        return diff == 0.0
    return diff <= float(tolerance_qty)


def compute_pair_reduce_step_qty(
    current_long_qty: float,
    current_short_qty: float,
    *,
    minimum_pair_qty: float,
    mode: str,
    fixed_qty: float | None,
    pct: float | None,
) -> float:
    """Return the per-leg reduction quantity for a neutral pair.

    The same quantity is intended to be applied to both long and short.
    The function ensures that:
    - the minimum pair quantity is not undershot,
    - no more than the current pair qty is closed,
    - the result is never negative.
    """
    pair_qty = min(float(current_long_qty), float(current_short_qty))
    min_pair = float(minimum_pair_qty)
    if pair_qty <= 0 or pair_qty <= min_pair:
        return 0.0

    mode_norm = str(mode or "fixed_qty").strip()
    raw_step = 0.0
    if mode_norm == "fixed_qty":
        if fixed_qty is None or fixed_qty <= 0:
            return 0.0
        raw_step = float(fixed_qty)
    elif mode_norm == "percent":
        if pct is None or pct <= 0:
            return 0.0
        raw_step = pair_qty * float(pct) / 100.0
    else:
        return 0.0

    # Do not reduce below the configured minimum pair qty.
    max_allowed = max(0.0, pair_qty - min_pair)
    step = min(raw_step, max_allowed, pair_qty)
    return max(0.0, float(step))


def would_exceed_loss_budget(
    loss_budget_usdt: float | None,
    loss_budget_used_usdt: float,
    projected_additional_loss_usdt: float,
) -> bool:
    """Return True if adding the projected loss would exceed the budget.

    ``loss_budget_usdt`` is the absolute budget, ``loss_budget_used_usdt`` the
    amount already consumed by earlier recovery actions. The projected loss is
    interpreted as a positive number of additional loss (USDT).
    """
    if loss_budget_usdt is None or loss_budget_usdt <= 0:
        return True if projected_additional_loss_usdt > 0 else False
    used = max(0.0, float(loss_budget_used_usdt))
    additional = max(0.0, float(projected_additional_loss_usdt))
    return used + additional > float(loss_budget_usdt)

