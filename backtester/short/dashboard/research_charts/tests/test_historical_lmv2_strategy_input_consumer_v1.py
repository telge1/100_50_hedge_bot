"""Phase 6F — LMV2 strategy input consumer tests."""

from __future__ import annotations

import copy
from unittest.mock import patch

import pytest

from research_charts.nested_ask_pool_backtester import (
    run_historical_lmv2_sequential_candle_input_dry_run,
    run_historical_lmv2_sequential_strategy_input_dry_run,
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


def test_consumer_unit_rejects_invalid_pd():
    ensure_oa_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_strategy_input_consumer_v1 import (
        StrategyInputConsumerError,
        consume_historical_lmv2_strategy_input_v1,
    )

    state = {
        "bridge_version": "x",
        "data_kind": "historical_lmv2_active_publication_state",
        "availability_class": "RESEARCH_HISTORICAL",
        "scanner_pool_equivalence": "NOT_CLAIMED",
        "symbol": "DOGEUSDT",
        "timeframe": "15m",
        "pool_detail": 10,
        "pipeline_line_id": "l",
        "config_hash": "c",
        "decision_close": "2026-09-01T12:00:00Z",
        "snapshot_id": "s",
        "active_state_fingerprint": "f",
        "members_hash": "m",
        "input_state_hash": "h",
        "active_members": [],
        "active_member_count": 0,
    }
    candle = {
        "candle_open_utc": "2026-09-01T11:45:00Z",
        "decision_close": "2026-09-01T12:00:00Z",
    }
    with pytest.raises(StrategyInputConsumerError) as exc:
        consume_historical_lmv2_strategy_input_v1(candle=candle, lmv2_state=state, pool_detail=7)
    assert exc.value.code == "INVALID_HISTORICAL_PD"


def test_consumer_unit_causality():
    ensure_oa_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_strategy_input_consumer_v1 import (
        StrategyInputConsumerError,
        consume_historical_lmv2_strategy_input_v1,
    )

    state = {
        "bridge_version": "x",
        "data_kind": "historical_lmv2_active_publication_state",
        "availability_class": "RESEARCH_HISTORICAL",
        "scanner_pool_equivalence": "NOT_CLAIMED",
        "symbol": "DOGEUSDT",
        "timeframe": "15m",
        "pool_detail": 10,
        "pipeline_line_id": "l",
        "config_hash": "c",
        "decision_close": "2026-09-01T12:00:00Z",
        "snapshot_id": "s",
        "active_state_fingerprint": "f",
        "members_hash": "m",
        "input_state_hash": "h",
        "active_members": [{"pool_id": "p", "known_at": "2026-09-02T00:00:00Z"}],
        "active_member_count": 1,
    }
    candle = {"candle_open_utc": "2026-09-01T11:45:00Z", "decision_close": "2026-09-01T12:00:00Z"}
    with pytest.raises(StrategyInputConsumerError) as exc:
        consume_historical_lmv2_strategy_input_v1(candle=candle, lmv2_state=state, pool_detail=10)
    assert exc.value.code == "CAUSALITY_VIOLATION"


def test_mutating_consumer_fails_sequential_loop(ch_client):
    client, database = ch_client
    ensure_oa_on_path()

    def bad_consumer(*, candle, lmv2_state, pool_detail):
        lmv2_state["members_hash"] = "mutated"
        return {"receipt": {"members_hash": "mutated"}}

    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_sequential_candle_input import (
        run_sequential_lmv2_candle_input_dry_run,
    )
    from research_charts.historical_lmv2_backtester_input_bridge_v1 import (
        load_historical_lmv2_backtester_input_at_close,
    )
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    reader = PublishedActiveReader()

    def loader(dc, *, pool_detail: int, symbol: str, timeframe: str, **_):
        return load_historical_lmv2_backtester_input_at_close(
            dc,
            pool_detail=pool_detail,
            symbol=symbol,
            timeframe=timeframe,
            client=client,
            database=database,
            reader=reader,
        ).to_dict()

    out = run_sequential_lmv2_candle_input_dry_run(
        first_decision_close=PILOT_FIRST,
        last_decision_close=PILOT_LAST,
        pool_detail=10,
        loader=loader,
        client=client,
        strategy_input_consumer=bad_consumer,
    )
    assert out["verdict"] == "FAIL"


@pytest.mark.integration
def test_phase6e_path_unchanged_without_consumer(ch_client):
    client, database = ch_client
    base = run_historical_lmv2_sequential_candle_input_dry_run(
        first_decision_close=PILOT_FIRST,
        last_decision_close=PILOT_LAST,
        pool_detail=10,
        client=client,
        database=database,
        ch_client=client,
    )
    assert "strategy_input_consumer_enabled" not in base
    assert base["verdict"] == "PASS"


@pytest.mark.integration
def test_pilot_short_pd10_exactly_once(ch_client):
    client, database = ch_client
    with patch(
        "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.backtest.run_backtest"
    ) as mock_bt:
        out = run_historical_lmv2_sequential_strategy_input_dry_run(
            first_decision_close=PILOT_FIRST,
            last_decision_close=PILOT_LAST,
            pool_detail=10,
            client=client,
            database=database,
            ch_client=client,
        )
        mock_bt.assert_not_called()
    assert out["verdict"] == "PASS"
    assert out["consumer_receipt_count"] == 5
    assert out["exactly_once_consumption"] is True


@pytest.mark.integration
def test_pilot_short_pd1_pd5(ch_client):
    client, database = ch_client
    for pd in (1, 5):
        out = run_historical_lmv2_sequential_strategy_input_dry_run(
            first_decision_close=PILOT_FIRST,
            last_decision_close=PILOT_LAST,
            pool_detail=pd,
            client=client,
            database=database,
            ch_client=client,
        )
        assert out["verdict"] == "PASS"
        assert out["consumer_receipt_count"] == 5


@pytest.mark.integration
def test_day96_pd10(ch_client):
    client, database = ch_client
    out = run_historical_lmv2_sequential_strategy_input_dry_run(
        first_decision_close=DAY_FIRST,
        last_decision_close=DAY_LAST,
        pool_detail=10,
        client=client,
        database=database,
        ch_client=client,
    )
    assert out["candle_count"] == 96
    assert out["consumer_receipt_count"] == 96
    assert out["verdict"] == "PASS"
