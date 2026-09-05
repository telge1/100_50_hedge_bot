"""Unit tests for LLD pool causality audit helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from orderbook_analyse.liquidity_location_pool_causality_audit_v1.prefix_engine import (
    candles_1m_until,
    confirmation_bar_end,
    utc_naive,
)
from orderbook_analyse.liquidity_location_pool_causality_audit_v1.future_ops import (
    LOOKAHEAD,
    build_future_operator_audit,
)


def test_candles_1m_until_excludes_open_bar():
    df = pd.DataFrame(
        {
            "open_time": pd.to_datetime(
                ["2026-08-28 03:28:00", "2026-08-28 03:29:00", "2026-08-28 03:30:00"]
            ),
            "open": [1.0, 1.0, 1.0],
            "high": [1.0, 1.0, 1.0],
            "low": [1.0, 1.0, 1.0],
            "close": [1.0, 1.0, 1.0],
            "volume": [1.0, 1.0, 1.0],
        }
    )
    # as_of 03:30 → bar 03:29 closed at 03:30 included; 03:30 open not closed
    out = candles_1m_until(df, "2026-08-28 03:30:00")
    assert list(out["open_time"].astype(str)) == ["2026-08-28 03:28:00", "2026-08-28 03:29:00"]


def test_confirmation_bar_end_15m():
    end = confirmation_bar_end(datetime(2026, 8, 28, 3, 30, tzinfo=timezone.utc), "15m")
    assert utc_naive(end) == utc_naive("2026-08-28 03:45:00")


def test_future_ops_flags_scanner_lookahead():
    rows = build_future_operator_audit()
    locs = [r["location"] for r in rows if r["classification"] == LOOKAHEAD]
    assert any("load_pools_at" in x for x in locs)
    assert any("known_at" in x for x in locs)


@pytest.mark.integration
def test_doge_ref_pool_causal_prefix_only():
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import (
        default_client,
        fetch_candles_1m,
    )
    from orderbook_analyse.liquidity_location_pool_causality_audit_v1.config import (
        AUDIT_END,
        WARMUP_START,
    )
    from orderbook_analyse.liquidity_location_pool_causality_audit_v1.runner import (
        compute_prefix_state,
    )

    pid = "lld:DOGEUSDT:15m:upper:1787886900"
    client = default_client()
    df = fetch_candles_1m(client, "DOGEUSDT", WARMUP_START, AUDIT_END)
    causal_330 = compute_prefix_state(df, "2026-08-28 03:30:00", mode="causal_prefix", timeframes=("15m",))
    causal_345 = compute_prefix_state(df, "2026-08-28 03:45:00", mode="causal_prefix", timeframes=("15m",))
    ids_c330 = {r["pool_id"] for r in causal_330["active_rows"]}
    ids_c345 = {r["pool_id"] for r in causal_345["active_rows"]}
    assert pid not in ids_c330
    assert pid in ids_c345
