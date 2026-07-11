"""Immutable configuration for the backtest-only regime scanner."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence


@dataclass(frozen=True)
class RegimeScannerConfig:
    """Indicator and structure defaults (no live trading / entry execution)."""

    ema_periods: tuple[int, ...] = (9, 20, 59, 200)
    adx_period: int = 14
    atr_period: int = 14
    slope_windows: tuple[int, ...] = (3, 6, 12, 48, 144)
    pivot_left: int = 3
    pivot_right: int = 3
    # Per-timeframe confirmed pivot windows (left / right bars).
    pivot_left_by_timeframe: dict[str, int] | None = None
    pivot_right_by_timeframe: dict[str, int] | None = None
    epsilon: float = 1e-12
    candle_interval_minutes: int = 5

    # Descriptive slope / band deadbands (percentage points).
    slope_change_epsilon: float = 0.05
    band_change_epsilon: float = 0.02
    band_orientation_epsilon: float = 1e-9
    band_windows: tuple[int, ...] = (3, 6, 12, 48)
    band_pairs: tuple[tuple[int, int], ...] = (
        (9, 20),
        (20, 59),
        (59, 200),
        (9, 200),
    )

    # ATR% vs recent mean ratios (descriptive only).
    atr_pct_mean_windows: tuple[int, ...] = (12, 48, 144)
    atr_pct_above_ratio: float = 1.10
    atr_pct_below_ratio: float = 0.90

    # Confirmed divergence filters.
    divergence_min_swing_separation: int = 5
    divergence_price_epsilon: float = 1e-9
    divergence_indicator_epsilon: float = 0.25
    divergence_max_age_candles: int | None = 720
    divergence_max_swing_gap: int = 288
    divergence_indicators: tuple[str, ...] = ("adx", "plus_di", "minus_di", "di_spread")

    # Momentum weakening lookbacks (descriptive; never labeled as divergence).
    weakening_lookbacks: tuple[int, ...] = (3, 6, 12)
    history_offsets: tuple[int, ...] = (0, 3, 6, 12, 24, 48, 72, 144)

    # Phase 5b: last-bar deltas / rollover (descriptive only).
    last_bar_change_epsilon: float = 1e-6
    last_bar_prior_rise_lookback: int = 1  # compare t-2 -> t-1 for prior rise
    recent_swing_pairs: int = 5
    last_closed_table_candles: int = 12
    atr_divergence_indicator_epsilon: float = 1e-9

    # Equal-high / retest exhaustion (descriptive; never classic HH divergence).
    # Diagnostic tolerances checked in parallel; primary bands pick structure type.
    retest_tolerances_pct: tuple[float, ...] = (0.10, 0.25, 0.50, 0.75)
    equal_high_tolerance_pct: float = 0.50
    lower_high_retest_tolerance_pct: float = 0.75
    # Lookback for selecting the medium/major reference pivot high (bars).
    exhaustion_reference_lookback_candles: int = 96
    # Heuristic minimum relative weakening so float noise is not a signal.
    exhaustion_adx_min_weakening_pct: float = 10.0
    exhaustion_atr_min_weakening_pct: float = 10.0
    exhaustion_atr_pct_min_weakening_pct: float = 10.0
    exhaustion_plus_di_min_weakening_pct: float = 5.0
    exhaustion_di_spread_min_weakening_pct: float = 5.0
    exhaustion_indicator_windows: tuple[int, ...] = (0, 1, 2)  # exact, ±1, ±2
    # Mark vs last-price deviation is documented only; never simulated in v1.
    mark_price_deviation_note: str = (
        "Mark-price vs last-price deviation is documented only and not simulated."
    )

    # --- Classifier v1 heuristics (documented, not trading truth) ---
    adx_very_weak: float = 15.0
    adx_weak: float = 20.0
    adx_moderate: float = 30.0
    adx_strong: float = 45.0
    di_spread_strong: float = 20.0
    di_spread_moderate: float = 10.0
    di_spread_near_zero: float = 5.0

    # Close vs EMA distance in ATR units (long overextension heuristics).
    oe_ema20_low: float = 1.0
    oe_ema20_elevated: float = 2.0
    oe_ema20_high: float = 3.0
    oe_ema59_low: float = 2.0
    oe_ema59_elevated: float = 4.0
    oe_ema59_high: float = 6.0
    oe_ema200_normal: float = 4.0
    oe_ema200_elevated: float = 7.0
    oe_ema200_high: float = 10.0

    # Explicit score component weights (must sum to the documented max).
    # trend_direction_score range: [-100, +100]
    w_dir_ema_alignment: float = 30.0
    w_dir_close_vs_ema: float = 25.0
    w_dir_di_spread: float = 25.0
    w_dir_slopes: float = 15.0
    w_dir_structure: float = 5.0

    # trend_strength_score range: [0, 100]
    w_str_adx: float = 30.0
    w_str_di_spread: float = 25.0
    w_str_ema_alignment: float = 20.0
    w_str_band_expansion: float = 15.0
    w_str_slope_consistency: float = 10.0

    # trend_acceleration_score range: [-100, +100]
    w_acc_medium_slopes: float = 35.0
    w_acc_long_slopes: float = 25.0
    w_acc_bands: float = 20.0
    w_acc_adx_di: float = 15.0
    w_acc_short_slopes: float = 5.0  # small weight; cannot alone flip regime to weakening

    # overextension scores range: [0, 100]
    w_oe_ema20: float = 30.0
    w_oe_ema59: float = 25.0
    w_oe_ema200: float = 25.0
    w_oe_ema9: float = 10.0
    w_oe_atr_ratio: float = 10.0

    # reversal risk scores range: [0, 100]
    w_rev_divergence: float = 35.0
    w_rev_weakening: float = 25.0
    w_rev_overextension: float = 20.0
    w_rev_adx_di_fall: float = 10.0
    w_rev_band_contract: float = 10.0

    # data_quality_score range: [0, 100]
    w_dq_warmup: float = 40.0
    w_dq_features: float = 30.0
    w_dq_swings: float = 20.0
    w_dq_consistency: float = 10.0

    # Regime / risk thresholds (heuristic).
    regime_strong_dir: float = 55.0
    regime_trend_dir: float = 25.0
    regime_strong_strength: float = 65.0
    regime_trend_strength: float = 40.0
    regime_weakening_acc: float = -15.0
    regime_accel_min: float = 10.0
    risk_moderate: float = 35.0
    risk_high: float = 55.0
    risk_extreme: float = 75.0
    min_data_quality_for_regime: float = 40.0

    @property
    def min_warmup_candles(self) -> int:
        """Minimum closed candles needed before features are considered ready."""
        max_ema = max(self.ema_periods) if self.ema_periods else 0
        max_slope = max(self.slope_windows) if self.slope_windows else 0
        return int(max_ema + max_slope + self.pivot_right)

    def pivot_left_for(self, timeframe: str) -> int:
        mapping = self.pivot_left_by_timeframe or DEFAULT_PIVOT_LEFT_BY_TIMEFRAME
        return int(mapping.get(str(timeframe).strip().lower(), self.pivot_left))

    def pivot_right_for(self, timeframe: str) -> int:
        mapping = self.pivot_right_by_timeframe or DEFAULT_PIVOT_RIGHT_BY_TIMEFRAME
        return int(mapping.get(str(timeframe).strip().lower(), self.pivot_right))

    def with_timeframe(self, timeframe: str) -> RegimeScannerConfig:
        """Return a copy with pivot windows and candle interval set for ``timeframe``."""
        from .timeframes import TIMEFRAME_MINUTES

        key = str(timeframe).strip().lower()
        if key not in TIMEFRAME_MINUTES:
            raise ValueError(f"unsupported timeframe: {timeframe!r}")
        return replace(
            self,
            pivot_left=self.pivot_left_for(key),
            pivot_right=self.pivot_right_for(key),
            pivot_left_by_timeframe=dict(
                self.pivot_left_by_timeframe or DEFAULT_PIVOT_LEFT_BY_TIMEFRAME
            ),
            pivot_right_by_timeframe=dict(
                self.pivot_right_by_timeframe or DEFAULT_PIVOT_RIGHT_BY_TIMEFRAME
            ),
            candle_interval_minutes=int(TIMEFRAME_MINUTES[key]),
        )


DEFAULT_PIVOT_LEFT_BY_TIMEFRAME: dict[str, int] = {
    "5m": 3,
    "15m": 2,
    "30m": 2,
}
DEFAULT_PIVOT_RIGHT_BY_TIMEFRAME: dict[str, int] = {
    "5m": 3,
    "15m": 2,
    "30m": 2,
}


def default_regime_scanner_config() -> RegimeScannerConfig:
    return RegimeScannerConfig(
        pivot_left_by_timeframe=dict(DEFAULT_PIVOT_LEFT_BY_TIMEFRAME),
        pivot_right_by_timeframe=dict(DEFAULT_PIVOT_RIGHT_BY_TIMEFRAME),
    )


def ensure_positive_periods(periods: Sequence[int], *, name: str) -> tuple[int, ...]:
    values = tuple(int(p) for p in periods)
    if not values or any(p <= 0 for p in values):
        raise ValueError(f"{name} must contain positive integers, got {periods!r}")
    return values
