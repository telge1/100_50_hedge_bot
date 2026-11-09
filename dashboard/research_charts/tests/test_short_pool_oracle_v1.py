"""Phase 0: Short pool oracle — current path only (ui_lld_config, no frozen)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_repo = Path(__file__).resolve().parents[5]
_dash = _repo / "backtester" / "short"
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))
if str(_dash) not in sys.path:
    sys.path.insert(0, str(_dash))

from pool_pattern.market import ensure_paths

ensure_paths()

from dashboard.research_charts.short_pool_oracle_v1 import (
    ORACLE_FIXTURES,
    oracle_snapshot_twice,
    short_pool_oracle_snapshot,
)

_GOLDEN_PATH = Path(__file__).resolve().parent / "short_pool_oracle_v1_golden.json"


def _load_golden() -> dict:
    if not _GOLDEN_PATH.is_file():
        pytest.skip(f"golden file missing: {_GOLDEN_PATH} (run refresh script or pytest with CH)")
    return json.loads(_GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
@pytest.mark.parametrize("symbol,decision_iso", ORACLE_FIXTURES)
def test_short_pool_oracle_deterministic_repeat(symbol: str, decision_iso: str):
    a, b = oracle_snapshot_twice(symbol, decision_iso)
    assert a["available_pool_ids"] == b["available_pool_ids"]
    assert a["available_rows_hash"] == b["available_rows_hash"]
    assert a["candle_index"] == b["candle_index"]


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
@pytest.mark.parametrize("symbol,decision_iso", ORACLE_FIXTURES)
def test_short_pool_oracle_matches_golden(symbol: str, decision_iso: str):
    golden = _load_golden()
    key = f"{symbol}|{decision_iso}"
    assert key in golden, f"missing golden key {key}"
    snap = short_pool_oracle_snapshot(symbol, decision_iso)
    expected = golden[key]
    assert snap["available_pool_ids"] == expected["available_pool_ids"]
    assert snap["available_rows_hash"] == expected["available_rows_hash"]
    assert snap["candle_index"] == expected["candle_index"]
    assert snap["entry_open"] == expected["entry_open"]
    assert snap["pane_first_timestamp"] == expected["pane_first_timestamp"]


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_short_pool_oracle_golden_file_covers_all_fixtures():
    golden = _load_golden()
    for symbol, decision_iso in ORACLE_FIXTURES:
        assert f"{symbol}|{decision_iso}" in golden
