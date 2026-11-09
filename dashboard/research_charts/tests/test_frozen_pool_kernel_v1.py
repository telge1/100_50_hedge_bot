"""Phase 2: frozen_pool_kernel_v1 parity vs Short oracle golden."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

_repo = Path(__file__).resolve().parents[5]
_dash = _repo / "backtester" / "short"
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.frozen_lld_config_v1 import PANE_FROM_UTC
from dashboard.research_charts.frozen_pool_kernel_v1 import (
    FrozenPoolKernelError,
    available_at_decision_v1,
    available_from_loaded_pane_v1,
    resolve_decision_bar_index,
)
from dashboard.research_charts.lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    parity_diff_pool_rows,
    scanner_pools_for_index,
)
from dashboard.research_charts.short_pool_oracle_v1 import (
    HISTORY_WEEKS,
    ORACLE_FIXTURES,
    PANE_FROM_UNIX,
    parse_decision_iso,
    rows_hash,
)
from dashboard.research_charts.frozen_lld_config_v1 import frozen_lld_config_for_timeframe_v1
from pool_scan.clock import bar_close

_GOLDEN_PATH = Path(__file__).resolve().parent / "short_pool_oracle_v1_golden.json"
_NOW_UNIX = int(datetime.now(timezone.utc).timestamp())


def _golden() -> dict:
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
@pytest.mark.parametrize("symbol,decision_iso", ORACLE_FIXTURES)
def test_kernel_matches_oracle_golden(symbol: str, decision_iso: str):
    key = f"{symbol}|{decision_iso}"
    expected = _golden()[key]
    decision = parse_decision_iso(decision_iso)
    result = available_at_decision_v1(symbol, decision)
    assert result.candle_index == expected["candle_index"]
    assert result.candle_open_timestamp.isoformat() == expected["entry_open"]
    assert result.pane_first_timestamp.isoformat() == expected["pane_first_timestamp"]
    assert len(result.available_pool_ids) == expected["available_count"]
    assert result.available_pool_ids == expected["available_pool_ids"]
    assert result.available_rows_hash == expected["available_rows_hash"]
    assert result.frozen_config_version == 1


@pytest.mark.medium
@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_decision_timestamp_is_bar_close_not_open():
    """DOGE 2026-06-11 12:00 UTC close ↔ 11:45 open (oracle fixture)."""
    decision_iso = "2026-06-11T12:00:00+00:00"
    open_iso = "2026-06-11T11:45:00+00:00"
    result = available_at_decision_v1("DOGEUSDT", parse_decision_iso(decision_iso))
    assert result.decision_timestamp.isoformat() == decision_iso
    assert result.candle_open_timestamp.isoformat() == open_iso
    assert bar_close(result.candle_open_timestamp, "15m") == result.decision_timestamp

    # 11:45 UTC is a valid *bar close* (open 11:30), not the open of the 12:00-close bar.
    misread = available_at_decision_v1("DOGEUSDT", parse_decision_iso(open_iso))
    assert misread.candle_index != result.candle_index
    assert misread.candle_open_timestamp != result.candle_open_timestamp

    with pytest.raises(FrozenPoolKernelError, match="not a 15m bar close"):
        available_at_decision_v1(
            "DOGEUSDT",
            datetime(2026, 6, 11, 12, 7, 0, tzinfo=timezone.utc),
        )


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
@pytest.mark.parametrize("symbol,decision_iso", ORACLE_FIXTURES)
def test_no_lookahead_suffix_pane(symbol: str, decision_iso: str):
    decision = parse_decision_iso(decision_iso)
    kernel = available_at_decision_v1(symbol, decision)
    cfg = frozen_lld_config_for_timeframe_v1("15m")
    _, trunc = load_pane_candles(
        symbol, "15m", from_unix=PANE_FROM_UNIX, to_unix=int(decision.timestamp()), history_weeks=HISTORY_WEEKS
    )
    _, full = load_pane_candles(
        symbol, "15m", from_unix=PANE_FROM_UNIX, to_unix=_NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    stamps_t = candle_stamps(trunc)
    idx_t = kernel.candle_index
    assert bar_close(stamps_t[idx_t], "15m") == decision
    idx_f = next(i for i, s in enumerate(candle_stamps(full)) if s == stamps_t[idx_t])
    pools_trunc, _, _ = scanner_pools_for_index(trunc, "15m", idx_t, cfg, cache={})
    pools_full, _, _ = scanner_pools_for_index(full, "15m", idx_f, cfg, cache={})
    diff = parity_diff_pool_rows(pools_trunc, pools_full)
    assert diff["ids_match"], diff
    assert sorted(r["pool_id"] for r in pools_trunc) == kernel.available_pool_ids


@pytest.mark.medium
@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_query_order_determinism_on_doge():
    t1 = parse_decision_iso("2026-06-07T05:15:00+00:00")
    t2 = parse_decision_iso("2026-06-11T12:00:00+00:00")
    t3 = parse_decision_iso("2026-06-25T17:30:00+00:00")
    symbol = "DOGEUSDT"

    r_a = available_at_decision_v1(symbol, t2, cache=None)

    shared: dict = {}
    available_at_decision_v1(symbol, t3, cache=shared)
    r_b = available_at_decision_v1(symbol, t2, cache=shared)

    shared2: dict = {}
    available_at_decision_v1(symbol, t1, cache=shared2)
    r_c = available_at_decision_v1(symbol, t2, cache=shared2)

    assert r_a.available_pool_ids == r_b.available_pool_ids
    assert r_a.available_rows_hash == r_b.available_rows_hash
    assert r_a.available_pool_ids == r_c.available_pool_ids


@pytest.mark.medium
@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_frozen_config_isolation_from_workspace(monkeypatch):
    decision = parse_decision_iso("2026-06-11T12:00:00+00:00")
    baseline = available_at_decision_v1("DOGEUSDT", decision)

    from dashboard.research_charts import workspace_session

    ws = workspace_session.get_workspace()
    original = (int(ws.lld_config.highest_len), int(ws.lld_config.lowest_len), int(ws.lld_config.amount))
    try:
        ws.lld_config.highest_len = 1
        ws.lld_config.lowest_len = 1
        ws.lld_config.amount = 1
        polluted = available_at_decision_v1("DOGEUSDT", decision)
    finally:
        ws.lld_config.highest_len = original[0]
        ws.lld_config.lowest_len = original[1]
        ws.lld_config.amount = original[2]

    assert polluted.available_pool_ids == baseline.available_pool_ids
    assert polluted.available_rows_hash == baseline.available_rows_hash


def test_invalid_decision_raises():
    with pytest.raises(FrozenPoolKernelError, match="not a 15m bar close"):
        available_at_decision_v1(
            "XRPUSDT",
            datetime(2026, 6, 11, 12, 7, 0, tzinfo=timezone.utc),
        )


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
@pytest.mark.parametrize("symbol,decision_iso", ORACLE_FIXTURES)
def test_loaded_pane_api_matches_at_decision_api(symbol: str, decision_iso: str):
    decision = parse_decision_iso(decision_iso)
    loaded = available_at_decision_v1(symbol, decision)
    _, candles = load_pane_candles(
        symbol,
        "15m",
        from_unix=PANE_FROM_UNIX,
        to_unix=int(decision.timestamp()),
        history_weeks=HISTORY_WEEKS,
    )
    from_pane = available_from_loaded_pane_v1(symbol, candles, decision)
    assert from_pane.available_pool_ids == loaded.available_pool_ids
    assert from_pane.available_rows_hash == loaded.available_rows_hash
    assert from_pane.candle_index == loaded.candle_index

    _, full = load_pane_candles(
        symbol, "15m", from_unix=PANE_FROM_UNIX, to_unix=_NOW_UNIX, history_weeks=HISTORY_WEEKS
    )
    from_full = available_from_loaded_pane_v1(symbol, full, decision)
    assert from_full.available_pool_ids == loaded.available_pool_ids
    assert from_full.available_rows_hash == loaded.available_rows_hash


def test_resolve_decision_bar_index_unit():
    base = PANE_FROM_UTC
    stamps = [base]
    with pytest.raises(FrozenPoolKernelError):
        resolve_decision_bar_index(stamps, base, "15m")
