"""Central display formatting for BTC OB Fight facts."""

from __future__ import annotations

import math
from typing import Any


def normalize_zero(value: float, *, decimals: int = 2) -> float:
    rounded = round(value, decimals)
    if rounded == 0:
        return 0.0
    return rounded


def fmt_bps(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    v = normalize_zero(float(value), decimals=decimals)
    sign = "+" if v > 0 else ""
    if v == 0:
        return f"{v:.{decimals}f} bps"
    return f"{sign}{v:.{decimals}f} bps"


def fmt_duration_seconds(value: float | None, *, decimals: int = 3) -> str:
    if value is None:
        return "n/a"
    v = normalize_zero(float(value), decimals=decimals)
    return f"{v:.{decimals}f} Sekunden"


def fmt_pct(value: float | None, *, decimals: int = 3) -> str:
    if value is None:
        return "n/a"
    v = normalize_zero(float(value), decimals=decimals)
    sign = "+" if v > 0 else ""
    if v == 0:
        return f"{v:.{decimals}f} %"
    return f"{sign}{v:.{decimals}f} %"


def fmt_fraction_as_pct(value: float | None, *, decimals: int = 1) -> str:
    """Format a 0–1 fraction as a percentage display (no leading sign)."""
    if value is None:
        return "n/a"
    v = normalize_zero(float(value) * 100.0, decimals=decimals)
    return f"{v:.{decimals}f} %"


def fmt_oi_delta(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    v = normalize_zero(float(value), decimals=decimals)
    sign = "+" if v > 0 else ""
    if v == 0:
        return f"{v:.{decimals}f}"
    return f"{sign}{v:.{decimals}f}"


def fmt_price(value: float | None, *, decimals: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.{decimals}f}"


def fmt_mio_usd(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    v = float(value)
    sign = "+" if v >= 0 else ""
    return f"{sign}{v / 1e6:.{decimals}f} Mio. USD"


def fmt_ts_display(ts: str | None) -> str:
    if not ts:
        return "n/a"
    return ts.replace("T", " ").replace("Z", " UTC")


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value
