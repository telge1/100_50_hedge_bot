"""Unit tests for historical LMV2 PD contract (Phase 5W)."""

from __future__ import annotations

import pytest

from research_charts.historical_lmv2_pd_contract_v1 import (
    historical_pd_registry_export,
    membership_slot_minutes,
    resolve_historical_pd_config,
    resolve_viewer_historical_scope,
    supported_historical_pool_details,
)
from research_charts.historical_pool_state_v1 import HistoricalPoolStateError


def test_supported_pool_details():
    assert supported_historical_pool_details() == (1, 5, 10)


def test_resolve_pd1_pd5_pd10_lines_and_hashes():
    for pd, suffix in ((1, "15mpd1_v1"), (5, "15mpd5_v1"), (10, "15mpd10_v1")):
        cfg = resolve_historical_pd_config(pd)
        assert cfg.pool_detail == pd
        assert cfg.pipeline_line_id.endswith(suffix)
        assert len(cfg.config_hash) == 64


def test_resolve_5m_pd1_line():
    cfg = resolve_historical_pd_config(1, timeframe="5m")
    assert cfg.timeframe == "5m"
    assert cfg.pipeline_line_id == "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"


def test_viewer_scope_5m_pd5_pd10_lines():
    for pd, suffix in ((5, "5mpd5_v1"), (10, "5mpd10_v1")):
        cfg = resolve_viewer_historical_scope(timeframe="5m", pool_detail=pd)
        assert cfg.timeframe == "5m"
        assert cfg.pool_detail == pd
        assert cfg.pipeline_line_id.endswith(suffix)


def test_viewer_scope_15m_pd5_pd10_lines():
    for pd, suffix in ((5, "15mpd5_v1"), (10, "15mpd10_v1")):
        cfg = resolve_viewer_historical_scope(timeframe="15m", pool_detail=pd)
        assert cfg.timeframe == "15m"
        assert cfg.pool_detail == pd
        assert cfg.pipeline_line_id.endswith(suffix)


def test_viewer_scope_4h_pd10_line():
    cfg = resolve_viewer_historical_scope(timeframe="4h", pool_detail=10)
    assert cfg.timeframe == "4h"
    assert cfg.pool_detail == 10
    assert cfg.pipeline_line_id == "historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1"
    assert membership_slot_minutes("4h") == 240


def test_viewer_scope_rejects_unapproved_combo():
    with pytest.raises(HistoricalPoolStateError) as exc:
        resolve_viewer_historical_scope(timeframe="4h", pool_detail=1)
    assert exc.value.code == "INVALID_SCOPE"
    with pytest.raises(HistoricalPoolStateError) as exc:
        resolve_viewer_historical_scope(timeframe="4h", pool_detail=5)
    assert exc.value.code == "INVALID_SCOPE"
    with pytest.raises(HistoricalPoolStateError) as exc:
        resolve_viewer_historical_scope(timeframe="15m", pool_detail=3)
    assert exc.value.code == "INVALID_SCOPE"


def test_invalid_pd_raises():
    with pytest.raises(HistoricalPoolStateError) as exc:
        resolve_historical_pd_config(7)
    assert exc.value.code == "INVALID_HISTORICAL_PD"


def test_registry_export_matches_entries():
    reg = historical_pd_registry_export()
    assert reg["supported_pool_details"] == [1, 5, 10]
    for pd in (1, 5, 10):
        assert str(pd) in reg["entries"]
        assert reg["entries"][str(pd)]["config_hash"] == resolve_historical_pd_config(pd).config_hash
