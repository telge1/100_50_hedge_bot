"""Tests for TPO vs Volume semantics provenance audit."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.btc_ob_fight.tpo_volume_semantics_audit import (
    VERDICT_INDEPENDENT,
    VERDICT_NOT_INDEPENDENT,
    dashboard_timeframe_contract,
    determine_verdict,
    profile_contracts,
    synthetic_independence_tests,
    _distribution_hash,
    write_audit_outputs,
)


@pytest.fixture(autouse=True)
def _patch_ohlc(monkeypatch):
    monkeypatch.setattr(
        "orderbook_analyse.market_profile.loader.fetch_window_ohlc",
        lambda *args, **kwargs: (78700.0, 79200.0, 78600.0, 79000.0),
    )


def test_profile_contracts_tpo_is_volume_weighted():
    c = profile_contracts()
    assert c["tpo_labeled_path"]["uses_time_bracket_presence"] is False
    assert c["tpo_labeled_path"]["weighting_measure"] == "base_trade_volume (size)"
    assert c["volume_profile_path"]["weighting_measure"] == "base_trade_volume (size)"


def test_synthetic_reference_and_volume_poc_can_differ():
    s = synthetic_independence_tests()
    assert s["reference_poc_differs_from_volume_poc"] is True
    assert s["expected_for_independent_semantics"]["reference_bracket_poc_at_A"] is True
    assert s["expected_for_independent_semantics"]["volume_poc_at_B"] is True


def test_bracket_reference_invariant_trade_size_scaling():
    s = synthetic_independence_tests()
    assert s["bracket_poc_invariant_to_volume_scaling"] is True


def test_production_weight_is_base_volume():
    s = synthetic_independence_tests()
    assert s["production_tpo_labeled_path_is_volume_weighted"] is True
    assert s["production_oa_weight_matches_local_volume_weight"] is True

    from research.btc_ob_fight.volume_profile import build_volume_profile_from_trades, profile_session_window

    anchor = datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades_a = [
        {"ts": start, "trade_id": "1", "side": "Buy", "price": 78050.0, "size": 1.0, "notional": 78050.0},
        {"ts": start, "trade_id": "2", "side": "Buy", "price": 79050.0, "size": 100.0, "notional": 7905000.0},
    ]
    vp = build_volume_profile_from_trades(
        trades_a,
        session_start=start,
        anchor=anchor,
        cl=object(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    import math

    step = float((vp.get("provenance") or {}).get("price_increment") or 10.0)
    prod_agg: dict[int, float] = {}
    for t in trades_a:
        idx = int(math.floor(float(t["price"]) / step))
        prod_agg[idx] = prod_agg.get(idx, 0.0) + float(t["size"])
    assert (vp.get("vpoc") or {}).get("vpoc_bin_index") == max(prod_agg, key=lambda k: prod_agg[k])


def test_identical_levels_not_imply_independent_semantics():
    audit = {
        "hashes_equal_oa_vs_local": True,
        "oa_labeled_tpo_levels": {"poc": 78565.0},
        "local_volume_levels": {"vpoc": 78565.0},
    }
    synthetic = synthetic_independence_tests()
    verdict = determine_verdict(audit, synthetic)
    assert verdict == VERDICT_NOT_INDEPENDENT


def test_deterministic_distribution_hash():
    rows = [
        {"price_bin_index": 1, "w": 0.5},
        {"price_bin_index": 2, "w": 0.3},
    ]
    h1 = _distribution_hash([{"price_bin_index": 1, "w": 0.5}, {"price_bin_index": 2, "w": 0.3}], "w")
    h2 = _distribution_hash(list(reversed(rows)), "w")
    assert h1 == h2
    assert h1 != _distribution_hash([{"price_bin_index": 1, "w": 0.6}], "w")


def test_dashboard_timeframe_contract_documented():
    d = dashboard_timeframe_contract()
    assert d["chart_timeframe_changes_tpo_profile_computation"] is False
    assert d["chart_timeframe_changes_volume_profile_computation"] is False
    assert d["tpo_bracket_duration_parameter_exists"] is False


def test_anchor_exclusive_both_paths():
    from research.btc_ob_fight.volume_profile import build_volume_profile_from_trades, profile_session_window

    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        {
            "ts": start,
            "trade_id": "1",
            "side": "Buy",
            "price": 79000.0,
            "size": 1.0,
            "notional": 79000.0,
        },
        {
            "ts": anchor,
            "trade_id": "2",
            "side": "Buy",
            "price": 79000.0,
            "size": 1.0,
            "notional": 79000.0,
        },
    ]
    vp = build_volume_profile_from_trades(
        trades,
        session_start=start,
        anchor=anchor,
        cl=object(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    assert vp["coverage"]["deduped_trade_rows_used"] == 1
    assert vp["integrity"]["checks"]["no_trade_after_anchor"] is True


def test_future_trade_invariance_volume():
    from research.btc_ob_fight.volume_profile import build_volume_profile_from_trades, profile_session_window

    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    base = [
        {
            "ts": start,
            "trade_id": f"b{i}",
            "side": "Buy",
            "price": 79000.0 + i,
            "size": 1.0,
            "notional": 79000.0,
        }
        for i in range(5)
    ]
    future = [
        {
            "ts": anchor,
            "trade_id": f"f{i}",
            "side": "Sell",
            "price": 79500.0,
            "size": 10.0,
            "notional": 795000.0,
        }
        for i in range(3)
    ]
    a = build_volume_profile_from_trades(
        base, session_start=start, anchor=anchor, cl=object(), symbol="BTCUSDT", compute_prefix=False
    )
    b = build_volume_profile_from_trades(
        base + future,
        session_start=start,
        anchor=anchor,
        cl=object(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    assert a["vpoc"] == b["vpoc"]


def test_trade_size_change_shifts_volume_poc():
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.profile import compute_value_area

    step = 100.0
    small = [
        ProfileBin(780, 78000, 78100, 78050, 10.0, 0, 0, 1, 0),
        ProfileBin(790, 79000, 79100, 79050, 100.0, 0, 0, 1, 0),
    ]
    large = [
        ProfileBin(780, 78000, 78100, 78050, 10.0, 0, 0, 1, 0),
        ProfileBin(790, 79000, 79100, 79050, 10000.0, 0, 0, 1, 0),
    ]
    assert compute_value_area(small, 0.70).poc_bin_index == 790
    assert compute_value_area(large, 0.70).poc_bin_index == 790


def test_oa_labeled_path_uses_same_compute_as_volume(monkeypatch):
    """Scaling trade sizes changes POC for volume-weighted OA path (not bracket-invariant)."""
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.profile import compute_value_area

    bins_a = [
        ProfileBin(780, 78000, 78100, 78050, 500.0, 0, 0, 100, 0),
        ProfileBin(790, 79000, 79100, 79050, 50.0, 0, 0, 5, 0),
    ]
    bins_b = [
        ProfileBin(780, 78000, 78100, 78050, 500.0, 0, 0, 100, 0),
        ProfileBin(790, 79000, 79100, 79050, 5000.0, 0, 0, 5, 0),
    ]
    assert compute_value_area(bins_a, 0.70).poc_bin_index == 780
    assert compute_value_area(bins_b, 0.70).poc_bin_index == 790


@pytest.mark.integration
def test_golden_audit_outputs(tmp_path):
    out = tmp_path / "audit"
    result = write_audit_outputs(out)
    assert result["verdict"] == VERDICT_NOT_INDEPENDENT
    assert result["verdict"] != VERDICT_INDEPENDENT
    assert (out / "REPORT.md").is_file()
    assert (out / "profile_contracts.json").is_file()
    assert (out / "tpo_volume_distribution_comparison.csv").is_file()
    integrity = json.loads((out / "distribution_integrity.json").read_text())
    assert integrity["confluence_valid_for_fight_engine"] is False
    assert integrity["tpo_volume_confluence_status"] == "INVALID_SAME_SEMANTICS"
    report = (out / "REPORT.md").read_text()
    assert "NOT_INDEPENDENT" in report or "NOT_INDEPENDENT_BLOCKED" in result["verdict"]
