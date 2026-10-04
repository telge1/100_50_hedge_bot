"""Historical parity vs frozen oracle."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bot.e1r_live_scanner.config import PARITY_COINS
from bot.e1r_live_scanner.parity import compare_parity, verify_frozen_integrity

JUNE_FROM = datetime(2026, 6, 1, tzinfo=timezone.utc)
JUNE_TO = datetime(2026, 6, 30, 23, 59, 59, tzinfo=timezone.utc)
JUN_JUL_TO = datetime(2026, 7, 31, 23, 59, 59, tzinfo=timezone.utc)


@pytest.mark.parametrize("symbol", PARITY_COINS)
def test_june_baseline_and_e1r_parity(symbol: str) -> None:
    r = compare_parity(symbol, JUNE_FROM, JUNE_TO)
    assert r["baseline_parity"], r
    assert r["e1r_parity"], r


@pytest.mark.parametrize("symbol", PARITY_COINS)
def test_jun_jul_baseline_and_e1r_parity(symbol: str) -> None:
    r = compare_parity(symbol, JUNE_FROM, JUN_JUL_TO)
    assert r["baseline_parity"], r
    assert r["e1r_parity"], r


def test_frozen_integrity() -> None:
    ok, lines = verify_frozen_integrity()
    assert ok, "\n".join(lines)
