"""Coverage → decision_hint for public-trade file source."""

from __future__ import annotations

from orderbook_analyse.public_trade_source.protocol import TradeCoverageReport

READY = "PUBLIC_TRADE_FILE_SOURCE_READY"
READY_WITH_GAP = "PUBLIC_TRADE_FILE_SOURCE_READY_WITH_COVERAGE_GAP"
NOT_READY = "PUBLIC_TRADE_FILE_SOURCE_NOT_READY"


def decision_hint_from_coverage(
    report: TradeCoverageReport,
    *,
    allow_partial: bool = False,
) -> str:
    """Derive decision solely from the coverage report (never a hardcoded gap)."""
    incomplete = bool(report.missing_dates) or bool(report.partial)
    if report.valid and not incomplete:
        return READY
    if incomplete and allow_partial:
        return READY_WITH_GAP
    return NOT_READY
