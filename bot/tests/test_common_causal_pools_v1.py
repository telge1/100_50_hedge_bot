"""Phase 1: COMMON_CAUSAL_POOL_UNIVERSE parity and drift guards."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[2]
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_repo / "backtester" / "short") not in sys.path:
    sys.path.insert(0, str(_repo / "backtester" / "short"))
_ctx = _repo / "results/pool_scan/context_study_v1"
if str(_ctx) not in sys.path:
    sys.path.insert(0, str(_ctx))

from pool_pattern.market import ensure_paths

ensure_paths()

from bot.common_causal_pools_v1 import (
    common_causal_pool_universe,
    common_causal_pools_from_loaded_pane,
)
from bot.e1r_live_scanner.short_pools import available_for_short_entry
from dashboard.research_charts.frozen_lld_config_v1 import (
    PANE_FROM_UTC,
    frozen_lld_config_for_timeframe_v1,
)
from dashboard.research_charts.frozen_pool_kernel_v1 import available_from_loaded_pane_v1
from dashboard.research_charts.lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    parity_diff_pool_rows,
    scanner_pools_for_index,
    ui_lld_config,
)
from dashboard.research_charts.short_pool_oracle_v1 import (
    HISTORY_WEEKS,
    ORACLE_FIXTURES,
    TIMEFRAME,
    parse_decision_iso,
    rows_hash,
)
from pool_scan.clock import bar_close

_TRP = Path("/home/telgenbuescher/projects/trading_research_platform").is_dir()
PANE_FROM_UNIX = int(PANE_FROM_UTC.timestamp())
NOW_UNIX = int(datetime.now(timezone.utc).timestamp())
RECENT_SYMBOLS = ("APTUSDT", "XRPUSDT", "DOGEUSDT")


def _legacy_long_pools(candles, idx, cache):
    pools, _ui, _proof = scanner_pools_for_index(
        candles, TIMEFRAME, idx, ui_lld_config(TIMEFRAME), cache=cache
    )
    return pools


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_workspace_frozen_config_parity():
    """Drift guard: workspace LLD must match frozen engine (CI fails on JSON drift)."""
    current = ui_lld_config(TIMEFRAME)
    frozen = frozen_lld_config_for_timeframe_v1(TIMEFRAME)
    assert bool(current.enabled) == bool(frozen.enabled)
    assert int(current.amount) == int(frozen.amount)
    assert int(current.highest_len) == int(frozen.highest_len)
    assert int(current.lowest_len) == int(frozen.lowest_len)


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("symbol", RECENT_SYMBOLS)
def test_common_equals_short_frozen_kernel(symbol: str):
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    idx = len(stamps) - 1
    decision = bar_close(stamps[idx], TIMEFRAME)
    cache: dict = {}
    kernel = available_from_loaded_pane_v1(
        symbol, candles, decision, timeframe=TIMEFRAME, cache=cache, strict_complete_buckets=True
    )
    cache2: dict = {}
    common = common_causal_pools_from_loaded_pane(
        symbol, candles, decision, timeframe=TIMEFRAME, cache=cache2
    )
    assert kernel.available_rows_hash == common.rows_hash
    assert kernel.available_pool_ids == common.available_pool_ids
    assert kernel.candle_index == common.decision_index
    diff = parity_diff_pool_rows(kernel.available, common.available)
    assert diff.get("ids_match") is True


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("symbol", RECENT_SYMBOLS)
def test_common_equals_legacy_long_path(symbol: str):
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    for idx in (len(stamps) - 3, len(stamps) - 2, len(stamps) - 1):
        decision = bar_close(stamps[idx], TIMEFRAME)
        legacy_cache: dict = {}
        common_cache: dict = {}
        legacy = _legacy_long_pools(candles, idx, legacy_cache)
        common = common_causal_pools_from_loaded_pane(
            symbol, candles, decision, timeframe=TIMEFRAME, cache=common_cache
        )
        assert rows_hash(legacy) == common.rows_hash
        diff = parity_diff_pool_rows(legacy, common.available)
        assert diff.get("ids_match") is True
        assert common.decision_index == idx


_ORACLE_FOR_COMMON = [
    (sym, iso) for sym, iso in ORACLE_FIXTURES if sym in RECENT_SYMBOLS
]


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
@pytest.mark.parametrize("symbol,decision_iso", _ORACLE_FOR_COMMON)
def test_common_historical_parity_short_and_long(symbol: str, decision_iso: str):
    decision = parse_decision_iso(decision_iso)
    common = common_causal_pool_universe(symbol, decision, timeframe=TIMEFRAME)
    _packed, candles = load_pane_candles(
        symbol,
        TIMEFRAME,
        from_unix=PANE_FROM_UNIX,
        to_unix=int(decision.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    stamps = candle_stamps(candles)
    idx = next(i for i, s in enumerate(stamps) if bar_close(s, TIMEFRAME) == decision)
    legacy = _legacy_long_pools(candles, idx, {})
    assert parity_diff_pool_rows(legacy, common.available).get("ids_match") is True
    assert common.decision_index == idx


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_prefix_causality_historical_t_unchanged_by_future_bars():
    symbol = "XRPUSDT"
    decision = parse_decision_iso("2026-06-18T00:45:00+00:00")
    at_t = common_causal_pool_universe(symbol, decision, timeframe=TIMEFRAME)
    _packed, full = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    at_t_on_full = common_causal_pools_from_loaded_pane(symbol, full, decision, timeframe=TIMEFRAME)
    assert at_t.rows_hash == at_t_on_full.rows_hash
    assert at_t.available_pool_ids == at_t_on_full.available_pool_ids


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_workspace_drift_common_unchanged_legacy_would_diverge():
    symbol = "DOGEUSDT"
    decision = parse_decision_iso("2026-06-11T12:00:00+00:00")
    _packed, candles = load_pane_candles(
        symbol,
        TIMEFRAME,
        from_unix=PANE_FROM_UNIX,
        to_unix=int(decision.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    stamps = candle_stamps(candles)
    idx = next(i for i, s in enumerate(stamps) if bar_close(s, TIMEFRAME) == decision)
    baseline = common_causal_pools_from_loaded_pane(symbol, candles, decision, timeframe=TIMEFRAME)
    fr = frozen_lld_config_for_timeframe_v1(TIMEFRAME)
    drifted = replace(fr, highest_len=1, lowest_len=1)
    legacy_drift, _, _ = scanner_pools_for_index(candles, TIMEFRAME, idx, drifted, cache={})
    common_again = common_causal_pools_from_loaded_pane(symbol, candles, decision, timeframe=TIMEFRAME)
    assert common_again.rows_hash == baseline.rows_hash
    assert parity_diff_pool_rows(baseline.available, legacy_drift).get("ids_match") is False


@pytest.mark.skipif(not _TRP, reason="TRP/CH not available")
def test_short_entry_uses_common_via_kernel():
    symbol = "APTUSDT"
    _packed, candles = load_pane_candles(
        symbol, TIMEFRAME, from_unix=PANE_FROM_UNIX, to_unix=NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps = candle_stamps(candles)
    decision = bar_close(stamps[-1], TIMEFRAME)
    cache: dict = {}
    short = available_for_short_entry(symbol, candles, decision, frozen_lld_cache=cache)
    common = common_causal_pools_from_loaded_pane(symbol, candles, decision, timeframe=TIMEFRAME)
    assert short.available_rows_hash == common.rows_hash
