"""Phase 1: frozen_lld_config_v1 must match current Short path pool outputs exactly."""

from __future__ import annotations

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

from dashboard.research_charts.frozen_lld_config_v1 import (
    FROZEN_LLD_CONFIG_VERSION,
    LLD_ENGINE_V1,
    frozen_lld_config_for_timeframe_v1,
    frozen_lld_config_snapshot_v1,
)
from dashboard.research_charts.lld_research_kernel import (
    candle_stamps,
    load_pane_candles,
    scanner_pools_for_index,
    ui_lld_config,
)
from dashboard.research_charts.short_pool_oracle_v1 import (
    HISTORY_WEEKS,
    ORACLE_FIXTURES,
    PANE_FROM_UNIX,
    TIMEFRAME,
    parse_decision_iso,
)
from pool_scan.clock import bar_close


def _config_diff(current, frozen) -> dict:
    return {
        "enabled": (bool(current.enabled), bool(frozen.enabled)),
        "amount": (int(current.amount), int(frozen.amount)),
        "highest_len": (int(current.highest_len), int(frozen.highest_len)),
        "lowest_len": (int(current.lowest_len), int(frozen.lowest_len)),
    }


def _available_pool_ids(candles, idx, cfg):
    available, _ui, _proof = scanner_pools_for_index(
        candles, TIMEFRAME, idx, cfg, cache={}
    )
    return sorted(r["pool_id"] for r in available)


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
def test_frozen_engine_matches_workspace_ui_lld_config_15m():
    current = ui_lld_config("15m")
    frozen = frozen_lld_config_for_timeframe_v1("15m")
    diff = _config_diff(current, frozen)
    assert diff["enabled"] == (True, True) or diff["enabled"][0] == diff["enabled"][1]
    assert current.enabled == frozen.enabled
    assert int(current.amount) == int(frozen.amount), diff
    assert int(current.highest_len) == int(frozen.highest_len), diff
    assert int(current.lowest_len) == int(frozen.lowest_len), diff


@pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/trading_research_platform").is_dir(),
    reason="TRP/CH not available",
)
@pytest.mark.parametrize("symbol,decision_iso", ORACLE_FIXTURES)
def test_frozen_config_available_pools_match_current_short(symbol: str, decision_iso: str):
    decision = parse_decision_iso(decision_iso)
    decision_unix = int(decision.timestamp())
    current_cfg = ui_lld_config(TIMEFRAME)
    frozen_cfg = frozen_lld_config_for_timeframe_v1(TIMEFRAME)

    _packed, candles = load_pane_candles(
        symbol,
        TIMEFRAME,
        from_unix=PANE_FROM_UNIX,
        to_unix=decision_unix,
        history_weeks=HISTORY_WEEKS,
    )
    stamps = candle_stamps(candles)
    idx = next(i for i, s in enumerate(stamps) if bar_close(s, TIMEFRAME) == decision)

    ids_current = _available_pool_ids(candles, idx, current_cfg)
    ids_frozen = _available_pool_ids(candles, idx, frozen_cfg)

    if ids_current != ids_frozen:
        only_current = sorted(set(ids_current) - set(ids_frozen))
        only_frozen = sorted(set(ids_frozen) - set(ids_current))
        cfg_diff = _config_diff(current_cfg, frozen_cfg)
        pytest.fail(
            "FROZEN_CONFIG_PARITY FAIL\n"
            f"symbol={symbol} decision={decision_iso}\n"
            f"only_current={only_current}\n"
            f"only_frozen={only_frozen}\n"
            f"config_diff={cfg_diff}\n"
            f"frozen_engine_v1={LLD_ENGINE_V1}\n"
            f"workspace_snapshot={frozen_lld_config_snapshot_v1(TIMEFRAME)}"
        )


def test_frozen_config_version_constant():
    assert FROZEN_LLD_CONFIG_VERSION == 1
