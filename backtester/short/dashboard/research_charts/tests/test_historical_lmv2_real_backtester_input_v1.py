"""Phase 6D — real nested-ask backtester LMV2 input path (no trades)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from research_charts.historical_lmv2_backtester_adapter_v1 import (
    active_members_hash,
    load_backtester_active_state_at_close,
)
from research_charts.historical_lmv2_backtester_input_bridge_v1 import (
    load_historical_lmv2_backtester_input_at_close,
)
from research_charts.historical_pool_state_v1 import HistoricalPoolStateError
from research_charts.nested_ask_pool_backtester import (
    run_historical_lmv2_input_dry_run,
    run_nested_ask_pool_backtest,
)
from research_charts.oa_import import ensure_oa_on_path

REFERENCE_CLOSE = "2026-09-01T12:00:00Z"
EXPECTED_ACTIVE = {1: 167, 5: 179, 10: 209}
MULTI_CLOSES = [
    "2026-09-01T00:00:00Z",
    "2026-09-01T12:00:00Z",
    "2026-09-03T23:45:00Z",
    "2026-09-04T00:00:00Z",
    "2026-09-07T23:45:00Z",
]


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


def test_legacy_run_nested_ask_still_calls_engine_backtest(tmp_path):
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    with patch(
        "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.research_entry.run_backtest"
    ) as mock_bt:
        mock_bt.return_value = {
            "run_id": 1,
            "out_dir": str(run_dir),
            "summary": {"coverage": {"DOGEUSDT": {"status": "OK"}}},
        }
        with patch(
            "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.research_entry.build_overlay_payload_from_run_dir"
        ) as mock_ov:
            mock_ov.return_value = {"specs": []}
            with patch("research_charts.nested_ask_pool_backtester.RESULTS_ROOT", tmp_path):
                run_nested_ask_pool_backtest(
                    symbol="DOGEUSDT",
                    start="2026-09-01T00:00:00Z",
                    end="2026-09-01T12:00:00Z",
                )
        assert mock_bt.call_count == 1


def test_dry_run_does_not_call_run_backtest(ch_client):
    client, database = ch_client
    ensure_oa_on_path()
    with patch(
        "orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.backtest.run_backtest"
    ) as mock_bt:
        with patch(
            "orderbook_analyse.a_plus_liquidity_pool_signal_scanner_v1.pools.load_engine_pools_at"
        ) as mock_pools:
            out = run_historical_lmv2_input_dry_run(
                decision_close=REFERENCE_CLOSE,
                pool_detail=10,
                client=client,
                database=database,
            )
            mock_bt.assert_not_called()
            mock_pools.assert_not_called()
    assert out["mode"] == "input_only"
    assert out["integrity"]["engine_pools_loaded"] is False
    assert out["integrity"]["trading_executed"] is False


def test_invalid_pd_rejected():
    ensure_oa_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_input_dry_run import (
        HistoricalLmv2InputDryRunError,
    )

    with pytest.raises(HistoricalLmv2InputDryRunError) as exc:
        run_historical_lmv2_input_dry_run(
            decision_close=REFERENCE_CLOSE,
            pool_detail=99,
        )
    assert exc.value.code == "INVALID_HISTORICAL_PD"


def test_missing_publication_fail_closed():
    with pytest.raises(HistoricalPoolStateError) as exc:
        run_historical_lmv2_input_dry_run(
            decision_close="2030-01-01T12:00:00Z",
            pool_detail=10,
        )
    assert exc.value.code == "MISSING_PUBLICATION"


@pytest.mark.integration
def test_reference_close_real_entry_parity_all_pd(ch_client):
    client, database = ch_client
    for pd in (1, 5, 10):
        bridge = load_historical_lmv2_backtester_input_at_close(
            REFERENCE_CLOSE,
            pool_detail=pd,
            client=client,
            database=database,
        )
        real = run_historical_lmv2_input_dry_run(
            decision_close=REFERENCE_CLOSE,
            pool_detail=pd,
            client=client,
            database=database,
        )
        st = real["lmv2_state"]
        assert real["entrypoint"].endswith("run_historical_lmv2_input_dry_run")
        assert st["members_hash"] == bridge.members_hash
        assert st["input_state_hash"] == bridge.input_state_hash
        assert st["snapshot_id"] == bridge.snapshot_id
        assert list(st["active_members"]) == list(bridge.active_members)
        assert real["integrity"]["active_member_count"] == EXPECTED_ACTIVE[pd]


@pytest.mark.integration
def test_cross_pd_isolation_real_entry(ch_client):
    client, database = ch_client
    r1 = run_historical_lmv2_input_dry_run(
        decision_close=REFERENCE_CLOSE,
        pool_detail=1,
        client=client,
        database=database,
    )
    r10 = run_historical_lmv2_input_dry_run(
        decision_close=REFERENCE_CLOSE,
        pool_detail=10,
        client=client,
        database=database,
    )
    assert r1["lmv2_state"]["members_hash"] != r10["lmv2_state"]["members_hash"]


@pytest.mark.integration
def test_handoff_hash_integrity(ch_client):
    client, database = ch_client
    real = run_historical_lmv2_input_dry_run(
        decision_close=REFERENCE_CLOSE,
        pool_detail=1,
        client=client,
        database=database,
    )
    integrity = real["integrity"]
    assert integrity["input_state_hash"] == integrity["input_state_hash_after_handoff"]
    assert integrity["members_hash"] == real["lmv2_state"]["members_hash"]


@pytest.mark.integration
@pytest.mark.parametrize("close_iso", MULTI_CLOSES)
def test_multi_close_real_entry_bridge_hash(ch_client, close_iso: str):
    client, database = ch_client
    for pd in (1, 5, 10):
        bridge = load_historical_lmv2_backtester_input_at_close(
            close_iso, pool_detail=pd, client=client, database=database
        )
        real = run_historical_lmv2_input_dry_run(
            decision_close=close_iso,
            pool_detail=pd,
            client=client,
            database=database,
        )
        assert real["lmv2_state"]["members_hash"] == bridge.members_hash


@pytest.mark.integration
def test_oa_entry_rejects_wrong_data_source_loader(ch_client):
    client, database = ch_client
    ensure_oa_on_path()
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.historical_lmv2_input_dry_run import (
        HistoricalLmv2InputDryRunError,
        run_historical_lmv2_input_dry_run as oa_run,
    )

    def bad_loader(dc, *, pool_detail: int, symbol: str, timeframe: str, **_):
        st = load_backtester_active_state_at_close(
            dc,
            pool_detail=pool_detail,
            symbol=symbol,
            timeframe=timeframe,
            client=client,
            database=database,
        )
        payload = {
            "bridge_version": "x",
            "data_kind": "wrong_kind",
            "availability_class": "RESEARCH_HISTORICAL",
            "scanner_pool_equivalence": "NOT_CLAIMED",
            "symbol": st.symbol,
            "timeframe": st.timeframe,
            "pool_detail": st.pool_detail,
            "pipeline_line_id": st.pipeline_line_id,
            "config_hash": st.config_hash,
            "decision_close": st.decision_close,
            "snapshot_id": st.snapshot_id,
            "members_hash": st.members_hash,
            "input_state_hash": "deadbeef",
            "active_members": list(st.active_members),
            "active_member_count": st.active_member_count,
        }
        return payload

    with pytest.raises(HistoricalLmv2InputDryRunError) as exc:
        oa_run(
            decision_close=REFERENCE_CLOSE,
            pool_detail=10,
            loader=bad_loader,
        )
    assert exc.value.code == "INVALID_DATA_KIND"
