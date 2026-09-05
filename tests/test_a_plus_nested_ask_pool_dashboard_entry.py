"""Engine-side tests for Nested Ask Pool research_entry (no dashboard)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.config import REFERENCE_ENTRY_APPROX
from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.research_entry import (
    STRATEGY_ID,
    bar_close_to_chart_open,
    build_overlay_payload_from_run_dir,
    frozen_config,
)


def test_strategy_id_distinct_from_aps():
    assert STRATEGY_ID == "a_plus_nested_ask_pool_edge_short_v1"
    assert STRATEGY_ID != "a_plus_liquidity_pool_signal_scanner_v1"
    assert "liquidity_pool_signal_scanner" not in str(frozen_config())


def test_bar_close_to_chart_open_offset():
    close = datetime(2026, 8, 28, 1, 3, 0)
    assert bar_close_to_chart_open(close) == datetime(2026, 8, 28, 1, 2, 0)


def test_overlay_from_existing_nested_run_dir():
    root = Path(
        "/home/telgenbuescher/projects/orderbook_analyse/results/"
        "a_plus_nested_ask_pool_edge_short_v1/nested_ask_pool_edge_short_v1_1787991024"
    )
    if not root.is_dir():
        pytest.skip("nested result artifact missing")
    start = datetime(2026, 8, 28, 0, 0, 0)
    end = datetime(2026, 8, 28, 23, 59, 0)
    payload = build_overlay_payload_from_run_dir(
        root, symbol="DOGEUSDT", scan_start=start, scan_end=end, show_rejected=False
    )
    assert payload["strategy_id"] == STRATEGY_ID
    kinds = {s["kind"] for s in payload["specs"]}
    assert "NAP_PENDING_LIMIT" in kinds
    assert "NAP_FILL" in kinds
    assert "NAP_SL" in kinds
    assert "NAP_TP" in kinds
    assert "NAP_EXIT" in kinds
    assert "NAP_REJECTED" not in kinds
    for s in payload["specs"]:
        if s["kind"] == "NAP_PENDING_LIMIT":
            assert s["price"] == s["start_price"] == s["end_price"]
            assert "SHORT LIMIT" in (s.get("text") or "")
            assert "child_pool_id" in (s.get("tooltip") or "")
            assert "LONG" not in (s.get("text") or "")
        if s["kind"] == "NAP_FILL":
            assert s.get("position") == "at_price"
            assert s.get("text") == "SHORT FILL"
            engine = datetime.fromisoformat(str(s["meta"]["engine_fill_at"])[:19])
            chart = datetime.fromisoformat(str(s["timestamp"]).replace("Z", "")[:19])
            assert chart == engine - timedelta(minutes=1)


def test_reference_approx_only_in_tests_not_detection():
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1 import geometry

    src = Path(geometry.__file__).read_text(encoding="utf-8")
    assert "0.08791" not in src
    assert "REFERENCE_ENTRY" not in src
    assert REFERENCE_ENTRY_APPROX


def test_single_symbol_validation():
    from orderbook_analyse.a_plus_nested_ask_pool_edge_short_v1.research_entry import (
        run_single_symbol_research_backtest,
    )

    with pytest.raises(ValueError, match="single_symbol"):
        run_single_symbol_research_backtest(
            symbol="DOGEUSDT,BTCUSDT",
            start="2026-08-28T00:00:00Z",
            end="2026-08-28T01:00:00Z",
        )
    with pytest.raises(ValueError, match="START_NOT_BEFORE_END"):
        run_single_symbol_research_backtest(
            symbol="DOGEUSDT",
            start="2026-08-28T02:00:00Z",
            end="2026-08-28T01:00:00Z",
        )
