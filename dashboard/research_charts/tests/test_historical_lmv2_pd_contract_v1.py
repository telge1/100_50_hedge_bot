"""Unit tests for historical LMV2 PD contract (Phase 5W)."""

from __future__ import annotations

import pytest

from research_charts.historical_lmv2_pd_contract_v1 import (
    historical_pd_registry_export,
    resolve_historical_pd_config,
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
