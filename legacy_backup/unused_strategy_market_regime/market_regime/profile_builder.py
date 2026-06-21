from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math
from statistics import mean, median, pstdev
from typing import Iterable

from .config import EPSILON, PROFILE_ROLLING_DAYS
from .db import PROFILED_FEATURES, MarketRegimeStore
from .models import CoinProfile, FeatureProfileStats


@dataclass(slots=True)
class ProfileBuildResult:
    profiles: dict[str, CoinProfile]
    skipped_symbols: dict[str, str]
    window_start: datetime
    window_end: datetime


class ProfileBuilder:
    def __init__(
        self,
        store: MarketRegimeStore,
        *,
        rolling_days: int = PROFILE_ROLLING_DAYS,
        profile_version: int = 1,
    ) -> None:
        self.store = store
        self.rolling_days = rolling_days
        self.profile_version = profile_version

    def build_profiles(
        self,
        symbols: list[str] | None = None,
        *,
        end_time: datetime | None = None,
        rolling_days: int | None = None,
    ) -> ProfileBuildResult:
        window_end = end_time or datetime.now(timezone.utc)
        days = rolling_days or self.rolling_days
        window_start = window_end - timedelta(days=days)
        target_symbols = symbols or self.store.load_active_symbols()
        grouped_rows = self.store.load_raw_feature_rows(
            symbols=target_symbols,
            start_time=window_start,
            end_time=window_end,
        )

        profiles: dict[str, CoinProfile] = {}
        skipped: dict[str, str] = {}
        for symbol in target_symbols:
            rows = grouped_rows.get(symbol.upper(), [])
            if not rows:
                skipped[symbol.upper()] = "no_rows_in_window"
                continue
            profile = self._build_profile_for_rows(
                symbol=symbol.upper(),
                rows=rows,
                window_start=window_start,
                window_end=window_end,
            )
            if profile is None:
                skipped[symbol.upper()] = "insufficient_feature_data"
                continue
            profiles[symbol.upper()] = profile

        return ProfileBuildResult(
            profiles=profiles,
            skipped_symbols=skipped,
            window_start=window_start,
            window_end=window_end,
        )

    def persist_profiles(
        self,
        result: ProfileBuildResult,
        *,
        write_history: bool = True,
    ) -> None:
        profiles = list(result.profiles.values())
        self.store.upsert_coin_profiles(profiles)
        if write_history:
            self.store.insert_coin_profile_history(profiles, snapshot_time=result.window_end)

    def build_and_persist(
        self,
        symbols: list[str] | None = None,
        *,
        end_time: datetime | None = None,
        rolling_days: int | None = None,
        write_history: bool = True,
    ) -> ProfileBuildResult:
        result = self.build_profiles(symbols=symbols, end_time=end_time, rolling_days=rolling_days)
        self.persist_profiles(result, write_history=write_history)
        return result

    def _build_profile_for_rows(
        self,
        *,
        symbol: str,
        rows: list[dict],
        window_start: datetime,
        window_end: datetime,
    ) -> CoinProfile | None:
        feature_values = self._extract_feature_values(rows)
        usable_features = {
            feature_name: values for feature_name, values in feature_values.items() if values
        }
        if not usable_features:
            return None

        feature_stats = {
            feature_name: self._build_feature_stats(feature_name, values)
            for feature_name, values in usable_features.items()
        }
        profile = CoinProfile(
            symbol=symbol,
            updated_at=window_end,
            sample_size=len(rows),
            profile_version=self.profile_version,
            window_start=window_start,
            window_end=window_end,
            features=feature_stats,
        )
        return profile

    def _extract_feature_values(self, rows: list[dict]) -> dict[str, list[float]]:
        feature_values: dict[str, list[float]] = {feature_name: [] for feature_name in PROFILED_FEATURES}
        for row in rows:
            price = self._safe_float(row.get("price"))
            trade_volume = self._safe_float(row.get("trade_volume_1m"))
            delta = self._safe_float(row.get("delta"))
            spread = self._safe_float(row.get("spread"))
            trade_count = self._safe_float(row.get("trade_count_1m"), none_default=None)
            no_trade_signal = (trade_count or 0.0) <= 0 or (trade_volume or 0.0) <= 0

            raw_values = {
                "price_change_1m": self._safe_float(row.get("price_change_1m"), none_default=None),
                "price_change_5m": self._safe_float(row.get("price_change_5m"), none_default=None),
                "price_change_15m": self._safe_float(row.get("price_change_15m"), none_default=None),
                "oi_change_ratio": self._safe_float(row.get("oi_change_ratio"), none_default=None),
                "trade_volume_1m": self._safe_float(row.get("trade_volume_1m"), none_default=None),
                "volume_spike_ratio": self._safe_float(row.get("volume_spike_ratio"), none_default=None),
                "orderflow_ratio": self._safe_float(row.get("orderflow_ratio"), none_default=None),
                "delta_ratio": self._safe_div(delta, trade_volume),
                "microburst_score": None
                if no_trade_signal
                else self._safe_float(row.get("microburst_score"), none_default=None),
                "liquidation_density_5m": self._safe_float(row.get("liquidation_density_5m"), none_default=None),
                "liquidation_cluster_score": self._safe_float(row.get("liquidation_cluster_score"), none_default=None),
                "spread_ratio": self._safe_div(spread, price),
                "trade_count_1m": trade_count,
                "avg_trade_size": None
                if no_trade_signal
                else self._safe_float(row.get("avg_trade_size"), none_default=None),
                "atr_1m": self._safe_float(row.get("atr_1m"), none_default=None),
            }
            for feature_name, value in raw_values.items():
                if value is None or math.isnan(value):
                    continue
                feature_values[feature_name].append(float(value))
        return feature_values

    def _build_feature_stats(self, feature_name: str, values: list[float]) -> FeatureProfileStats:
        ordered = sorted(values)
        feature_stats = FeatureProfileStats(
            mean=mean(ordered),
            std=pstdev(ordered) if len(ordered) > 1 else 0.0,
            p50=self._percentile(ordered, 0.50),
            p75=self._percentile(ordered, 0.75),
            p90=self._percentile(ordered, 0.90),
            p95=self._percentile(ordered, 0.95),
            p99=self._percentile(ordered, 0.99),
            median=median(ordered),
            mad=self._median_abs_deviation(ordered),
        )
        if feature_name.startswith("price_change"):
            negatives = sorted(value for value in ordered if value < 0)
            if negatives:
                feature_stats.n95 = self._percentile(negatives, 0.05)
                feature_stats.n99 = self._percentile(negatives, 0.01)
        return feature_stats

    @staticmethod
    def _percentile(sorted_values: list[float], quantile: float) -> float:
        if not sorted_values:
            return 0.0
        if len(sorted_values) == 1:
            return sorted_values[0]
        position = (len(sorted_values) - 1) * quantile
        lower_index = int(math.floor(position))
        upper_index = int(math.ceil(position))
        if lower_index == upper_index:
            return sorted_values[lower_index]
        lower_value = sorted_values[lower_index]
        upper_value = sorted_values[upper_index]
        weight = position - lower_index
        return lower_value + (upper_value - lower_value) * weight

    @staticmethod
    def _median_abs_deviation(values: Iterable[float]) -> float:
        ordered = sorted(values)
        if not ordered:
            return 0.0
        med = median(ordered)
        deviations = [abs(value - med) for value in ordered]
        return median(deviations)

    @staticmethod
    def _safe_div(numerator: float, denominator: float) -> float | None:
        if abs(denominator) <= EPSILON:
            return None
        return numerator / denominator

    @staticmethod
    def _safe_float(value: object, none_default: float | None = 0.0) -> float | None:
        if value is None:
            return none_default
        try:
            return float(value)
        except (TypeError, ValueError):
            return none_default
