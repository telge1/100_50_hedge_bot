from __future__ import annotations

from .config import (
    DIRTY_BREAKOUT_SPREAD_THRESHOLD,
    LARGE_TRADE_Z,
    LIQ_CLUSTER_Z,
    MICROBURST_RISK_Z,
    OI_BUILD_Z,
    OI_FLUSH_Z,
    ORDERFLOW_PUSH_Z,
    PARTICIPATION_WEAK_THRESHOLD,
    PANIC_LIQ_THRESHOLD,
    PRICE_FLIP_CONFIRM_Z,
    PRICE_IMPULSE_Z,
    PRICE_PREV_IMPULSE_Z,
    SPREAD_EXPANSION_Z,
    SPREAD_STRESS_THRESHOLD,
    TRADE_SURGE_Z,
    VOLUME_EXTREME_Z,
    VOLUME_HIGH_Z,
    VOLATILITY_EXPANSION_THRESHOLD,
    VOLATILITY_EXPANSION_Z,
)
from .models import CoinProfile, NormalizedSnapshot, PrimitiveEvents


def compute_primitive_events(
    current: NormalizedSnapshot,
    previous: NormalizedSnapshot | None,
    profile: CoinProfile,
) -> PrimitiveEvents:
    prev_price_change_1m_z = previous.z("price_change_1m") if previous is not None else 0.0
    prev_oi_change_ratio_z = previous.z("oi_change_ratio") if previous is not None else 0.0
    prev_orderflow_ratio_z = previous.z("orderflow_ratio") if previous is not None else 0.0
    prev_buy_sell_imbalance = previous.value("buy_sell_imbalance") if previous is not None else 0.0
    prev_velocity_1m = previous.value("velocity_1m") if previous is not None else 0.0
    oi_price_state = current.label("oi_price_state", "neutral")

    oi_price_build_long = oi_price_state == "price_up_oi_up"
    oi_price_short_covering = oi_price_state == "price_up_oi_down"
    oi_price_build_short = oi_price_state == "price_down_oi_up"
    oi_price_long_flush = oi_price_state == "price_down_oi_down"

    price_impulse_up = current.z("price_change_1m") > PRICE_IMPULSE_Z or current.z("price_change_5m") > PRICE_IMPULSE_Z
    price_impulse_down = current.z("price_change_1m") < -PRICE_IMPULSE_Z or current.z("price_change_5m") < -PRICE_IMPULSE_Z
    price_extreme_up = current.flag("price_change_1m_gte_p99") or current.flag("price_change_5m_gte_p99")
    price_extreme_down = current.flag("price_change_1m_lte_n99") or current.flag("price_change_5m_lte_n99")

    price_flip_long = (
        prev_price_change_1m_z < -PRICE_PREV_IMPULSE_Z
        and current.z("price_change_1m") > PRICE_FLIP_CONFIRM_Z
        and current.value("acceleration_1m") > 0
    )
    price_flip_short = (
        prev_price_change_1m_z > PRICE_PREV_IMPULSE_Z
        and current.z("price_change_1m") < -PRICE_FLIP_CONFIRM_Z
        and current.value("acceleration_1m") < 0
    )

    oi_build_up = current.z("oi_change_ratio") > OI_BUILD_Z
    oi_extreme_build = current.flag("oi_change_ratio_gte_p99")
    oi_flush = current.z("oi_change_ratio") < OI_FLUSH_Z
    oi_flip_down = (
        prev_oi_change_ratio_z > 0.75
        and current.z("oi_change_ratio") < -0.5
        and current.value("oi_slope_short") < 0
    )
    oi_flip_up = (
        prev_oi_change_ratio_z < -0.75
        and current.z("oi_change_ratio") > 0.5
        and current.value("oi_slope_short") > 0
    )

    volume_participation_high = (
        current.z("trade_volume_1m") > VOLUME_HIGH_Z or current.z("volume_spike_ratio") > VOLUME_HIGH_Z
    )
    volume_participation_extreme = (
        current.flag("trade_volume_1m_gte_p99") or current.flag("volume_spike_ratio_gte_p99")
    )
    trade_participation_surge = current.z("trade_count_1m") > TRADE_SURGE_Z
    large_trade_presence = current.z("avg_trade_size") > LARGE_TRADE_Z

    orderflow_push_long = (
        current.z("orderflow_ratio") > ORDERFLOW_PUSH_Z
        or current.value("buy_sell_imbalance") > profile.threshold_buy_sell_imbalance_long
    )
    orderflow_push_short = (
        current.z("orderflow_ratio") < -ORDERFLOW_PUSH_Z
        or current.value("buy_sell_imbalance") < profile.threshold_buy_sell_imbalance_short
    )
    orderflow_flip_long = (
        prev_buy_sell_imbalance < -0.10
        and current.value("buy_sell_imbalance") > profile.threshold_buy_sell_imbalance_long
        and current.z("orderflow_ratio") > 0.5
    )
    orderflow_flip_short = (
        prev_buy_sell_imbalance > 0.10
        and current.value("buy_sell_imbalance") < profile.threshold_buy_sell_imbalance_short
        and current.z("orderflow_ratio") < -0.5
    )

    rebound_participation_surge_long = (
        current.z("trade_count_1m") > VOLUME_EXTREME_Z
        and current.z("volume_spike_ratio") > VOLUME_EXTREME_Z
        and current.value("buy_sell_imbalance") > profile.threshold_buy_sell_imbalance_long
        and price_flip_long
    )
    rebound_participation_surge_short = (
        current.z("trade_count_1m") > VOLUME_EXTREME_Z
        and current.z("volume_spike_ratio") > VOLUME_EXTREME_Z
        and current.value("buy_sell_imbalance") < profile.threshold_buy_sell_imbalance_short
        and price_flip_short
    )

    microburst_risk = current.z("microburst_score") > MICROBURST_RISK_Z
    microburst_extreme = current.flag("microburst_score_gte_p99")
    liq_cluster_event = (
        current.z("liquidation_cluster_score") > LIQ_CLUSTER_Z
        or current.z("liquidation_density_5m") > LIQ_CLUSTER_Z
    )
    liq_flush_down = price_impulse_down and liq_cluster_event and orderflow_push_short
    liq_flush_up = price_impulse_up and liq_cluster_event and orderflow_push_long
    spread_expansion = current.z("spread_ratio") > SPREAD_EXPANSION_Z
    spread_explosion = current.flag("spread_ratio_gte_p99")
    liquidity_vacuum = spread_expansion and abs(current.z("price_change_1m")) > PRICE_IMPULSE_Z and current.z("trade_count_1m") < 0.0

    velocity_slowdown_long = (
        current.value("velocity_1m") > 0
        and prev_velocity_1m > 0
        and current.value("velocity_1m") < prev_velocity_1m
        and current.value("acceleration_1m") < 0
    )
    velocity_slowdown_short = (
        current.value("velocity_1m") < 0
        and prev_velocity_1m < 0
        and current.value("velocity_1m") > prev_velocity_1m
        and current.value("acceleration_1m") > 0
    )

    pressure_divergence_long = (
        current.raw.price_change_1m > 0
        and (current.value("oi_slope_short") < 0 or current.z("orderflow_ratio") < prev_orderflow_ratio_z)
    )
    pressure_divergence_short = (
        current.raw.price_change_1m < 0
        and (current.value("oi_slope_short") > 0 or current.z("orderflow_ratio") > prev_orderflow_ratio_z)
    )
    exhaustion_long = velocity_slowdown_long and (oi_flush or pressure_divergence_long or microburst_risk or liq_cluster_event)
    exhaustion_short = velocity_slowdown_short and (oi_flush or pressure_divergence_short or microburst_risk or liq_cluster_event)

    # Derived feature based events (Phase 3)
    price_move_vs_atr = current.value("price_move_vs_atr", default=None)
    spread_vs_atr = current.value("spread_vs_atr", default=None)
    atr_regime_zscore = current.value("atr_regime_zscore", default=None)
    spread_ratio_zscore = current.value("spread_ratio_zscore", default=None)
    spread_stress_score = current.value("spread_stress_score", default=None)
    trade_intensity_score = current.value("trade_intensity_score", default=None)
    avg_trade_size_value = current.value("avg_trade_size", default=None)
    oi_abs_zscore = current.value("oi_abs_zscore", default=None)
    oi_delta_zscore = current.value("oi_delta_zscore", default=None)
    price_oi_alignment = current.value("price_oi_alignment", default=None)
    panic_liq_score = current.value("panic_liq_score", default=None)
    exhaustion_reversal_score = current.value("exhaustion_reversal_score", default=None)

    volatility_expansion = (
        (price_move_vs_atr is not None and price_move_vs_atr >= VOLATILITY_EXPANSION_THRESHOLD)
        or (spread_vs_atr is not None and spread_vs_atr >= VOLATILITY_EXPANSION_THRESHOLD)
        or (atr_regime_zscore is not None and atr_regime_zscore >= VOLATILITY_EXPANSION_Z)
    )

    thin_orderflow_instability = (
        spread_ratio_zscore is not None
        and spread_ratio_zscore >= SPREAD_EXPANSION_Z
        and trade_intensity_score is not None
        and trade_intensity_score < TRADE_SURGE_Z * 0.5
    )

    fresh_long_build_up = (
        price_oi_alignment == 1
        and oi_abs_zscore is not None
        and oi_abs_zscore >= OI_BUILD_Z
        and oi_delta_zscore is not None
        and oi_delta_zscore >= OI_BUILD_Z * 0.5
    )
    fresh_short_build_up = (
        price_oi_alignment == -1
        and oi_abs_zscore is not None
        and oi_abs_zscore >= OI_BUILD_Z
        and oi_delta_zscore is not None
        and oi_delta_zscore >= OI_BUILD_Z * 0.5
    )

    high_participation_breakout = (
        trade_intensity_score is not None
        and trade_intensity_score >= TRADE_SURGE_Z
        and avg_trade_size_value is not None
        and current.z("avg_trade_size") > LARGE_TRADE_Z
        and price_move_vs_atr is not None
        and price_move_vs_atr >= 0.8
    )

    weak_move_low_participation = (
        trade_intensity_score is not None
        and trade_intensity_score < PARTICIPATION_WEAK_THRESHOLD
        and price_move_vs_atr is not None
        and price_move_vs_atr >= 0.25
    )

    panic_liquidation_phase = (
        panic_liq_score is not None
        and panic_liq_score >= PANIC_LIQ_THRESHOLD
        and exhaustion_reversal_score is not None
        and exhaustion_reversal_score >= PANIC_LIQ_THRESHOLD
        and price_move_vs_atr is not None
        and price_move_vs_atr >= VOLATILITY_EXPANSION_THRESHOLD
    )

    squeeze_exhaustion_reversal = (
        panic_liq_score is not None
        and panic_liq_score >= PANIC_LIQ_THRESHOLD
        and exhaustion_reversal_score is not None
        and exhaustion_reversal_score >= PANIC_LIQ_THRESHOLD
        and oi_delta_zscore is not None
        and abs(oi_delta_zscore) >= abs(OI_FLUSH_Z)
    )

    spread_stress_phase = (
        spread_stress_score is not None
        and spread_stress_score >= SPREAD_STRESS_THRESHOLD
        and (trade_intensity_score is None or trade_intensity_score < TRADE_SURGE_Z)
    )

    dirty_breakout_risk = (
        spread_stress_score is not None
        and spread_stress_score >= DIRTY_BREAKOUT_SPREAD_THRESHOLD
        and volatility_expansion
        and not high_participation_breakout
    )

    return PrimitiveEvents(
        oi_price_build_long=oi_price_build_long,
        oi_price_short_covering=oi_price_short_covering,
        oi_price_build_short=oi_price_build_short,
        oi_price_long_flush=oi_price_long_flush,
        price_impulse_up=price_impulse_up,
        price_impulse_down=price_impulse_down,
        price_extreme_up=price_extreme_up,
        price_extreme_down=price_extreme_down,
        price_flip_long=price_flip_long,
        price_flip_short=price_flip_short,
        oi_build_up=oi_build_up,
        oi_extreme_build=oi_extreme_build,
        oi_flush=oi_flush,
        oi_flip_down=oi_flip_down,
        oi_flip_up=oi_flip_up,
        volume_participation_high=volume_participation_high,
        volume_participation_extreme=volume_participation_extreme,
        trade_participation_surge=trade_participation_surge,
        large_trade_presence=large_trade_presence,
        rebound_participation_surge_long=rebound_participation_surge_long,
        rebound_participation_surge_short=rebound_participation_surge_short,
        orderflow_push_long=orderflow_push_long,
        orderflow_push_short=orderflow_push_short,
        orderflow_flip_long=orderflow_flip_long,
        orderflow_flip_short=orderflow_flip_short,
        microburst_risk=microburst_risk,
        microburst_extreme=microburst_extreme,
        liq_cluster_event=liq_cluster_event,
        liq_flush_down=liq_flush_down,
        liq_flush_up=liq_flush_up,
        spread_expansion=spread_expansion,
        spread_explosion=spread_explosion,
        liquidity_vacuum=liquidity_vacuum,
        velocity_slowdown_long=velocity_slowdown_long,
        velocity_slowdown_short=velocity_slowdown_short,
        pressure_divergence_long=pressure_divergence_long,
        pressure_divergence_short=pressure_divergence_short,
        exhaustion_long=exhaustion_long,
        exhaustion_short=exhaustion_short,
        volatility_expansion=volatility_expansion,
        thin_orderflow_instability=thin_orderflow_instability,
        fresh_long_build_up=fresh_long_build_up,
        fresh_short_build_up=fresh_short_build_up,
        high_participation_breakout=high_participation_breakout,
        weak_move_low_participation=weak_move_low_participation,
        panic_liquidation_phase=panic_liquidation_phase,
        squeeze_exhaustion_reversal=squeeze_exhaustion_reversal,
        spread_stress_phase=spread_stress_phase,
        dirty_breakout_risk=dirty_breakout_risk,
    )
