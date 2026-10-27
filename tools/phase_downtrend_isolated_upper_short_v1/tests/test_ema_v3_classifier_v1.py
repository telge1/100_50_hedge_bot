"""BT-D70 V3 classifier integration tests."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
for p in (
    PKG,
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
    ROOT / "tools/phase_bt_d69_pure_ema_false_allow_audit_v1",
):
    sys.path.insert(0, str(p))

from pure_ema_classifier_v1 import classify_feature_row  # noqa: E402
from pure_ema_series_provider_v1 import PureEmaSeriesProviderV1  # noqa: E402


def _minimal(**kwargs):
    base = {
        "ema_ready": True,
        "ema_bull_stack_9_20_59_200": False,
        "ema_tight_cluster": False,
        "close_below_ema20": True,
        "close_below_ema59": True,
        "ema59_slope_6b_pct": -0.1,
        "ema59_slope_12b_pct": -0.1,
        "ema200_slope_12b_pct": -0.05,
        "ema200_slope_24b_pct": -0.05,
    }
    base.update(kwargs)
    return base


def test_v1_unchanged_fallback_path():
    row = _minimal(ema_bear_stack_9_20_59_200=False)
    out = classify_feature_row(row, "v1")
    assert out["ema_structure_decision"] == "SHORT_OK"
    assert out["decision_reason_code"] == "PRICE_BELOW_FALLING_EMA59_200"


def test_v3_removes_fallback():
    row = _minimal(ema_bear_stack_9_20_59_200=False)
    out = classify_feature_row(row, "v3")
    assert out["ema_structure_decision"] != "SHORT_OK"


def test_v3_keeps_bear_stack_path():
    row = _minimal(ema_bear_stack_9_20_59_200=True)
    out = classify_feature_row(row, "v3")
    assert out["ema_structure_decision"] == "SHORT_OK"


def test_v3_reference_timestamps_if_ch_available():
    manifest = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"
    if not manifest.is_file():
        return
    from datetime import timedelta

    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch
    from validation_config_v1 import REFERENCE_WARMUP_HOURS

    lo = _parse_utc("2026-08-01T00:00:00Z") - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    keys, prices, _ = load_5m_ohlc_ch(_iso(lo), "2026-09-30T00:00:00Z")
    v3 = PureEmaSeriesProviderV1(bar_keys=keys, prices=prices, classifier_version="v3")
    for ts in ("2026-08-06T00:00:00Z", "2026-08-12T08:30:00Z", "2026-09-11T12:05:00Z"):
        assert v3.confirm_at_close(ts)["ema_structure_decision"] != "SHORT_OK"
    for ts in (
        "2026-09-09T20:55:00Z",
        "2026-08-05T03:30:00Z",
        "2026-08-05T07:30:00Z",
        "2026-08-13T10:30:00Z",
    ):
        assert v3.confirm_at_close(ts)["ema_structure_decision"] == "SHORT_OK"
    assert v3.confirm_at_close("2026-09-09T16:30:00Z")["ema_structure_decision"] != "SHORT_OK"


def test_chart20_v1_matches_d65_csv_if_present():
    chart = ROOT / "results/phase_bt_d65_pure_ema_review_v1/chart_review_20_timestamps.csv"
    manifest = ROOT / "results/phase_bt_d60_ema_history_v1/ema_history_manifest.json"
    if not chart.is_file() or not manifest.is_file():
        return
    from datetime import timedelta

    from ohlc_loader_v1 import _iso, _parse_utc, load_5m_ohlc_ch
    from validation_config_v1 import REFERENCE_WARMUP_HOURS

    lo = _parse_utc("2026-08-01T00:00:00Z") - timedelta(hours=REFERENCE_WARMUP_HOURS + 48)
    keys, prices, _ = load_5m_ohlc_ch(_iso(lo), "2026-09-30T00:00:00Z")
    v1 = PureEmaSeriesProviderV1(bar_keys=keys, prices=prices, classifier_version="v1")
    with chart.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            got = v1.confirm_at_close(r["decision_close_utc"])["ema_structure_decision"]
            assert got == r["ema_structure_decision"]
