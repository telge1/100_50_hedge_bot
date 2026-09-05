"""Tests for closed confirmation bar pool availability fix."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools import pool_from_engine, pool_valid_at
from orderbook_analyse.liquidity_location_causal.prefix import candles_1m_closed_until
from orderbook_analyse.liquidity_location_pool_causality_audit_v1.runner import compute_prefix_state


class _FakePool:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _fake_pool(*, confirm_open: datetime, tf: str = "15m", source_open: datetime | None = None):
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import _timeframe_minutes

    src = source_open or (confirm_open - timedelta(minutes=_timeframe_minutes(tf)))
    minutes = _timeframe_minutes(tf)
    confirm_end = confirm_open + timedelta(minutes=minutes)
    meta = {
        "confirmation_bar_start": confirm_open.isoformat(),
        "confirmation_bar_end": confirm_end.isoformat(),
        "available_at": confirm_end.isoformat(),
        "known_at": confirm_end.isoformat(),
        "max_feature_timestamp": confirm_end.isoformat(),
        "source_bar_start": src.isoformat(),
        "source_bar_end": (src + timedelta(minutes=minutes)).isoformat(),
    }
    p = _FakePool(
        pool_id=f"lld:DOGEUSDT:{tf}:upper:{int(src.timestamp())}",
        symbol="DOGEUSDT",
        timeframe=tf,
        side="upper",
        bottom_price=0.088,
        top_price=0.0883,
        strength=1.0,
        created_timestamp=confirm_open,
        source_timestamp=src,
        invalidated_timestamp=None,
        active=True,
        metadata=meta,
    )
    return p, confirm_end


def test_known_at_equals_confirmation_bar_close():
    confirm_open = datetime(2026, 8, 28, 3, 30)
    p, confirm_end = _fake_pool(confirm_open=confirm_open)
    pr = pool_from_engine(p)
    assert pr.known_at == confirm_end
    assert pr.available_at == confirm_end
    assert pr.known_at == pr.available_at


def test_not_available_before_confirm_close():
    confirm_open = datetime(2026, 8, 28, 3, 30)
    p, confirm_end = _fake_pool(confirm_open=confirm_open)
    pr = pool_from_engine(p)
    assert not pool_valid_at(pr, confirm_open)
    assert not pool_valid_at(pr, confirm_end - timedelta(seconds=1))
    assert pool_valid_at(pr, confirm_end)


def test_candles_1m_closed_until():
    df = pd.DataFrame(
        {
            "open_time": pd.to_datetime(["2026-08-28 03:44:00", "2026-08-28 03:45:00"]),
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1.0, 1.0],
        }
    )
    out = candles_1m_closed_until(df, "2026-08-28 03:45:00")
    assert len(out) == 1


@pytest.mark.integration
def test_doge_short_entry_boundary():
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import default_client, fetch_candles_1m
    from orderbook_analyse.liquidity_location_pool_causality_audit_v1.config import AUDIT_END, WARMUP_START

    pid = "lld:DOGEUSDT:15m:upper:1787886900"
    df = fetch_candles_1m(default_client(), "DOGEUSDT", WARMUP_START, AUDIT_END)
    for ts, expect in [("2026-08-28 03:44:59", False), ("2026-08-28 03:45:00", True)]:
        st = compute_prefix_state(df, ts, mode="causal_prefix", timeframes=("15m",))
        active = pid in {r["pool_id"] for r in st["active_rows"]}
        assert active == expect, ts
