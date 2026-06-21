from __future__ import annotations

from datetime import datetime

from .config import EPSILON, MIN_STD
from .models import CoinProfile, FeatureProfileStats, NormalizedSnapshot, RawMarketSnapshot

PROFILE_ALIASES = {
    "velocity_1m": "price_change_1m",
    "velocity_5m": "price_change_5m",
    "velocity_15m": "price_change_15m",
}


def calc_zscore(current: float, mean: float, std: float, min_std: float = MIN_STD) -> float:
    return (current - mean) / max(abs(std), min_std)


def _safe_float(value: float | int | None, default: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    if abs(denominator) <= EPSILON:
        return default
    return numerator / denominator


MIN_MICROBURST_TRADES = 2
MIN_MICROBURST_VOLUME = 1.0


def _stats_for(profile: CoinProfile, feature_name: str) -> FeatureProfileStats:
    direct = profile.get_stats(feature_name)
    if feature_name in profile.features:
        return direct
    alias = PROFILE_ALIASES.get(feature_name)
    if alias:
        return profile.get_stats(alias)
    return direct


def compute_oi_price_state(
    *,
    price_change_1m: float | int | None,
    oi_change: float | int | None,
) -> str:
    price_change_1m = None if price_change_1m is None else float(price_change_1m)
    oi_change = None if oi_change is None else float(oi_change)
    if price_change_1m is None or oi_change is None:
        return "neutral"
    if price_change_1m > 0 and oi_change > 0:
        return "price_up_oi_up"
    if price_change_1m > 0 and oi_change < 0:
        return "price_up_oi_down"
    if price_change_1m < 0 and oi_change > 0:
        return "price_down_oi_up"
    if price_change_1m < 0 and oi_change < 0:
        return "price_down_oi_down"
    return "neutral"


def _compute_percentile_flags(
    feature_name: str,
    value: float,
    stats: FeatureProfileStats,
) -> dict[str, bool]:
    flags: dict[str, bool] = {}
    flags[f"{feature_name}_gte_p95"] = value >= stats.p95
    flags[f"{feature_name}_gte_p99"] = value >= stats.p99
    if stats.n95 is not None:
        flags[f"{feature_name}_lte_n95"] = value <= stats.n95
    if stats.n99 is not None:
        flags[f"{feature_name}_lte_n99"] = value <= stats.n99
    return flags


def _empty_percentile_flags(feature_name: str, stats: FeatureProfileStats) -> dict[str, bool]:
    flags = {
        f"{feature_name}_gte_p95": False,
        f"{feature_name}_gte_p99": False,
    }
    if stats.n95 is not None:
        flags[f"{feature_name}_lte_n95"] = False
    if stats.n99 is not None:
        flags[f"{feature_name}_lte_n99"] = False
    return flags


def calc_price_move_vs_atr(price_change: float | None, atr: float | None) -> float | None:
    if price_change is None or atr is None or atr <= 0:
        return None
    return abs(price_change) / atr


def calc_spread_vs_atr(spread_ratio: float | None, atr: float | None) -> float | None:
    if spread_ratio is None or atr is None or atr <= 0:
        return None
    return spread_ratio / atr


def calc_price_oi_alignment(price_change: float | None, oi_change: float | None) -> int | None:
    if price_change in (None, 0.0) or oi_change in (None, 0.0):
        return None
    return 1 if price_change * oi_change > 0 else -1


def calc_avg_trade_size(trade_volume: float | None, trade_count: float | None) -> float | None:
    if not trade_count or trade_count <= 0 or trade_volume is None:
        return None
    return trade_volume / trade_count


def calc_trade_intensity_score(trade_count: float | None) -> float | None:
    if trade_count is None or trade_count <= 0:
        return None
    return float(trade_count)


def calc_panic_liq_score(
    density: float | None,
    cluster: float | None,
) -> float | None:
    parts: list[float] = []
    if density is not None:
        parts.append(density)
    if cluster is not None:
        parts.append(cluster)
    if not parts:
        return None
    return sum(parts) / len(parts)


def calc_exhaustion_reversal_score(
    panic_score: float | None,
    price_change: float | None,
    atr: float | None,
) -> float | None:
    if panic_score is None or price_change is None or atr is None or atr <= 0:
        return None
    return panic_score * abs(price_change) / atr


def calc_spread_stress_score(
    spread_ratio: float | None,
    trade_volume: float | None,
) -> float | None:
    if spread_ratio is None or trade_volume is None or trade_volume <= 0:
        return None
    return spread_ratio * (1 + 1 / trade_volume)


def _calc_feature_zscore(
    profile: CoinProfile,
    feature_name: str,
    value: float | None,
) -> float | None:
    if value is None:
        return None
    stats = _stats_for(profile, feature_name)
    if (
        stats.std == 0.0
        and stats.mean == 0.0
        and stats.p95 == 0.0
        and stats.p99 == 0.0
    ):
        return None
    return calc_zscore(value, stats.mean, stats.std)


def _normalize_zero_vs_missing(value: float | None) -> float | None:
    if value is None:
        return None
    return value


def _enrich_derived_features(
    derived: dict[str, float | None],
    current: RawMarketSnapshot,
    profile: CoinProfile,
) -> None:
    derived["atr_regime_zscore"] = _calc_feature_zscore(profile, "atr_1m", _safe_float(current.atr_1m))
    derived["oi_abs_zscore"] = _calc_feature_zscore(
        profile, "open_interest", _safe_float(current.get("open_interest"))
    )
    derived["oi_delta_zscore"] = _calc_feature_zscore(
        profile, "oi_change", _safe_float(current.get("oi_change"))
    )
    derived["price_oi_alignment"] = calc_price_oi_alignment(
        current.get("price_change_1m"), current.get("oi_change")
    )
    derived["trade_intensity_score"] = _calc_feature_zscore(
        profile, "trade_count_1m", derived.get("trade_intensity_base")
    )
    derived["spread_ratio_zscore"] = _calc_feature_zscore(
        profile, "spread_ratio", derived.get("spread_ratio")
    )
    derived.pop("trade_intensity_base", None)


def _microburst_valid(
    trade_count: float | None,
    trade_volume: float | None,
) -> bool:
    return (
        trade_count is not None
        and trade_volume is not None
        and trade_count >= MIN_MICROBURST_TRADES
        and trade_volume >= MIN_MICROBURST_VOLUME
    )


def derive_features(
    current: RawMarketSnapshot,
    previous: RawMarketSnapshot | None = None,
) -> dict[str, float | None]:
    prev_velocity_1m = previous.get("price_change_1m") if previous is not None else 0.0
    prev_velocity_5m = _safe_div(previous.get("price_change_5m"), 5.0) if previous is not None else 0.0
    prev_oi_change_ratio = previous.get("oi_change_ratio") if previous is not None else 0.0
    prev_volume_spike_ratio = previous.get("volume_spike_ratio") if previous is not None else 0.0
    prev_trade_count = previous.get("trade_count_1m") if previous is not None else 0.0

    price = current.get("price")
    delta = current.get("delta")
    trade_volume_1m = current.get("trade_volume_1m")
    buy_volume = current.get("buy_volume")
    sell_volume = current.get("sell_volume")
    spread = current.get("spread")
    velocity_1m = current.get("price_change_1m")
    velocity_5m = _safe_div(current.get("price_change_5m"), 5.0)
    velocity_15m = _safe_div(current.get("price_change_15m"), 15.0)
    atr_1m = _safe_float(current.atr_1m, None)

    derived = {
        "delta_ratio": _safe_div(delta, trade_volume_1m),
        "velocity_1m": velocity_1m,
        "velocity_5m": velocity_5m,
        "velocity_15m": velocity_15m,
        "acceleration_1m": velocity_1m - prev_velocity_1m,
        "acceleration_5m": velocity_5m - prev_velocity_5m,
        "buy_sell_imbalance": _safe_div(buy_volume - sell_volume, buy_volume + sell_volume),
        "spread_ratio": _safe_div(spread, price),
        "trade_count_delta": current.get("trade_count_1m") - prev_trade_count,
        "oi_slope_short": current.get("oi_change_ratio") - prev_oi_change_ratio,
        "volume_slope_short": current.get("volume_spike_ratio") - prev_volume_spike_ratio,
    }
    trade_count = current.get("trade_count_1m")
    panic_score = calc_panic_liq_score(
        current.get("liquidation_density_5m"),
        current.get("liquidation_cluster_score"),
    )
    derived.update(
        {
            "price_move_vs_atr": calc_price_move_vs_atr(velocity_1m, atr_1m),
            "spread_vs_atr": calc_spread_vs_atr(derived["spread_ratio"], atr_1m),
            "avg_trade_size": calc_avg_trade_size(trade_volume_1m, trade_count),
            "trade_intensity_base": trade_count if trade_count and trade_count > 0 else None,
            "panic_liq_score": panic_score,
            "exhaustion_reversal_score": calc_exhaustion_reversal_score(
                panic_score, velocity_1m, atr_1m
            ),
            "spread_stress_score": calc_spread_stress_score(
                derived["spread_ratio"], trade_volume_1m
            ),
            "atr_move_ratio": calc_price_move_vs_atr(velocity_1m, atr_1m),
        }
    )
    return derived


def normalize_snapshot(
    current: RawMarketSnapshot,
    previous: RawMarketSnapshot | None,
    profile: CoinProfile,
) -> NormalizedSnapshot:
    derived = derive_features(current, previous)
    _enrich_derived_features(derived, current, profile)
    categoricals = {
        "oi_price_state": compute_oi_price_state(
            price_change_1m=current.price_change_1m,
            oi_change=current.oi_change,
        )
    }
    zscores: dict[str, float] = {}
    percentile_flags: dict[str, bool] = {}
    missing_inputs: set[str] = set()
    no_trade_signal = current.get("trade_count_1m") <= 0 or current.get("trade_volume_1m") <= 0
    avg_trade_size_value = derived.get("avg_trade_size")
    microburst_score_value = (
        current.get("microburst_score")
        if _microburst_valid(current.get("trade_count_1m"), current.get("trade_volume_1m"))
        else None
    )
    atr_move_ratio_value = derived.get("price_move_vs_atr")

    feature_values: dict[str, float | None] = {
        "price_change_1m": current.get("price_change_1m"),
        "price_change_5m": current.get("price_change_5m"),
        "price_change_15m": current.get("price_change_15m"),
        "oi_change_ratio": current.get("oi_change_ratio"),
        "trade_volume_1m": current.get("trade_volume_1m"),
        "volume_spike_ratio": current.get("volume_spike_ratio"),
        "orderflow_ratio": current.get("orderflow_ratio"),
        "delta_ratio": derived["delta_ratio"],
        "microburst_score": microburst_score_value,
        "liquidation_density_5m": current.get("liquidation_density_5m"),
        "liquidation_cluster_score": current.get("liquidation_cluster_score"),
        "spread_ratio": derived["spread_ratio"],
        "trade_count_1m": current.get("trade_count_1m"),
        "avg_trade_size": avg_trade_size_value,
        "velocity_1m": derived["velocity_1m"],
        "velocity_5m": derived["velocity_5m"],
        "velocity_15m": derived["velocity_15m"],
        "atr_move_ratio": atr_move_ratio_value,
    }

    for feature_name, value in feature_values.items():
        stats = _stats_for(profile, feature_name)
        if value is None:
            missing_inputs.add(feature_name)
            zscores[feature_name] = 0.0
            percentile_flags.update(_empty_percentile_flags(feature_name, stats))
            continue
        if stats.std == 0.0 and stats.mean == 0.0 and stats.p95 == 0.0 and stats.p99 == 0.0:
            missing_inputs.add(feature_name)
            zscores[feature_name] = 0.0
            percentile_flags.update(_empty_percentile_flags(feature_name, FeatureProfileStats()))
            continue
        zscores[feature_name] = calc_zscore(value, stats.mean, stats.std)
        percentile_flags.update(_compute_percentile_flags(feature_name, value, stats))

    return NormalizedSnapshot(
        symbol=current.symbol,
        ts=current.ts if isinstance(current.ts, datetime) else datetime.utcnow(),
        raw=current,
        derived=derived,
        categoricals=categoricals,
        zscores=zscores,
        percentile_flags=percentile_flags,
        missing_inputs=missing_inputs,
    )
