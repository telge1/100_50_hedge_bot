"""Typed configuration for the isolated Emergency-Lock backtester."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any


@dataclass
class EmergencyLockRecoveryConfig:
    """Single config source for Emergency-Lock research runs.

    CLI overrides must go through :func:`apply_cli_overrides` so there is
    only one authoritative dataclass instance per run.
    """

    symbol: str = "APTUSDT"
    timeframe: str = "5m"

    initial_long_notional_usdt: float = 100.0
    initial_short_notional_usdt: float = 50.0

    emergency_trigger_pct: float = 0.10
    trigger_price_source: str = "low"

    fee_rate: float = 0.00055
    slippage_bps: float = 2.0

    funding_enabled: bool = False
    funding_rate_per_interval: float = 0.0
    funding_interval_hours: int = 8

    start_timestamp: str | None = None
    start_index: int | None = None
    max_candles: int | None = None

    intrabar_mode: str = "conservative"

    # Phase A: if the start candle already touches the trigger, lock same bar.
    # Set to "reject" to abort instead.
    start_below_trigger_policy: str = "lock_immediately"

    qty_tolerance: float = 1e-12
    pnl_tolerance: float = 1e-9

    output_dir: str = "research/backtests/results/emergency_lock/phase_a"

    # --- Phase B: unlock / re-lock / basket exit ---
    unlock_signal_type: str = "rebound_from_post_lock_low"

    unlock_rebound_pcts: tuple[float, ...] = (0.03, 0.05, 0.075, 0.10)
    unlock_steps: tuple[float, ...] = (0.10, 0.10, 0.15, 0.15)

    unlock_confirmation_bars: int = 1
    unlock_execution_delay_bars: int = 0

    relock_mode: str = "pct_below_unlock_fill"
    relock_distance_pct: float = 0.02
    relock_confirmation_bars: int = 1
    relock_execution_delay_bars: int = 0

    relock_last_removed_tranche_only: bool = True
    max_failed_unlocks: int = 2
    cooldown_bars_after_relock: int = 12

    minimum_short_profit_buffer_usdt: float = 0.0
    minimum_distance_to_short_avg_pct: float = 0.0
    maximum_net_long_fraction: float = 0.50

    basket_exit_target_usdt: float = 0.0
    basket_exit_buffer_usdt: float = 0.05

    max_post_lock_bars: int = 5000
    max_added_loss_after_lock_usdt: float | None = None

    # When False, Phase B/C full-lock control: no unlock / re-lock actions.
    enable_unlock: bool = True

    # --- Phase C: crash event selection (hindsight) ---
    event_drop_thresholds: tuple[float, ...] = (0.10, 0.125, 0.15)
    event_peak_lookback_bars: int = 288
    event_max_drop_bars: int = 576
    event_pre_peak_bars: int = 12
    event_post_low_bars: int = 2880
    event_cooldown_bars: int = 576
    event_peak_source: str = "high"
    event_drop_source: str = "low"
    event_entry_mode: str = "peak_close"
    event_entry_offset_bars: int = 0
    event_dedupe_mode: str = "shared_drawdown_leg"
    event_min_separation_bars: int = 288


def apply_cli_overrides(
    cfg: EmergencyLockRecoveryConfig,
    **overrides: Any,
) -> EmergencyLockRecoveryConfig:
    """Return a copy of ``cfg`` with only known, non-None overrides applied."""
    valid = {f.name for f in fields(EmergencyLockRecoveryConfig)}
    cleaned: dict[str, Any] = {}
    for key, value in overrides.items():
        if key not in valid:
            raise ValueError(f"unknown EmergencyLockRecoveryConfig field: {key}")
        if value is not None:
            cleaned[key] = value
    return replace(cfg, **cleaned) if cleaned else cfg


def validate_phase_b_config(cfg: EmergencyLockRecoveryConfig) -> None:
    """Validate unlock / exposure constraints for Phase B."""
    pcts = tuple(float(x) for x in cfg.unlock_rebound_pcts)
    steps = tuple(float(x) for x in cfg.unlock_steps)
    if len(pcts) != len(steps):
        raise ValueError("unlock_rebound_pcts and unlock_steps must have equal length")
    if not pcts:
        raise ValueError("unlock_rebound_pcts must be non-empty")
    if any(p <= 0.0 for p in pcts):
        raise ValueError("unlock_rebound_pcts must be strictly positive")
    if any(pcts[i] >= pcts[i + 1] for i in range(len(pcts) - 1)):
        raise ValueError("unlock_rebound_pcts must be strictly increasing")
    if any(s <= 0.0 for s in steps):
        raise ValueError("unlock_steps must be strictly positive")
    if sum(steps) - float(cfg.maximum_net_long_fraction) > 1e-12:
        raise ValueError(
            "sum(unlock_steps) must not exceed maximum_net_long_fraction "
            f"({sum(steps)} > {cfg.maximum_net_long_fraction})"
        )
    if cfg.unlock_signal_type != "rebound_from_post_lock_low":
        raise ValueError(f"unsupported unlock_signal_type: {cfg.unlock_signal_type}")
    if cfg.relock_mode != "pct_below_unlock_fill":
        raise ValueError(f"unsupported relock_mode: {cfg.relock_mode}")
    if cfg.intrabar_mode != "conservative":
        raise ValueError(f"unsupported intrabar_mode: {cfg.intrabar_mode}")
    if int(cfg.unlock_confirmation_bars) < 1:
        raise ValueError("unlock_confirmation_bars must be >= 1")
    if int(cfg.relock_confirmation_bars) < 1:
        raise ValueError("relock_confirmation_bars must be >= 1")
    if float(cfg.maximum_net_long_fraction) <= 0.0 or float(cfg.maximum_net_long_fraction) > 1.0:
        raise ValueError("maximum_net_long_fraction must be in (0, 1]")


def validate_phase_c_config(cfg: EmergencyLockRecoveryConfig) -> None:
    """Validate Phase C event-finder fields (also runs Phase B validation)."""
    validate_phase_b_config(cfg)
    thresholds = tuple(float(x) for x in cfg.event_drop_thresholds)
    if not thresholds:
        raise ValueError("event_drop_thresholds must be non-empty")
    if any(t <= 0.0 or t >= 1.0 for t in thresholds):
        raise ValueError("event_drop_thresholds must be in (0, 1)")
    if any(thresholds[i] >= thresholds[i + 1] for i in range(len(thresholds) - 1)):
        raise ValueError("event_drop_thresholds must be strictly increasing")
    for name in (
        "event_peak_lookback_bars",
        "event_max_drop_bars",
        "event_pre_peak_bars",
        "event_post_low_bars",
        "event_cooldown_bars",
        "event_min_separation_bars",
    ):
        if int(getattr(cfg, name)) < 0:
            raise ValueError(f"{name} must be >= 0")
    if int(cfg.event_max_drop_bars) < 1:
        raise ValueError("event_max_drop_bars must be >= 1")
    if cfg.event_peak_source != "high":
        raise ValueError(f"unsupported event_peak_source: {cfg.event_peak_source}")
    if cfg.event_drop_source != "low":
        raise ValueError(f"unsupported event_drop_source: {cfg.event_drop_source}")
    if cfg.event_entry_mode != "peak_close":
        raise ValueError(f"unsupported event_entry_mode: {cfg.event_entry_mode}")
    if cfg.event_dedupe_mode != "shared_drawdown_leg":
        raise ValueError(f"unsupported event_dedupe_mode: {cfg.event_dedupe_mode}")
    if int(cfg.event_entry_offset_bars) < 0:
        raise ValueError("event_entry_offset_bars must be >= 0")
