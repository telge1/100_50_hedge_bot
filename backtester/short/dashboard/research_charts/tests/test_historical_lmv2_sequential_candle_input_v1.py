"""Phase 6E — sequential 15m candle LMV2 input tests."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from research_charts.historical_pool_state_v1 import HistoricalPoolStateError
from research_charts.nested_ask_pool_backtester import (
    run_historical_lmv2_sequential_candle_input_dry_run,
)
from research_charts.oa_import import ensure_oa_on_path

PILOT_FIRST = "2026-09-01T11:30:00Z"
PILOT_LAST = "2026-09-01T12:30:00Z"
DAY_FIRST = "2026-09-01T00:00:00Z"
DAY_LAST = "2026-09-01T23:45:00Z"


@pytest.fixture
def ch_client():
    from research_charts.clickhouse_config import load_clickhouse_config

    import clickhouse_connect

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    try:
        yield client, cfg.database
    finally:
        client.close()


def test_decision_close_time_mapping_unit():
    ensure_oa_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_sequential_candle_input import (
        decision_close_from_15m_open,
        open_time_from_decision_close,
        parse_decision_close,
    )

    dc = parse_decision_close("2026-09-01T12:00:00Z")
    ot = open_time_from_decision_close(dc)
    assert decision_close_from_15m_open(ot) == dc
    assert ot == datetime(2026, 9, 1, 11, 45, tzinfo=timezone.utc)


def test_validate_monotonic_rejects_duplicate_and_gap():
    ensure_oa_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_sequential_candle_input import (
        SequentialCandleInputError,
        validate_monotonic_candle_series,
    )

    ok = [
        {"decision_close": "2026-09-01T11:30:00Z"},
        {"decision_close": "2026-09-01T11:45:00Z"},
    ]
    validate_monotonic_candle_series(ok)
    with pytest.raises(SequentialCandleInputError) as exc:
        validate_monotonic_candle_series(
            ok + [{"decision_close": "2026-09-01T11:45:00Z"}]
        )
    assert exc.value.code == "DUPLICATE_DECISION_CLOSE"
    with pytest.raises(SequentialCandleInputError) as exc2:
        validate_monotonic_candle_series(
            [
                {"decision_close": "2026-09-01T11:30:00Z"},
                {"decision_close": "2026-09-01T12:00:00Z"},
            ]
        )
    assert exc2.value.code == "CANDLE_GAP_OR_ORDER"


def test_invalid_pd_rejected():
    with pytest.raises(Exception) as exc:
        run_historical_lmv2_sequential_candle_input_dry_run(
            first_decision_close=PILOT_FIRST,
            last_decision_close=PILOT_LAST,
            pool_detail=8,
        )
    assert "INVALID_HISTORICAL_PD" in str(exc.value) or getattr(exc.value, "code", "") == "INVALID_HISTORICAL_PD"


@pytest.mark.integration
def test_pilot_window_pd10(ch_client):
    client, database = ch_client
    with patch(
        "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.backtest.run_backtest"
    ) as mock_bt:
        with patch(
            "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools.load_engine_pools_at"
        ) as mock_pools:
            out = run_historical_lmv2_sequential_candle_input_dry_run(
                first_decision_close=PILOT_FIRST,
                last_decision_close=PILOT_LAST,
                pool_detail=10,
                client=client,
                database=database,
                ch_client=client,
            )
            mock_bt.assert_not_called()
            mock_pools.assert_not_called()
    assert out["verdict"] == "PASS"
    assert out["candle_count"] == 5
    assert out["consumer_entrypoint"].endswith("run_sequential_lmv2_candle_input_dry_run")


@pytest.mark.integration
def test_pilot_window_pd1_pd5(ch_client):
    client, database = ch_client
    for pd in (1, 5):
        out = run_historical_lmv2_sequential_candle_input_dry_run(
            first_decision_close=PILOT_FIRST,
            last_decision_close=PILOT_LAST,
            pool_detail=pd,
            client=client,
            database=database,
            ch_client=client,
        )
        assert out["verdict"] == "PASS"


@pytest.mark.integration
def test_one_day_96_closes_pd10(ch_client):
    client, database = ch_client
    out = run_historical_lmv2_sequential_candle_input_dry_run(
        first_decision_close=DAY_FIRST,
        last_decision_close=DAY_LAST,
        pool_detail=10,
        client=client,
        database=database,
        ch_client=client,
    )
    assert out["candle_count"] == 96
    assert out["verdict"] == "PASS"


@pytest.mark.integration
def test_no_candle_data_fail_closed(ch_client):
    client, database = ch_client
    ensure_oa_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_sequential_candle_input import (
        SequentialCandleInputError,
    )

    with pytest.raises(SequentialCandleInputError) as exc:
        run_historical_lmv2_sequential_candle_input_dry_run(
            first_decision_close="2030-01-01T00:00:00Z",
            last_decision_close="2030-01-01T00:15:00Z",
            pool_detail=10,
            client=client,
            database=database,
            ch_client=client,
        )
    assert exc.value.code == "NO_CANDLE_DATA"


def test_missing_publication_fail_closed():
    with pytest.raises(HistoricalPoolStateError) as exc:
        from research_charts.nested_ask_pool_backtester import run_historical_lmv2_input_dry_run

        run_historical_lmv2_input_dry_run(
            decision_close="2030-01-01T12:00:00Z",
            pool_detail=10,
        )
    assert exc.value.code == "MISSING_PUBLICATION"
