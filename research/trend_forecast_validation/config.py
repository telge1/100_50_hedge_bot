"""Central configuration for trend forecast validation (no hidden outcome constants)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS_ROOT = ROOT / "results"


@dataclass(frozen=True)
class ForecastValidationConfig:
    symbol: str = "APTUSDT"
    timeframe: str = "5m"
    data_source: str = "mysql"  # prefer MySQL; feather fallback documented in loader
    exchange: str = "bybit"
    candle_interval_minutes: int = 5

    # Calendar split (UTC). OOS must not drive parameter choice.
    warmup_start: str = "2026-01-01T00:00:00+00:00"
    warmup_end: str = "2026-03-31T23:59:59+00:00"
    development_start: str = "2026-04-01T00:00:00+00:00"
    development_end: str = "2026-05-31T23:59:59+00:00"
    out_of_sample_start: str = "2026-06-01T00:00:00+00:00"
    # None = last available closed candle
    out_of_sample_end: str | None = None

    min_warmup_days: int = 90
    gap_tolerance_minutes: int = 5  # expected grid; larger gaps are reported

    htf_timeframes: tuple[str, ...] = ("30m", "4h")

    # Horizons in 5m bars
    horizons_bars: tuple[int, ...] = (6, 12, 24, 48, 96, 288)

    # Percent targets (absolute); sign applied by direction
    percent_targets: tuple[float, ...] = (0.25, 0.50, 1.00)
    atr_target_multiples: tuple[float, ...] = (0.5, 1.0, 1.5)

    ambiguity_mode: str = "conservative"  # primary; also emit optimistic bounds
    structure_variant: str = "protected_medium"

    signal_types: tuple[str, ...] = (
        "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK",
        "BEARISH_EXTERNAL_BOS_AFTER_PULLBACK",
        "BULLISH_CHOCH",
        "BEARISH_CHOCH",
        "PROTECTED_LOW_BREAK",
        "PROTECTED_HIGH_BREAK",
    )

    primary_continuation_types: tuple[str, ...] = (
        "BULLISH_EXTERNAL_BOS_AFTER_PULLBACK",
        "BEARISH_EXTERNAL_BOS_AFTER_PULLBACK",
    )

    write_candle_trace: bool = True
    write_optional_event_timeline: bool = True

    timezone_name: str = "UTC"
    candle_timestamp_semantics: str = (
        "timestamp = candle open time (UTC); decision_time = open + 5m = close; "
        "only fully closed candles are used; HTF usable iff HTF close_decision <= LTF decision_time"
    )

    adx_buckets: tuple[tuple[str, float | None, float | None], ...] = (
        ("adx_lt_20", None, 20.0),
        ("adx_20_25", 20.0, 25.0),
        ("adx_25_35", 25.0, 35.0),
        ("adx_ge_35", 35.0, None),
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def default_config() -> ForecastValidationConfig:
    return ForecastValidationConfig()


def parse_utc(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
    else:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def run_output_dir(run_date: str | None = None) -> Path:
    day = run_date or datetime.now(timezone.utc).strftime("%Y%m%d")
    return DEFAULT_RESULTS_ROOT / f"aptusdt_forecast_validation_{day}"
