"""MC4E — D2 window API symbol resolution + cache key isolation."""

from __future__ import annotations

import pytest

from pd1_landscape_viewer_v1.historical_window_v1 import LandscapeWindowError
from pd1_landscape_viewer_v1.window_service_v1 import _key_parts, _normalize_window_symbol
from pd1_landscape_viewer_v1.window_response_cache_v1 import COMPUTE_MODE_EXACT_ORIGIN_V1, clear_cache_for_tests
from research_charts.historical_lmv2_pd_contract_v1 import resolve_viewer_historical_scope
from research_charts.multicoin_pd1_scope_v1 import MC3D_SOL_FULL_LINE, MC3E_BNB_FULL_LINE, DOGE_PROTECTED_5M_PD1_LINE

_WINDOW = dict(
    from_utc="2026-11-10T08:00:00Z",
    to_utc="2026-11-10T10:00:00Z",
    timeframe="5m",
    pool_detail=1,
    compute_mode=COMPUTE_MODE_EXACT_ORIGIN_V1,
)


def test_doge_default_symbol_matches_legacy_scope():
    legacy = resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
    implicit = _key_parts(symbol=None, **_WINDOW)
    explicit = _key_parts(symbol="DOGEUSDT", **_WINDOW)
    assert implicit.symbol == "DOGEUSDT"
    assert implicit.pipeline_line_id == legacy.pipeline_line_id
    assert implicit.pipeline_line_id == DOGE_PROTECTED_5M_PD1_LINE
    assert implicit.digest() == explicit.digest()


def test_sol_and_bnb_lines_isolated():
    doge = _key_parts(symbol="DOGEUSDT", **_WINDOW)
    sol = _key_parts(symbol="SOLUSDT", **_WINDOW)
    bnb = _key_parts(symbol="BNBUSDT", **_WINDOW)
    assert sol.symbol == "SOLUSDT"
    assert sol.pipeline_line_id == MC3D_SOL_FULL_LINE
    assert bnb.symbol == "BNBUSDT"
    assert bnb.pipeline_line_id == MC3E_BNB_FULL_LINE
    digests = {doge.digest(), sol.digest(), bnb.digest()}
    assert len(digests) == 3


def test_invalid_symbol_fail_closed():
    with pytest.raises(LandscapeWindowError) as exc:
        _normalize_window_symbol("NOTAUSDT")
    assert exc.value.code == "INVALID_SYMBOL"


def test_empty_symbol_defaults_doge():
    assert _normalize_window_symbol(None) == "DOGEUSDT"
    assert _normalize_window_symbol("") == "DOGEUSDT"


@pytest.mark.parametrize(
    "symbol,line",
    [
        ("DOGEUSDT", DOGE_PROTECTED_5M_PD1_LINE),
        ("SOLUSDT", MC3D_SOL_FULL_LINE),
        ("BNBUSDT", MC3E_BNB_FULL_LINE),
    ],
)
def test_each_symbol_pipeline_line(symbol, line):
    parts = _key_parts(symbol=symbol, **_WINDOW)
    assert parts.symbol == symbol
    assert parts.pipeline_line_id == line
    assert parts.timeframe == "5m"
    assert parts.pool_detail == 1


def test_cache_clear_api_smoke():
    clear_cache_for_tests()
