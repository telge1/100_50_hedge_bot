"""Source-coverage assessment for public-trade last-price carry."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from .public_trade_index import PublicTradeIndex

SOURCE_COVERAGE_CONFIRMED = "SOURCE_COVERAGE_CONFIRMED"
SOURCE_GAP_CONFIRMED = "SOURCE_GAP_CONFIRMED"
SOURCE_COVERAGE_UNKNOWN = "SOURCE_COVERAGE_UNKNOWN"


def _parse_meta_ts(value: Any) -> pd.Timestamp | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts


@dataclass(frozen=True)
class SourceCoverageDecision:
    status: str
    reason: str

    @property
    def confirmed(self) -> bool:
        return self.status == SOURCE_COVERAGE_CONFIRMED


def assess_source_coverage(
    *,
    target_ts: pd.Timestamp,
    last_trade_ts: pd.Timestamp | None,
    trade_index: PublicTradeIndex,
    load_meta: dict[str, Any] | None,
) -> SourceCoverageDecision:
    """Decide whether the PT source was available through ``target_ts``.

    Empty seconds / quiet markets are not gaps when the load window and CH tip
    cover ``target_ts``. Unknown tip + target past last loaded trade → UNKNOWN.
    """
    target_ts = pd.Timestamp(target_ts).tz_convert("UTC")
    meta = load_meta or {}
    load_start = _parse_meta_ts(meta.get("load_start"))
    load_end = _parse_meta_ts(meta.get("load_end_exclusive"))
    tip = _parse_meta_ts(meta.get("ch_tip_trade_ts"))

    if not trade_index.trades:
        return SourceCoverageDecision(SOURCE_COVERAGE_UNKNOWN, "empty_trade_index")

    if load_start is not None and target_ts < load_start:
        return SourceCoverageDecision(SOURCE_COVERAGE_UNKNOWN, "target_before_load_start")

    if load_end is not None and target_ts >= load_end:
        return SourceCoverageDecision(SOURCE_COVERAGE_UNKNOWN, "target_outside_loaded_window")

    # Tip after target: historical/live stream has progressed past target → source was writable.
    if tip is not None and tip >= target_ts:
        return SourceCoverageDecision(SOURCE_COVERAGE_CONFIRMED, "ch_tip_covers_target")

    # Trades at/after target in index.
    if trade_index.last_trade_ts is not None and trade_index.last_trade_ts >= target_ts:
        return SourceCoverageDecision(SOURCE_COVERAGE_CONFIRMED, "index_has_trades_at_or_after_target")

    # Quiet after last print but load window extends past target.
    if (
        last_trade_ts is not None
        and load_end is not None
        and last_trade_ts < target_ts < load_end
    ):
        if tip is None:
            # Loaded window proves query coverage; tip missing → still treat as confirmed
            # for closed historical loads that returned rows spanning the window.
            if trade_index.first_trade_ts is not None and trade_index.first_trade_ts <= last_trade_ts:
                return SourceCoverageDecision(
                    SOURCE_COVERAGE_CONFIRMED,
                    "quiet_inside_loaded_window_no_tip",
                )
            return SourceCoverageDecision(SOURCE_COVERAGE_UNKNOWN, "quiet_without_tip")
        if tip < target_ts:
            return SourceCoverageDecision(SOURCE_GAP_CONFIRMED, "tip_before_target_after_last_trade")
        return SourceCoverageDecision(SOURCE_COVERAGE_CONFIRMED, "quiet_inside_loaded_window")

    if tip is not None and tip < target_ts:
        return SourceCoverageDecision(SOURCE_GAP_CONFIRMED, "ch_tip_before_target")

    return SourceCoverageDecision(SOURCE_COVERAGE_UNKNOWN, "insufficient_coverage_evidence")


def age_quality_flags(age_ms: float) -> tuple[list[str], str]:
    """Return (cumulative flags, exclusive class). Age bands are quality-only."""
    flags: list[str] = []
    if age_ms > 1000:
        flags.append("PRICE_AGE_GT_1S")
    if age_ms > 5000:
        flags.append("PRICE_AGE_GT_5S")
    if age_ms > 10000:
        flags.append("PRICE_AGE_GT_10S")
    if age_ms > 30000:
        flags.append("PRICE_AGE_GT_30S")
    if age_ms <= 1000:
        cls = "AGE_LE_1S"
    elif age_ms <= 5000:
        cls = "AGE_GT_1S_LE_5S"
    elif age_ms <= 10000:
        cls = "AGE_GT_5S_LE_10S"
    elif age_ms <= 30000:
        cls = "AGE_GT_10S_LE_30S"
    elif age_ms <= 60000:
        cls = "AGE_GT_30S_LE_60S"
    else:
        cls = "AGE_GT_60S"
    return flags, cls
