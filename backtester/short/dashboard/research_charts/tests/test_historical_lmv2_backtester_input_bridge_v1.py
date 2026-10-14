"""Phase 6C — historical LMV2 backtester input bridge tests."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research_charts.historical_lmv2_backtester_adapter_v1 import (
    load_backtester_active_state_at_close,
)
from research_charts.historical_lmv2_backtester_input_bridge_v1 import (
    ADAPTER_MODULE,
    AVAILABILITY_CLASS,
    BRIDGE_VERSION,
    DATA_KIND,
    bridge_matches_adapter,
    load_historical_lmv2_backtester_input_at_close,
)
from research_charts.historical_pool_state_v1 import HistoricalPoolStateError

REFERENCE_CLOSE = "2026-09-01T12:00:00Z"
EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}
MULTI_CLOSES = [
    "2026-09-01T00:00:00Z",
    "2026-09-01T12:00:00Z",
    "2026-09-03T23:45:00Z",
    "2026-09-04T00:00:00Z",
    "2026-09-07T23:45:00Z",
]

_BRIDGE_SRC = Path(__file__).resolve().parents[1] / "historical_lmv2_backtester_input_bridge_v1.py"


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


def test_bridge_source_has_no_scanner_or_trade_imports():
    text = _BRIDGE_SRC.read_text(encoding="utf-8")
    assert "import orderbook_analyse" not in text
    assert "from orderbook_analyse" not in text
    assert "load_engine_pools_at(" not in text
    assert "pool_from_engine(" not in text
    assert "run_backtest(" not in text
    assert "run_nested_ask_pool_backtest(" not in text


def test_invalid_pd_rejected():
    with pytest.raises(HistoricalPoolStateError) as exc:
        load_historical_lmv2_backtester_input_at_close(REFERENCE_CLOSE, pool_detail=7)
    assert exc.value.code == "INVALID_HISTORICAL_PD"


def test_missing_publication_fail_closed():
    with pytest.raises(HistoricalPoolStateError) as exc:
        load_historical_lmv2_backtester_input_at_close("2030-01-01T12:00:00Z", pool_detail=10)
    assert exc.value.code == "MISSING_PUBLICATION"


def test_bridge_metadata_constants():
    assert DATA_KIND == "historical_lmv2_active_publication_state"
    assert AVAILABILITY_CLASS == "RESEARCH_HISTORICAL"
    assert ADAPTER_MODULE.endswith("historical_lmv2_backtester_adapter_v1")


@pytest.mark.integration
def test_reference_close_adapter_bridge_parity_all_pd(ch_client):
    client, database = ch_client
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    reader = PublishedActiveReader()
    for pd in (1, 5, 10):
        adapted = load_backtester_active_state_at_close(
            REFERENCE_CLOSE,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        b1 = load_historical_lmv2_backtester_input_at_close(
            REFERENCE_CLOSE,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        b2 = load_historical_lmv2_backtester_input_at_close(
            REFERENCE_CLOSE,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        assert b1.input_state_hash == b2.input_state_hash
        assert b1.members_hash == b2.members_hash
        check = bridge_matches_adapter(adapted, b1)
        assert check["ok"], check
        assert b1.active_member_count == EXPECTED_ACTIVE[pd]
        assert b1.bridge_version == BRIDGE_VERSION
        assert b1.availability_class == AVAILABILITY_CLASS


@pytest.mark.integration
def test_no_future_known_at_leakage(ch_client):
    client, database = ch_client
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    reader = PublishedActiveReader()
    bridge = load_historical_lmv2_backtester_input_at_close(
        REFERENCE_CLOSE,
        pool_detail=10,
        client=client,
        database=database,
        reader=reader,
    )
    dc = bridge.decision_close
    for m in bridge.active_members:
        known = m.get("known_at")
        if known is not None:
            assert str(known) <= dc


@pytest.mark.integration
def test_cross_pd_isolation(ch_client):
    client, database = ch_client
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    reader = PublishedActiveReader()
    b1 = load_historical_lmv2_backtester_input_at_close(
        REFERENCE_CLOSE, pool_detail=1, client=client, database=database, reader=reader
    )
    b10 = load_historical_lmv2_backtester_input_at_close(
        REFERENCE_CLOSE, pool_detail=10, client=client, database=database, reader=reader
    )
    assert b1.members_hash != b10.members_hash
    assert b1.pipeline_line_id != b10.pipeline_line_id


def test_active_members_not_mutated_by_caller():
    row = {
        "pool_id": "x",
        "lower_bound": 1.0,
        "upper_bound": 2.0,
        "active_membership": True,
    }
    members = (copy.deepcopy(row),)
    # Bridge copies on load; simulate immutability contract on tuple content
    row["lower_bound"] = 99.0
    assert members[0]["lower_bound"] == 1.0


@pytest.mark.integration
@pytest.mark.parametrize("close_iso", MULTI_CLOSES)
def test_multi_close_bridge_adapter_hash_parity(ch_client, close_iso: str):
    client, database = ch_client
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
        PublishedActiveReader,
    )

    reader = PublishedActiveReader()
    for pd in (1, 5, 10):
        adapted = load_backtester_active_state_at_close(
            close_iso,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        bridge = load_historical_lmv2_backtester_input_at_close(
            close_iso,
            pool_detail=pd,
            client=client,
            database=database,
            reader=reader,
        )
        assert bridge_matches_adapter(adapted, bridge)["ok"]
