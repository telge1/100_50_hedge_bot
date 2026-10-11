"""Fail-closed checks for the Phase 11V-A adapter. No ClickHouse and no engine run."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from research_charts.live_micro_lld_v2_ch_test.phase11u_b.adapter import (
    AdapterStop,
    CatalogRow,
    MemberRow,
    PublicationHit,
)
from research_charts.live_micro_lld_v2_ch_test.phase11u_c.coverage import base_file_status
from research_charts.live_micro_lld_v2_ch_test.phase11v_a.adapter import (
    MISSING_ORIGINAL_PUBLICATION,
    get_remaining_heatmap,
)
from research_charts.live_micro_lld_v2_ch_test.phase11v_a.causality import (
    close_is_on_grid,
    decision_bar_is_complete,
    future_candle,
    reject_reappearance,
)
from research_charts.live_micro_lld_v2_ch_test.phase11v_a.matrix import coverage_matrix
from research_charts.live_micro_lld_v2_ch_test.phase11v_a.payload import generic_payload
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1 import build_fixed_segments
from research_charts.live_micro_lld_v2_ch_test.remaining_pool_projection_v1_config import STRENGTH_CLASS_COLORS

ROOT = Path(__file__).resolve().parents[1] / "live_micro_lld_v2_ch_test"
CLOSE = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
OFFICIAL = "4b509b6565b06c1e37a09f195f001b55e7517de29355a0482871798472de7d31"


class Mem:
    def __init__(self, hits, members=None, catalog=None):
        self.hits = hits
        self._members = members or []
        self._catalog = catalog or []

    def find(self, **_kwargs):
        return self.hits

    def members(self, _snapshot_id):
        return self._members

    def catalog(self, _snapshot_id):
        return self._catalog


def _hit(snapshot="s1", fingerprint="fp"):
    return PublicationHit(snapshot, "line", fingerprint, "cfg", 1)


def test_unknown_symbol():
    with pytest.raises(AdapterStop) as caught:
        get_remaining_heatmap(Mem([]), symbol="BTCUSDT", timeframe="5m", pool_detail=10, decision_close=CLOSE, immutable_input_id=OFFICIAL, research_root=ROOT)
    assert caught.value.status == "UNSUPPORTED_SYMBOL"


def test_invalid_timeframe():
    with pytest.raises(AdapterStop) as caught:
        get_remaining_heatmap(Mem([]), symbol="DOGEUSDT", timeframe="2m", pool_detail=10, decision_close=CLOSE, immutable_input_id=OFFICIAL, research_root=ROOT)
    assert caught.value.status == "INVALID_REQUEST"


def test_invalid_pool_detail():
    with pytest.raises(AdapterStop) as caught:
        get_remaining_heatmap(Mem([]), symbol="DOGEUSDT", timeframe="5m", pool_detail=0, decision_close=CLOSE, immutable_input_id=OFFICIAL, research_root=ROOT)
    assert caught.value.status == "INVALID_REQUEST"


def test_invalid_close_grid():
    bad = datetime(2026, 9, 30, 12, 3, tzinfo=timezone.utc)
    assert close_is_on_grid(bad, "5m") is False
    assert close_is_on_grid(CLOSE, "5m") is True


def test_missing_original_publication():
    with pytest.raises(AdapterStop) as caught:
        get_remaining_heatmap(Mem([]), symbol="DOGEUSDT", timeframe="15m", pool_detail=10, decision_close=CLOSE, immutable_input_id=OFFICIAL, research_root=ROOT)
    assert caught.value.status == MISSING_ORIGINAL_PUBLICATION


def test_missing_catalog():
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import active_state_fingerprint

    fingerprint = active_state_fingerprint(["p"], ["upper"])
    hit = _hit(fingerprint=fingerprint)
    source = Mem([hit], members=[MemberRow("p", "upper")], catalog=[])
    with pytest.raises(AdapterStop) as caught:
        get_remaining_heatmap(source, symbol="DOGEUSDT", timeframe="5m", pool_detail=10, decision_close=CLOSE, immutable_input_id=OFFICIAL, research_root=ROOT)
    assert caught.value.status == "MISSING_CATALOG"


def test_ambiguous_snapshot():
    source = Mem([_hit("a", "fp"), _hit("b", "fp")])
    with pytest.raises(AdapterStop) as caught:
        get_remaining_heatmap(source, symbol="DOGEUSDT", timeframe="5m", pool_detail=10, decision_close=CLOSE, immutable_input_id=OFFICIAL, research_root=ROOT)
    assert caught.value.status == "AMBIGUOUS_PUBLICATION"


def test_wrong_composite_id():
    with pytest.raises(AdapterStop) as caught:
        get_remaining_heatmap(Mem([]), symbol="DOGEUSDT", timeframe="5m", pool_detail=10, decision_close=CLOSE, immutable_input_id="0" * 64, research_root=ROOT)
    assert caught.value.status == "INPUT_IDENTITY_MISMATCH"


def test_incomplete_warmup_is_not_candle_ready():
    assert base_file_status(CLOSE, "4h", 1) == "INSUFFICIENT_CANDLE_COVERAGE"


def test_future_candle_and_complete_bars():
    assert future_candle(int(CLOSE.timestamp()) + 60, CLOSE) is True
    for timeframe in ("5m", "15m", "30m", "1h", "4h"):
        assert decision_bar_is_complete(CLOSE, timeframe)


def test_mixed_timeframe_pool_rejected():
    from research_charts.live_micro_lld_v2_ch_test.phase11v_a.adapter import _reject_mixed

    with pytest.raises(AdapterStop) as caught:
        _reject_mixed("lldv2:DOGEUSDT:15m:micro:upper:1", "5m")
    assert caught.value.status == "MIXED_TIMEFRAME"


def test_payload_rejects_foreign_strength_and_collapsed_bounds():
    base = {
        "symbol": "DOGEUSDT",
        "timeframe": "5m",
        "pool_detail": 10,
        "decision_close": "2026-09-30T12:00:00Z",
        "published_snapshot_id": "s",
        "immutable_input_id": OFFICIAL,
        "coverage": "READY",
        "clickhouse_candle_reads": 0,
        "payload_id": "x",
        "fully_traversed_active_ids": [],
        "remaining": [],
    }
    bad_color = dict(base, active=[{"pool_id": "lldv2:DOGEUSDT:5m:micro:upper:1", "side": "upper", "bottom": 1, "top": 2, "strength": 1, "strength_class": "Weak", "color": "#ffffff", "known_at": "2026-09-30T11:00:00Z"}])
    with pytest.raises(ValueError):
        generic_payload(bad_color)
    collapsed = dict(base, active=[{"pool_id": "lldv2:DOGEUSDT:5m:micro:upper:1", "side": "upper", "bottom": 2, "top": 1, "strength": 1, "strength_class": "Weak", "color": STRENGTH_CLASS_COLORS["Weak"], "known_at": "2026-09-30T11:00:00Z"}])
    with pytest.raises(ValueError):
        generic_payload(collapsed)


def test_fixed_segments_keep_original_edges():
    segments = build_fixed_segments(original_bottom=1.0, original_top=2.0, remaining_bottom=1.4, remaining_top=2.0, strength_class="Weak")
    assert segments[0].segment_bottom == 1.0
    assert segments[-1].segment_top == 2.0
    assert segments[0].display_color == "#005BFF"


def test_reappearance_is_rejected():
    assert reject_reappearance(True, True) == "FAIL"
    assert reject_reappearance(True, False) == "PASS"


def test_adapter_does_not_call_the_engine_or_clickhouse_candles():
    text = Path(__file__).resolve().parents[1].joinpath(
        "live_micro_lld_v2_ch_test/phase11v_a/adapter.py"
    ).read_text(encoding="utf-8")
    assert "run_live_micro_lld_v2" not in text
    assert "clickhouse_connect" not in text


def test_matrix_has_fifty_rows_and_eight_ready():
    rows = coverage_matrix()
    assert len(rows) == 50
    ready = [f"{row['timeframe']} PD{row['pool_detail']}" for row in rows if row["CANDLE_INPUT_READY"]]
    assert ready == ["5m PD5", "5m PD6", "5m PD7", "5m PD8", "5m PD9", "5m PD10", "15m PD9", "15m PD10"]


def test_pd_presets_stay_original():
    from research_charts.live_micro_lld_v2_ch_test.identity import build_live_micro_lld_v2_config

    assert build_live_micro_lld_v2_config(1).swing_len == 30
    assert build_live_micro_lld_v2_config(10).volume_lookback == 300
    assert build_live_micro_lld_v2_config(5).swing_len == 12
