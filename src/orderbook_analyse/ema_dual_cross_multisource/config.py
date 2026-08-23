"""Configurable research thresholds — defaults documented, not outcome-tuned."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

POLICY_VERSION = "EMA_MULTI_SOURCE_GATE_V1"
STRATEGY_ID = "ema_dual_cross_multisource_v1"
STRATEGY_VERSION = "ema_dual_cross_multisource_v1"


@dataclass(frozen=True)
class EmaDualCrossConfig:
    """Research defaults for XRPUSDT 15m validation."""

    ema_fast: int = 9
    ema_medium: int = 20
    ema_slow: int = 59
    band_compression_pct: float = 0.15  # max |ema9-ema20|/close % before cross
    band_compression_atr: float = 0.35  # max |ema9-ema20|/ATR before cross
    max_band_lookback: int = 5
    max_total_band_atr: float = 0.55  # 9/20/59 span / ATR for compression
    flat_slope_atr: float = 0.02  # per-bar EMA slope / ATR below → flat
    rebound_body_atr_min: float = 0.45
    rebound_range_atr_min: float = 0.55
    rebound_ema_dist_atr_max: float = 0.40
    atr_period: int = 14
    # Candidate type switches — sync default; rebound optional via cfg/UI
    enable_sync_cross: bool = True
    enable_compressed_rebound: bool = False
    # Coverage policy — full multi-source requires OB + OI + liquidations
    require_ob_for_allow: bool = True
    require_trades_for_allow: bool = True
    require_candles: bool = True
    require_oi_for_allow: bool = True
    require_liq_for_allow: bool = True
    ob_stale_minutes: int = 30
    # Episode — structural reset preferred; time fallback for active ALLOW only
    episode_reset_bars: int = 48


EMA_DUAL_CROSS_DEFAULTS = EmaDualCrossConfig()


def config_to_dict(cfg: EmaDualCrossConfig | None = None) -> dict[str, Any]:
    c = cfg or EMA_DUAL_CROSS_DEFAULTS
    return {
        "policy_version": POLICY_VERSION,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "ema_fast": c.ema_fast,
        "ema_medium": c.ema_medium,
        "ema_slow": c.ema_slow,
        "band_compression_pct": c.band_compression_pct,
        "band_compression_atr": c.band_compression_atr,
        "max_band_lookback": c.max_band_lookback,
        "max_total_band_atr": c.max_total_band_atr,
        "flat_slope_atr": c.flat_slope_atr,
        "rebound_body_atr_min": c.rebound_body_atr_min,
        "rebound_range_atr_min": c.rebound_range_atr_min,
        "rebound_ema_dist_atr_max": c.rebound_ema_dist_atr_max,
        "atr_period": c.atr_period,
        "enable_sync_cross": c.enable_sync_cross,
        "enable_compressed_rebound": c.enable_compressed_rebound,
        "require_ob_for_allow": c.require_ob_for_allow,
        "require_trades_for_allow": c.require_trades_for_allow,
        "require_candles": c.require_candles,
        "require_oi_for_allow": c.require_oi_for_allow,
        "require_liq_for_allow": c.require_liq_for_allow,
        "ob_stale_minutes": c.ob_stale_minutes,
        "episode_reset_bars": c.episode_reset_bars,
        "profitability_claim": False,
    }
