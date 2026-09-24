"""Aggression–Velocity–Response Engine V1 contracts (provisional thresholds).

Classification priority (exclusive, first match wins after contradiction guards):
  1. INSUFFICIENT_DATA / INSUFFICIENT_BASELINE
  2. SELLER_CONTROL / BUYER_CONTROL
  3. SELL_ABSORPTION_CANDIDATE / BUY_ABSORPTION_CANDIDATE
  4. VACUUM_DOWN_PROXY / VACUUM_UP_PROXY  (PROXY_UNCONFIRMED)
  5. BALANCED

Causality: available_at = T uses only trades with trade_ts < T
(1s buckets with second_ts in [T - window, T)).

Efficiency (documented):
  impact_efficiency_bps_per_million =
      max(directional_move_bps, 0) / max(aggression_notional_millions, eps)
  where directional_move_bps is the move in the aggression direction
  (sell → max(-move_bps,0), buy → max(move_bps,0)).

  Unit: bps per USDT million notional.

  High volume alone does NOT imply absorption: CONTROL also requires
  absolute progress + fast velocity; ABSORPTION is blocked when
  progress+velocity+efficiency are jointly strong (contradiction guard).

Baseline method: empirical_percentiles_causal_prior_1s_features over [T-30m, T).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any


RESPONSE_ENGINE_ID = "aggression-velocity-response-v1"
RESPONSE_ENGINE_VERSION = "1.1.0"

BASELINE_LOOKBACK_S = 30 * 60  # 1800
MIN_BASELINE_VALID_SECONDS = 900
CANDLE_SECONDS = 300
THIRD_BOUNDS = {
    "EARLY": (0, 100),
    "MIDDLE": (100, 200),
    "LATE": (200, 300),
}
ROLLING_WINDOWS_S = (1, 3, 5, 15, 30)
PRIMARY_WINDOW_S = 15

EPS_NOTIONAL_MILLIONS = 1e-9
NOTIONAL_TO_MILLIONS = 1e-6

VERIFICATION_VERIFIED = "VERIFIED"
VERIFICATION_UNVERIFIED = "UNVERIFIED"
VACUUM_CONFIRMATION = "PROXY_UNCONFIRMED"

LIFECYCLE_PROVISIONAL = "PROVISIONAL"
LIFECYCLE_CLOSED = "CLOSED"

# Dominant-state priority when strengths tie (higher = preferred).
STATE_PRIORITY = {
    "SELLER_CONTROL": 80,
    "BUYER_CONTROL": 80,
    "SELL_ABSORPTION_CANDIDATE": 60,
    "BUY_ABSORPTION_CANDIDATE": 60,
    "VACUUM_DOWN_PROXY": 40,
    "VACUUM_UP_PROXY": 40,
    "BALANCED": 10,
    "INSUFFICIENT_DATA": 0,
    "INSUFFICIENT_BASELINE": 0,
}


class ResponseState(str, Enum):
    SELLER_CONTROL = "SELLER_CONTROL"
    BUYER_CONTROL = "BUYER_CONTROL"
    SELL_ABSORPTION_CANDIDATE = "SELL_ABSORPTION_CANDIDATE"
    BUY_ABSORPTION_CANDIDATE = "BUY_ABSORPTION_CANDIDATE"
    VACUUM_DOWN_PROXY = "VACUUM_DOWN_PROXY"
    VACUUM_UP_PROXY = "VACUUM_UP_PROXY"
    BALANCED = "BALANCED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INSUFFICIENT_BASELINE = "INSUFFICIENT_BASELINE"


BADGE_LABELS: dict[str, str] = {
    ResponseState.SELLER_CONTROL.value: "S CTRL",
    ResponseState.BUYER_CONTROL.value: "B CTRL",
    ResponseState.SELL_ABSORPTION_CANDIDATE.value: "S ABS",
    ResponseState.BUY_ABSORPTION_CANDIDATE.value: "B ABS",
    ResponseState.VACUUM_DOWN_PROXY.value: "VAC↓?",
    ResponseState.VACUUM_UP_PROXY.value: "VAC↑?",
    ResponseState.BALANCED.value: "",
    ResponseState.INSUFFICIENT_DATA.value: "DATA?",
    ResponseState.INSUFFICIENT_BASELINE.value: "DATA?",
}


@dataclass(frozen=True)
class AvrThresholds:
    """Provisional relative thresholds — not outcome-tuned."""

    high_aggression_percentile: float = 90.0
    fast_velocity_percentile: float = 90.0
    high_efficiency_percentile: float = 70.0
    low_efficiency_percentile: float = 35.0
    minimum_directional_dominance: float = 0.55
    minimum_valid_seconds: float = 0.5
    vacuum_aggression_max_percentile: float = 50.0
    slow_velocity_max_percentile: float = 40.0
    # Absolute progress floors (bps over primary window) — provisional.
    min_control_progress_bps: float = 1.5
    weak_progress_bps: float = 0.75
    primary_window_s: int = PRIMARY_WINDOW_S
    provisional: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_THRESHOLDS = AvrThresholds()


def config_hash(thresholds: AvrThresholds = DEFAULT_THRESHOLDS) -> str:
    blob = json.dumps(
        {
            "engine": RESPONSE_ENGINE_ID,
            "version": RESPONSE_ENGINE_VERSION,
            "baseline_lookback_s": BASELINE_LOOKBACK_S,
            "min_baseline_valid_seconds": MIN_BASELINE_VALID_SECONDS,
            "rolling_windows_s": list(ROLLING_WINDOWS_S),
            "causality": "half_open_[T-W,T)_trade_ts_lt_T",
            "thresholds": thresholds.to_dict(),
            "baseline_method": "empirical_percentiles_causal_prior_1s_features",
            "classification_priority": [
                "CONTROL",
                "ABSORPTION",
                "VACUUM_PROXY",
                "BALANCED",
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def badge_for_state(state: str | ResponseState) -> str:
    key = state.value if isinstance(state, ResponseState) else str(state)
    return BADGE_LABELS.get(key, "")


def clv(open_: float, high: float, low: float, close: float) -> float:
    span = float(high) - float(low)
    if span <= 0.0 or not (span == span):
        return 0.5
    return (float(close) - float(low)) / span


__all__ = [
    "RESPONSE_ENGINE_ID",
    "RESPONSE_ENGINE_VERSION",
    "BASELINE_LOOKBACK_S",
    "MIN_BASELINE_VALID_SECONDS",
    "CANDLE_SECONDS",
    "THIRD_BOUNDS",
    "ROLLING_WINDOWS_S",
    "PRIMARY_WINDOW_S",
    "EPS_NOTIONAL_MILLIONS",
    "NOTIONAL_TO_MILLIONS",
    "VERIFICATION_VERIFIED",
    "VERIFICATION_UNVERIFIED",
    "VACUUM_CONFIRMATION",
    "LIFECYCLE_PROVISIONAL",
    "LIFECYCLE_CLOSED",
    "STATE_PRIORITY",
    "ResponseState",
    "BADGE_LABELS",
    "AvrThresholds",
    "DEFAULT_THRESHOLDS",
    "config_hash",
    "badge_for_state",
    "clv",
]
