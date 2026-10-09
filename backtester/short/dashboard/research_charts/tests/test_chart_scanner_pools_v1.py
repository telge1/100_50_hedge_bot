"""Phase 2: Live Chart scanner pool adapter parity with COMMON."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[4]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
_dash = _repo / "backtester" / "short"
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from pool_pattern.market import ensure_paths

ensure_paths()

from bot.common_causal_pools_v1 import common_causal_pool_universe
from dashboard.research_charts.chart_scanner_pools_v1 import (
    chart_common_pool_snapshot,
    chart_scanner_pool_snapshot,
)
from dashboard.research_charts.frozen_lld_config_v1 import HISTORY_WEEKS, PANE_FROM_UTC
from dashboard.research_charts.lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    parity_diff_pool_rows,
)
from dashboard.research_charts.short_pool_oracle_v1 import ORACLE_FIXTURES, TIMEFRAME, parse_decision_iso
from pool_scan.clock import bar_close

_TRP = Path("/home/telgenbuescher/projects/trading_research_platform").is_dir()
PANE_FROM_UNIX = int(PANE_FROM_UTC.timestamp())
NOW_UNIX = int(datetime.now(timezone.utc).timestamp())
RECENT_SYMBOLS = ("APTUSDT", "XRPUSDT", "DOGEUSDT")


def _chart_vs_common(symbol: str, decision_iso: str) -> None:
    common = common_causal_pool_universe(symbol, parse_decision_iso(decision_iso))
    chart = chart_scanner_pool_snapshot(symbol, decision_iso)
    assert chart["symbol"] == common.symbol
    assert chart["timeframe"] == common.timeframe
    assert chart["decision_close"] == common.decision_close.isoformat()
    assert chart["decision_index"] == common.decision_index
    assert chart["pool_count"] == len(common.available)
    assert chart["rows_hash"] == common.rows_hash
    assert chart["config_version"] == common.config_version
    assert sorted(p["pool_id"] for p in chart["pools"]) == sorted(common.available_pool_ids)
    chart_rows = [
        {"pool_id": p["pool_id"], "bottom": p["lower"], "top": p["upper"]} for p in chart["pools"]
    ]
    diff = parity_diff_pool_rows(common.available, chart_rows)
    assert diff.get("ids_match") is True
    for row in chart["pools"]:
        assert row["source_timeframe"] == TIMEFRAME
        assert "lower" in row and "upper" in row


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("symbol", RECENT_SYMBOLS)
def test_chart_current_t_parity(symbol: str):
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    for idx in (len(stamps) - 1, max(0, len(stamps) - 5)):
        decision = bar_close(stamps[idx], TIMEFRAME)
        _chart_vs_common(symbol, decision.isoformat())


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("symbol,decision_iso", ORACLE_FIXTURES)
def test_chart_historical_oracle_parity(symbol: str, decision_iso: str):
    if symbol not in RECENT_SYMBOLS and symbol not in ("ADAUSDT",):
        pytest.skip("oracle fixture outside phase-2 symbol set")
    _chart_vs_common(symbol, decision_iso)


def _assert_causal_prefix_parity(symbol: str, decision_iso: str) -> None:
    decision = parse_decision_iso(decision_iso)
    full = chart_scanner_pool_snapshot(symbol, decision.isoformat())
    _packed, candles = load_pane_candles(
        symbol,
        TIMEFRAME,
        from_unix=PANE_FROM_UNIX,
        to_unix=int(decision.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    from bot.common_causal_pools_v1 import common_causal_pools_from_loaded_pane

    prefix = common_causal_pools_from_loaded_pane(symbol, candles, decision)
    trunc = chart_common_pool_snapshot(symbol, decision.isoformat())
    assert trunc["decision_close"] == decision.isoformat()
    assert trunc["rows_hash"] == prefix.rows_hash
    assert trunc["rows_hash"] == full["rows_hash"]
    assert trunc["decision_index"] == prefix.decision_index
    assert sorted(p["pool_id"] for p in trunc["pools"]) == sorted(prefix.available_pool_ids)


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("symbol,decision_iso", [ORACLE_FIXTURES[0]])
def test_chart_causal_prefix_parity_oracle(symbol: str, decision_iso: str):
    _assert_causal_prefix_parity(symbol, decision_iso)


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("symbol", ("APTUSDT", "XRPUSDT"))
def test_chart_causal_prefix_parity_recent(symbol: str):
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    decision = bar_close(stamps[max(0, len(stamps) - 20)], TIMEFRAME)
    _assert_causal_prefix_parity(symbol, decision.isoformat())


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_chart_overlays_namespace_isolated():
    symbol = RECENT_SYMBOLS[0]
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    decision = bar_close(stamps[-1], TIMEFRAME)
    snap = chart_scanner_pool_snapshot(symbol, decision.isoformat())
    assert snap["namespace"] == "SCANNER_COMMON"
    for ov in snap["overlays"]:
        assert ov["namespace"] == "SCANNER_COMMON"
        assert str(ov["id"]).startswith("scanner-common:")
        end_t = ov["points"][1]["time"]
        assert end_t <= int(decision.timestamp()) + 1
