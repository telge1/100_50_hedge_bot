"""Never write unit-test data to production ClickHouse."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_live_shadow_ch_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests use MockChClient or explicit opt-in; default is CH sync off."""
    monkeypatch.setenv("SHADOW_CH_SYNC_ENABLED", "0")
