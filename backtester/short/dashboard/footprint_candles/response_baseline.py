"""Causal baseline: percentiles over prior valid feature samples in [t−30m, t).

Method (documented):
  empirical_percentiles_causal_prior_1s_features

For each prior second t' with enough valid window coverage, collect the
primary-window feature vector. Classification at t compares current features
to the percentile rank within that prior distribution. The second t itself is
never included in its own baseline.

Gaps: missing seconds are omitted (not treated as zero activity). If fewer
than MIN_BASELINE_VALID_SECONDS prior samples exist, return INSUFFICIENT_BASELINE.
Known gap overlap invalidates the baseline window when flagged by the caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .response_contracts import (
    MIN_BASELINE_VALID_SECONDS,
    ResponseState,
)


def percentile_rank(sorted_vals: Sequence[float], value: float) -> float:
    """Percentile rank in [0, 100] via mid-rank. Empty → 50.0 neutral."""
    n = len(sorted_vals)
    if n <= 0:
        return 50.0
    # Count strictly less / equal
    lo = 0
    hi = 0
    for v in sorted_vals:
        if v < value:
            lo += 1
            hi += 1
        elif v == value:
            hi += 1
        else:
            break
    # mid-rank of equal band
    mid = (lo + hi) / 2.0
    return 100.0 * mid / n


def empirical_percentile(sorted_vals: Sequence[float], p: float) -> float | None:
    """Linear interpolation percentile; p in [0, 100]."""
    if not sorted_vals:
        return None
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 100:
        return float(sorted_vals[-1])
    n = len(sorted_vals)
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return float(sorted_vals[lo]) * (1.0 - frac) + float(sorted_vals[hi]) * frac


def median_mad(values: Sequence[float]) -> tuple[float | None, float | None]:
    """Robust location/scale helpers (not used for primary classify)."""
    if not values:
        return None, None
    s = sorted(float(v) for v in values)
    mid = len(s) // 2
    if len(s) % 2:
        med = s[mid]
    else:
        med = 0.5 * (s[mid - 1] + s[mid])
    devs = sorted(abs(v - med) for v in s)
    mid2 = len(devs) // 2
    if len(devs) % 2:
        mad = devs[mid2]
    else:
        mad = 0.5 * (devs[mid2 - 1] + devs[mid2])
    return med, mad


@dataclass
class BaselineDistributions:
    """Sorted sample lists for percentile ranking. Empty → insufficient."""

    buy_notional_rate: list[float] = field(default_factory=list)
    sell_notional_rate: list[float] = field(default_factory=list)
    abs_delta_notional_rate: list[float] = field(default_factory=list)
    up_velocity: list[float] = field(default_factory=list)
    down_velocity: list[float] = field(default_factory=list)
    buy_efficiency: list[float] = field(default_factory=list)
    sell_efficiency: list[float] = field(default_factory=list)
    buy_response: list[float] = field(default_factory=list)
    sell_response: list[float] = field(default_factory=list)
    up_progress: list[float] = field(default_factory=list)
    down_progress: list[float] = field(default_factory=list)
    total_notional_rate: list[float] = field(default_factory=list)
    trade_rate: list[float] = field(default_factory=list)
    sample_count: int = 0
    valid_second_buckets: int = 0
    invalidated: bool = False
    invalidate_reason: str | None = None

    @property
    def sufficient(self) -> bool:
        if self.invalidated:
            return False
        # Spec: ≥900 valid 1s buckets in the causal lookback; feature
        # rows may be fewer (coverage filter) but still usable.
        return (
            self.valid_second_buckets >= MIN_BASELINE_VALID_SECONDS
            and self.sample_count >= 100
        )

    def ranks(self, features: dict[str, float]) -> dict[str, float]:
        vel = float(features.get("price_velocity_bps_per_second") or 0.0)
        return {
            "buy_aggression_percentile": percentile_rank(
                self.buy_notional_rate, float(features.get("buy_notional_rate") or 0.0)
            ),
            "sell_aggression_percentile": percentile_rank(
                self.sell_notional_rate, float(features.get("sell_notional_rate") or 0.0)
            ),
            "abs_delta_percentile": percentile_rank(
                self.abs_delta_notional_rate,
                abs(float(features.get("delta_notional_rate") or 0.0)),
            ),
            "up_velocity_percentile": percentile_rank(self.up_velocity, max(vel, 0.0)),
            "down_velocity_percentile": percentile_rank(
                self.down_velocity, max(-vel, 0.0)
            ),
            "buy_efficiency_percentile": percentile_rank(
                self.buy_efficiency, float(features.get("buy_efficiency") or 0.0)
            ),
            "sell_efficiency_percentile": percentile_rank(
                self.sell_efficiency, float(features.get("sell_efficiency") or 0.0)
            ),
            "buy_response_percentile": percentile_rank(
                self.buy_response, float(features.get("response_ratio_buy") or 0.0)
            ),
            "sell_response_percentile": percentile_rank(
                self.sell_response, float(features.get("response_ratio_sell") or 0.0)
            ),
            "up_progress_percentile": percentile_rank(
                self.up_progress,
                max(float(features.get("price_move_bps") or 0.0), 0.0),
            ),
            "down_progress_percentile": percentile_rank(
                self.down_progress,
                max(-float(features.get("price_move_bps") or 0.0), 0.0),
            ),
            "total_notional_percentile": percentile_rank(
                self.total_notional_rate, float(features.get("total_notional_rate") or 0.0)
            ),
            "trade_rate_percentile": percentile_rank(
                self.trade_rate, float(features.get("trade_rate") or 0.0)
            ),
        }

    def to_meta(self) -> dict[str, Any]:
        return {
            "sample_count": self.sample_count,
            "valid_second_buckets": self.valid_second_buckets,
            "sufficient": self.sufficient,
            "min_required_second_buckets": MIN_BASELINE_VALID_SECONDS,
            "method": "empirical_percentiles_causal_prior_1s_features",
            "invalidated": self.invalidated,
            "invalidate_reason": self.invalidate_reason,
            "status": (
                ResponseState.INSUFFICIENT_BASELINE.value
                if not self.sufficient
                else "OK"
            ),
        }


def build_baseline_from_feature_rows(
    rows: Sequence[dict[str, float]],
    *,
    invalidated: bool = False,
    invalidate_reason: str | None = None,
    valid_second_buckets: int | None = None,
) -> BaselineDistributions:
    """Assemble sorted distributions from prior feature dicts (each at some t' < t)."""
    dist = BaselineDistributions(
        invalidated=invalidated,
        invalidate_reason=invalidate_reason,
        valid_second_buckets=(
            int(valid_second_buckets)
            if valid_second_buckets is not None
            else len(rows)
        ),
    )
    if invalidated:
        return dist
    for row in rows:
        dist.buy_notional_rate.append(float(row.get("buy_notional_rate", 0.0)))
        dist.sell_notional_rate.append(float(row.get("sell_notional_rate", 0.0)))
        dist.abs_delta_notional_rate.append(
            abs(float(row.get("delta_notional_rate", 0.0)))
        )
        vel = float(row.get("price_velocity_bps_per_second", 0.0) or 0.0)
        dist.up_velocity.append(max(vel, 0.0))
        dist.down_velocity.append(max(-vel, 0.0))
        dist.buy_efficiency.append(float(row.get("buy_efficiency", 0.0) or 0.0))
        dist.sell_efficiency.append(float(row.get("sell_efficiency", 0.0) or 0.0))
        dist.buy_response.append(float(row.get("response_ratio_buy", 0.0) or 0.0))
        dist.sell_response.append(float(row.get("response_ratio_sell", 0.0) or 0.0))
        mv = float(row.get("price_move_bps", 0.0) or 0.0)
        dist.up_progress.append(max(mv, 0.0))
        dist.down_progress.append(max(-mv, 0.0))
        dist.total_notional_rate.append(float(row.get("total_notional_rate", 0.0)))
        dist.trade_rate.append(float(row.get("trade_rate", 0.0)))
    dist.sample_count = len(rows)
    for name in (
        "buy_notional_rate",
        "sell_notional_rate",
        "abs_delta_notional_rate",
        "up_velocity",
        "down_velocity",
        "buy_efficiency",
        "sell_efficiency",
        "buy_response",
        "sell_response",
        "up_progress",
        "down_progress",
        "total_notional_rate",
        "trade_rate",
    ):
        getattr(dist, name).sort()
    return dist


__all__ = [
    "percentile_rank",
    "empirical_percentile",
    "median_mad",
    "BaselineDistributions",
    "build_baseline_from_feature_rows",
]
