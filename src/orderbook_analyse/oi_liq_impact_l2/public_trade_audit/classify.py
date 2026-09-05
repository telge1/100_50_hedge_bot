"""Impact compression classification from precomputed F3 metrics."""

from __future__ import annotations

import math
from typing import Any, Mapping

import pandas as pd

from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.constants import (
    CATEGORY_FALLING_FLOW_LOW_IMPACT,
    CATEGORY_FALLING_FLOW_NO_COMPRESSION,
    CATEGORY_INVALID_OR_ZERO_FLOW,
    CATEGORY_SUSTAINED_FLOW_COMPRESSION,
    CATEGORY_SUSTAINED_FLOW_NO_COMPRESSION,
    WINDOW_COMPARISONS,
)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _bool_value(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if math.isnan(value):
            return None
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def safe_ratio(num: float | None, den: float | None) -> float | None:
    if num is None or den is None or den == 0:
        return None
    out = num / den
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def classify_window(
    *,
    first_notional: float | None,
    last_notional: float | None,
    first_impact: float | None,
    last_impact: float | None,
    first_trades_present: bool | None,
    last_trades_present: bool | None,
    data_abort: bool,
) -> str:
    if data_abort:
        return CATEGORY_INVALID_OR_ZERO_FLOW
    if first_trades_present is not True or last_trades_present is not True:
        return CATEGORY_INVALID_OR_ZERO_FLOW
    if (
        first_notional is None
        or last_notional is None
        or first_impact is None
        or last_impact is None
    ):
        return CATEGORY_INVALID_OR_ZERO_FLOW
    if first_notional <= 0:
        return CATEGORY_INVALID_OR_ZERO_FLOW

    if last_impact < first_impact:
        if last_notional >= first_notional:
            return CATEGORY_SUSTAINED_FLOW_COMPRESSION
        return CATEGORY_FALLING_FLOW_LOW_IMPACT
    if last_notional >= first_notional:
        return CATEGORY_SUSTAINED_FLOW_NO_COMPRESSION
    return CATEGORY_FALLING_FLOW_NO_COMPRESSION


def classify_row(
    row: Mapping[str, Any],
    comparison_pair: str,
    first_prefix: str,
    last_prefix: str,
) -> dict[str, Any]:
    data_abort = _bool_value(row.get("data_abort")) is True
    first_notional = _number(row.get(f"{first_prefix}_aggressive_notional"))
    last_notional = _number(row.get(f"{last_prefix}_aggressive_notional"))
    first_impact = _number(row.get(f"{first_prefix}_impact_per_notional"))
    last_impact = _number(row.get(f"{last_prefix}_impact_per_notional"))
    first_trades_present = _bool_value(row.get(f"{first_prefix}_trades_present"))
    last_trades_present = _bool_value(row.get(f"{last_prefix}_trades_present"))

    category = classify_window(
        first_notional=first_notional,
        last_notional=last_notional,
        first_impact=first_impact,
        last_impact=last_impact,
        first_trades_present=first_trades_present,
        last_trades_present=last_trades_present,
        data_abort=data_abort,
    )
    trades_present = (
        first_trades_present is True and last_trades_present is True and not data_abort
    )
    return {
        "cluster_id": row.get("cluster_id"),
        "direction": row.get("direction"),
        "comparison_pair": comparison_pair,
        "category": category,
        "data_abort": data_abort,
        "trades_present": trades_present,
        "first_aggressive_notional": first_notional,
        "last_aggressive_notional": last_notional,
        "notional_ratio_last_over_first": safe_ratio(last_notional, first_notional),
        "first_impact_per_notional": first_impact,
        "last_impact_per_notional": last_impact,
        "impact_ratio_last_over_first": safe_ratio(last_impact, first_impact),
    }


def classify_impact_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        for comparison_pair, first_prefix, last_prefix in WINDOW_COMPARISONS:
            rows.append(
                classify_row(
                    row,
                    comparison_pair,
                    first_prefix,
                    last_prefix,
                )
            )
    return rows
