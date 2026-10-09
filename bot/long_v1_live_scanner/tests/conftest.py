"""Long live scanner test hooks."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "slow: full-symbol replay (ClickHouse / market data); skip with -m 'not slow'",
    )
