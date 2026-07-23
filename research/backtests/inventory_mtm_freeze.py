"""Backtest-only inventory MTM freeze policy math (research-only).

Pure dataclasses + helper functions used by ``inventory_mtm_freeze_shim.py``.
No live config, runtime, or strategy default is touched by this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

VARIANT_NAMES: tuple[str, ...] = ("A0", "A1", "A2", "A3", "A4", "A5", "A6")

_CYCLE_LEG_RE = re.compile(r"^CYCLE_(\d+)_([A-Z_]+)$")

INJUSDT_UNDERCOVERAGE_COIN = "INJUSDT"
INJUSDT_UNDERCOVERAGE_TRADE_NUMBER = 8


@dataclass(frozen=True)
class InventoryMtmFreezeConfig:
    """Configuration for one inventory-MTM freeze variant run.

    Defaults preserve the original A0..A6 / B0..B5 behaviour:
    primary trigger is ``inventory_mtm < threshold_usdt`` once within
    ``[0, max_trigger_candle]``.

    Extended research knobs (trigger combinations, staged secondary gates,
    emergency neutralization) are opt-in and unused by existing audits.
    """

    variant: str = "A0"
    threshold_usdt: float = -1.0
    max_trigger_candle: int = 500  # inclusive: candles 0..max_trigger_candle
    # A5: fraction of abs(net) to neutralize once (A6 always neutralizes ~fully).
    partial_neutralize_fraction: float = 0.5
    # Staged two-step freeze: stage 1 = exposure freeze (typically variant A2);
    # stage 2 = escalate to an A1-style cycle freeze once a secondary condition fires.
    staged_cycle_freeze: bool = False
    secondary_hold_candles_below_threshold: int = 100
    secondary_mtm_threshold_usdt: float = -2.0
    secondary_exit_increase_count: int = 2
    # Which secondary conditions are eligible (OR). Defaults match B5.
    secondary_use_hold: bool = True
    secondary_use_mtm: bool = True
    secondary_use_exit_increase: bool = True
    secondary_use_cycle: bool = False
    secondary_cycle_count: int | None = None
    # Primary trigger condition flags (AND/OR via ``trigger_combine``).
    use_mtm_trigger: bool = True
    use_cycle_trigger: bool = False
    use_exit_increase_trigger: bool = False
    use_required_recovery_move_trigger: bool = False
    cycle_count_threshold: int | None = None
    exit_increase_count_threshold: int | None = None
    required_recovery_move_pct_threshold: float | None = None
    trigger_combine: str = "and"  # "and" | "or"
    # Emergency: one-shot partial neutralize N candles after primary trigger if still open.
    emergency_neutralize_after_candles: int | None = None
    emergency_neutralize_fraction: float = 0.25
    # Safe Cycle Boundary Freeze (research-only; default off preserves A0..A6 / C1a).
    # When True: trigger (or stop_after_cycle arm) enters FREEZE_PENDING and only
    # activates opener blocking after canonical cycle-complete + exit commit.
    safe_cycle_boundary: bool = False
    # "mtm" → inventory_mtm trigger arms PENDING (S1).
    # "stop_after_cycle" → arm PENDING from entry; activate after N complete (S2/S3).
    safe_boundary_arm_mode: str = "mtm"
    stop_after_cycle: int | None = None
    # Research label for audits (S0/S1/S2/S3); unused by legacy path.
    safe_boundary_variant: str | None = None


@dataclass
class FreezeRuntimeState:
    """Backtest-only per-trade runtime state for one installed freeze variant."""

    variant: str = "A0"
    realized_pnl: float = 0.0
    triggered: bool = False
    trigger_candle: int | None = None
    trigger_mtm: float | None = None
    trigger_mark: float | None = None
    trigger_long_qty: float | None = None
    trigger_long_avg: float | None = None
    trigger_short_qty: float | None = None
    trigger_short_avg: float | None = None
    cycles_at_trigger: int | None = None
    active_exit_at_trigger: float | None = None
    net_exposure_at_trigger: float | None = None
    exit_increases_at_trigger: int = 0
    latched_exit_ceiling: float | None = None
    latched_exit_floor: float | None = None
    policy_actions: list[dict[str, Any]] = field(default_factory=list)
    neutralization_done: bool = False
    cycles_after_trigger: int = 0
    exit_increases_after_trigger: int = 0
    # Lifetime exit-increase counter (from trade start; used for pre-trigger gates).
    exit_increases_lifetime: int = 0
    last_observed_exit: float | None = None
    # Staged two-step freeze escalation bookkeeping.
    cycle_freeze_enabled: bool = False
    secondary_trigger_candle: int | None = None
    secondary_trigger_reason: str | None = None
    candles_below_threshold_since_trigger: int = 0
    # Rich diagnostic snapshot / post-trigger path (research exports).
    trigger_gross_notional: float | None = None
    trigger_net_exposure_usdt: float | None = None
    trigger_exit_distance_pct: float | None = None
    trigger_required_recovery_move_pct: float | None = None
    trigger_pending_cycle_loss: float | None = None
    trigger_condition_details: dict[str, Any] = field(default_factory=dict)
    worst_mtm_after_trigger: float | None = None
    max_adverse_price_move_after_trigger: float | None = None
    max_favorable_price_move_after_trigger: float | None = None
    first_reclaim_candle: int | None = None
    min_mark_after_trigger: float | None = None
    max_mark_after_trigger: float | None = None
    # Emergency neutralization bookkeeping.
    emergency_armed: bool = False
    emergency_fired: bool = False
    emergency_candle: int | None = None
    force_exposure_freeze_after_emergency: bool = False
    # Safe-boundary runtime (research); None unless safe_cycle_boundary enabled.
    safe_boundary: Any = None


def safe_float(value: object, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def inventory_mtm_usdt(
    *,
    realized: float,
    long_qty: float,
    long_avg: float,
    short_qty: float,
    short_avg: float,
    mark: float,
) -> float:
    """Mark-to-market USDT value of the whole basket including realized PnL."""
    return (
        float(realized)
        + float(long_qty) * (float(mark) - float(long_avg))
        + float(short_qty) * (float(short_avg) - float(mark))
    )


def required_recovery_move_pct(
    *,
    mark: float,
    active_exit: float | None,
    primary_side: str = "long",
) -> float | None:
    """Pct move of mark required to reach the currently active exit/flat path.

    Long-primary: ``(active_exit - mark) / mark * 100`` (positive => exit above mark).
    Short-primary: ``(mark - active_exit) / mark * 100``.
    Returns ``None`` when exit/mark is unavailable.
    """
    if active_exit is None:
        return None
    mark_f = float(mark)
    exit_f = float(active_exit)
    if mark_f <= 0 or exit_f <= 0:
        return None
    if str(primary_side or "long").strip().lower() == "long":
        return (exit_f - mark_f) / mark_f * 100.0
    return (mark_f - exit_f) / mark_f * 100.0


def exit_distance_pct(*, mark: float, active_exit: float | None) -> float | None:
    """Absolute pct distance from mark to active exit (unsigned)."""
    move = required_recovery_move_pct(mark=mark, active_exit=active_exit, primary_side="long")
    if move is None:
        return None
    return abs(float(move))


def would_increase_abs_net_exposure(
    *,
    long_qty: float,
    short_qty: float,
    side: str,
    qty: float,
    reduce_only: bool,
) -> bool:
    """Project abs(net) before vs after a fill-like action.

    ``net = long_qty - short_qty``. Returns True iff abs(net) strictly grows.
    """
    net_before = float(long_qty) - float(short_qty)
    qty_abs = abs(float(qty))
    side_norm = str(side or "").strip().lower()
    if side_norm == "long":
        delta = -min(qty_abs, float(long_qty)) if reduce_only else qty_abs
    elif side_norm == "short":
        delta = min(qty_abs, float(short_qty)) if reduce_only else -qty_abs
    else:
        delta = 0.0
    net_after = net_before + delta
    return abs(net_after) > abs(net_before) + 1e-9


def apply_exit_freeze_long(
    *,
    raw_exit: float,
    latched_ceiling: float | None,
    active_exit: float | None = None,
) -> float:
    """Long-primary: effective = min(raw, latched_ceiling) when latched.

    Never raises the exit above the latch. ``active_exit`` is accepted for
    call-site symmetry/logging but is not required by the math itself.
    """
    raw = float(raw_exit)
    if latched_ceiling is None:
        return raw
    return min(raw, float(latched_ceiling))


def apply_exit_freeze_short(
    *,
    raw_exit: float,
    latched_floor: float | None,
    active_exit: float | None = None,
) -> float:
    """Short-primary mirror of :func:`apply_exit_freeze_long`."""
    raw = float(raw_exit)
    if latched_floor is None:
        return raw
    return max(raw, float(latched_floor))


def parse_cycle_number(purpose: str) -> int | None:
    match = _CYCLE_LEG_RE.match(str(purpose or "").strip().upper())
    return int(match.group(1)) if match else None


def cycle_leg_kind(purpose: str) -> str | None:
    match = _CYCLE_LEG_RE.match(str(purpose or "").strip().upper())
    return match.group(2) if match else None


def is_new_cycle_open_purpose(purpose: str, *, primary_side: str) -> bool:
    """True for the first leg of a new cycle on the primary side.

    Long-primary opens a new cycle via ``CYCLE_N_LONG_ADD``; short-primary
    mirrors this via ``CYCLE_N_SHORT_ADD``.
    """
    leg = cycle_leg_kind(purpose)
    if leg is None:
        return False
    if str(primary_side or "long").strip().lower() == "long":
        return leg == "LONG_ADD"
    return leg == "SHORT_ADD"


def classify_trigger_case(*, baseline_is_blocker: bool, trigger_fired: bool) -> str:
    """TP/FP/FN/TN classification of one variant trade against the A0 baseline."""
    if baseline_is_blocker:
        return "TP" if trigger_fired else "FN"
    return "FP" if trigger_fired else "TN"


def is_injusdt_trade8_undercoverage(*, coin: str, trade_number: int) -> bool:
    """INJUSDT trade 8 undercoverage marker: kept separate, never a policy success."""
    return (
        str(coin or "").strip().upper() == INJUSDT_UNDERCOVERAGE_COIN
        and int(trade_number) == INJUSDT_UNDERCOVERAGE_TRADE_NUMBER
    )


def evaluate_primary_trigger(
    *,
    config: InventoryMtmFreezeConfig,
    mtm: float,
    cycle_count: int,
    exit_increase_count: int,
    required_recovery_move: float | None,
) -> tuple[bool, dict[str, Any]]:
    """Return ``(should_fire, condition_details)`` for the primary freeze trigger."""
    checks: list[tuple[str, bool]] = []
    details: dict[str, Any] = {
        "mtm": mtm,
        "cycle_count": cycle_count,
        "exit_increase_count": exit_increase_count,
        "required_recovery_move_pct": required_recovery_move,
        "combine": config.trigger_combine,
    }
    if config.use_mtm_trigger:
        ok = float(mtm) < float(config.threshold_usdt)
        checks.append(("mtm", ok))
        details["mtm_ok"] = ok
        details["mtm_threshold"] = config.threshold_usdt
    if config.use_cycle_trigger:
        thr = int(config.cycle_count_threshold or 0)
        ok = int(cycle_count) >= thr
        checks.append(("cycle", ok))
        details["cycle_ok"] = ok
        details["cycle_threshold"] = thr
    if config.use_exit_increase_trigger:
        thr = int(config.exit_increase_count_threshold or 0)
        ok = int(exit_increase_count) >= thr
        checks.append(("exit_increase", ok))
        details["exit_increase_ok"] = ok
        details["exit_increase_threshold"] = thr
    if config.use_required_recovery_move_trigger:
        thr = float(config.required_recovery_move_pct_threshold or 0.0)
        ok = required_recovery_move is not None and float(required_recovery_move) >= thr
        checks.append(("required_recovery_move", ok))
        details["required_recovery_move_ok"] = ok
        details["required_recovery_move_threshold"] = thr

    if not checks:
        return False, details

    combine = str(config.trigger_combine or "and").strip().lower()
    if combine == "or":
        fire = any(ok for _, ok in checks)
    else:
        fire = all(ok for _, ok in checks)
    details["fired"] = fire
    details["active_checks"] = [name for name, _ in checks]
    return fire, details


def freeze_state_summary(state: FreezeRuntimeState) -> dict[str, Any]:
    """JSON-friendly summary of one :class:`FreezeRuntimeState`."""
    return {
        "variant": state.variant,
        "realized_pnl": state.realized_pnl,
        "triggered": state.triggered,
        "trigger_candle": state.trigger_candle,
        "trigger_mtm": state.trigger_mtm,
        "trigger_mark": state.trigger_mark,
        "trigger_long_qty": state.trigger_long_qty,
        "trigger_long_avg": state.trigger_long_avg,
        "trigger_short_qty": state.trigger_short_qty,
        "trigger_short_avg": state.trigger_short_avg,
        "cycles_at_trigger": state.cycles_at_trigger,
        "active_exit_at_trigger": state.active_exit_at_trigger,
        "net_exposure_at_trigger": state.net_exposure_at_trigger,
        "exit_increases_at_trigger": state.exit_increases_at_trigger,
        "latched_exit_ceiling": state.latched_exit_ceiling,
        "latched_exit_floor": state.latched_exit_floor,
        "neutralization_done": state.neutralization_done,
        "cycles_after_trigger": state.cycles_after_trigger,
        "exit_increases_after_trigger": state.exit_increases_after_trigger,
        "exit_increases_lifetime": state.exit_increases_lifetime,
        "policy_action_count": len(state.policy_actions),
        "cycle_freeze_enabled": state.cycle_freeze_enabled,
        "secondary_trigger_candle": state.secondary_trigger_candle,
        "secondary_trigger_reason": state.secondary_trigger_reason,
        "candles_below_threshold_since_trigger": state.candles_below_threshold_since_trigger,
        "trigger_gross_notional": state.trigger_gross_notional,
        "trigger_net_exposure_usdt": state.trigger_net_exposure_usdt,
        "trigger_exit_distance_pct": state.trigger_exit_distance_pct,
        "trigger_required_recovery_move_pct": state.trigger_required_recovery_move_pct,
        "trigger_pending_cycle_loss": state.trigger_pending_cycle_loss,
        "trigger_condition_details": dict(state.trigger_condition_details or {}),
        "worst_mtm_after_trigger": state.worst_mtm_after_trigger,
        "max_adverse_price_move_after_trigger": state.max_adverse_price_move_after_trigger,
        "max_favorable_price_move_after_trigger": state.max_favorable_price_move_after_trigger,
        "first_reclaim_candle": state.first_reclaim_candle,
        "emergency_fired": state.emergency_fired,
        "emergency_candle": state.emergency_candle,
        "force_exposure_freeze_after_emergency": state.force_exposure_freeze_after_emergency,
        "safe_boundary": (
            {
                "freeze_state": getattr(state.safe_boundary, "freeze_state", None),
                "freeze_requested_at_candle": getattr(
                    state.safe_boundary, "freeze_requested_at_candle", None
                ),
                "freeze_requested_cycle": getattr(
                    state.safe_boundary, "freeze_requested_cycle", None
                ),
                "freeze_activated_at_candle": getattr(
                    state.safe_boundary, "freeze_activated_at_candle", None
                ),
                "freeze_activated_after_cycle": getattr(
                    state.safe_boundary, "freeze_activated_after_cycle", None
                ),
                "blocked_opener_count": getattr(
                    state.safe_boundary, "blocked_opener_count", 0
                ),
                "blocked_opener_purposes": list(
                    getattr(state.safe_boundary, "blocked_opener_purposes", []) or []
                ),
                "allowed_current_cycle_action_count": getattr(
                    state.safe_boundary, "allowed_current_cycle_action_count", 0
                ),
                "safe_boundary_reason": getattr(
                    state.safe_boundary, "safe_boundary_reason", None
                ),
                "exit_signature_at_activation": getattr(
                    state.safe_boundary, "exit_signature_at_activation", None
                ),
                "event_count": len(getattr(state.safe_boundary, "events", []) or []),
            }
            if state.safe_boundary is not None
            else None
        ),
    }
