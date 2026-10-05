"""Historical parity vs frozen oracle / cross-section trades."""

from __future__ import annotations

import pytest

from bot.long_v1_live_scanner.config import PARITY_COINS
from bot.long_v1_live_scanner.parity import JUN_FROM, JUN_JUL_TO, compare_parity, verify_frozen_integrity


@pytest.mark.parametrize("symbol", PARITY_COINS)
def test_jun_jul_allowed_parity(symbol: str) -> None:
    r = compare_parity(symbol, JUN_FROM, JUN_JUL_TO)
    assert r["parity"], r


def test_frozen_integrity() -> None:
    ok, lines = verify_frozen_integrity()
    assert ok, "\n".join(lines)
