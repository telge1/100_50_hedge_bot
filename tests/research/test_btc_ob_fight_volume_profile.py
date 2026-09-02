"""Tests for causal volume-at-price profile pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from orderbook_analyse.market_profile.contracts import ProfileBin
from orderbook_analyse.market_profile.profile import compute_value_area

from research.btc_ob_fight.contracts import FORBIDDEN_REASON_CODES
from research.btc_ob_fight.formatting import json_safe
from research.btc_ob_fight.factual_reasons import derive_factual_reason_codes
from research.btc_ob_fight.profiles import anchor_profile_facts
from research.btc_ob_fight.volume_profile import (
    PRIMARY_VOLUME_BASIS,
    _aggregate_trades_to_bins,
    build_volume_profile_from_trades,
    profile_session_window,
    verify_prefix_parity,
)


def _trade(ts: datetime, price: float, size: float, side: str, tid: str) -> dict:
    return {
        "ts": ts,
        "trade_id": tid,
        "side": side,
        "price": price,
        "size": size,
        "notional": price * size,
    }


def _mock_cl():
    return object()


def _mock_tpo(*, poc=79010.0, vah=79100.0, val=78900.0, status="COMPUTED_SEPARATELY"):
    return {
        "tpo_profile_status": status,
        "tpoc": {"tpoc_price": poc},
        "value_area": {"tpoc_vah": vah, "tpoc_val": val, "actual_value_area_share": 0.71},
        "provenance": {"profile_kind": "TPO_BRACKET", "weighting": "DISTINCT_BRACKET_PRESENCE", "trade_size_used_as_weight": False},
        "rows": [{"price": poc, "tpo_count": 1}],
    }


@pytest.fixture(autouse=True)
def _patch_ohlc(monkeypatch):
    monkeypatch.setattr(
        "orderbook_analyse.market_profile.loader.fetch_window_ohlc",
        lambda *args, **kwargs: (78700.0, 79200.0, 78600.0, 79000.0),
    )


def test_no_trade_after_anchor():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a"),
        _trade(anchor + timedelta(seconds=1), 79000.0, 1.0, "Buy", "b"),
    ]
    vp = build_volume_profile_from_trades(
        trades,
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    assert vp["coverage"]["deduped_trade_rows_used"] == 1
    assert vp["integrity"]["checks"]["no_trade_after_anchor"] is True


def test_deterministic_binning():
    step = 100.0
    trades = [
        _trade(datetime(2026, 1, 1, tzinfo=timezone.utc), 79050.0, 1.0, "Buy", "1"),
        _trade(datetime(2026, 1, 1, tzinfo=timezone.utc), 79050.0, 1.0, "Buy", "2"),
    ]
    bins1, _ = _aggregate_trades_to_bins(trades, step)
    bins2, _ = _aggregate_trades_to_bins(trades, step)
    assert bins1[0].bin_index == bins2[0].bin_index == 790


def test_trade_on_bin_boundary():
    step = 100.0
    trades = [_trade(datetime(2026, 1, 1, tzinfo=timezone.utc), 79100.0, 1.0, "Buy", "1")]
    bins, _ = _aggregate_trades_to_bins(trades, step)
    assert bins[0].bin_index == 791


def test_negative_qty_blocked():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, -1.0, "Buy", "bad")]
    vp = build_volume_profile_from_trades(
        trades,
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    assert vp["volume_profile_status"] == "INTEGRITY_FAILED"


def test_conservation():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=i), 78900.0 + i, 0.5, "Buy" if i % 2 == 0 else "Sell", f"t{i}")
        for i in range(20)
    ]
    vp = build_volume_profile_from_trades(
        trades,
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    assert vp["integrity"]["status"] == "PASS"
    assert vp["integrity"]["checks"]["base_volume_conservation"] is True
    assert vp["integrity"]["checks"]["trade_count_conservation"] is True


def test_vpoc_tie_break():
    bins = [
        ProfileBin(0, 100.0, 110.0, 105.0, 10.0, 5.0, 5.0, 1, 1000.0),
        ProfileBin(1, 110.0, 120.0, 115.0, 10.0, 5.0, 5.0, 1, 1100.0),
    ]
    va = compute_value_area(bins, 0.70)
    assert va.poc_volume == 10.0


def test_value_area_reaches_target_share():
    bins = [
        ProfileBin(i, i * 10.0, (i + 1) * 10.0, i * 10.0 + 5.0, float(10 - i), 0.0, 0.0, 1, 0.0)
        for i in range(10)
    ]
    va = compute_value_area(bins, 0.70)
    assert va.volume_share >= 0.70


def test_empty_data_fails():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    vp = build_volume_profile_from_trades(
        [],
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    assert vp["volume_profile_status"] == "INTEGRITY_FAILED"


def test_json_no_nan():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "1")]
    vp = build_volume_profile_from_trades(
        trades,
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    text = json.dumps(json_safe(vp))
    assert "NaN" not in text and "Infinity" not in text


def test_separate_provenance():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "1")]
    vp = build_volume_profile_from_trades(
        trades,
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    assert vp["provenance"]["tpo_values_not_copied"] is True
    assert vp["provenance"]["engine"] == "research.btc_ob_fight.volume_profile"


def test_prefix_parity():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "1"),
        _trade(anchor + timedelta(minutes=5), 79000.0, 1.0, "Buy", "future"),
    ]
    result = verify_prefix_parity(
        trades,
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        value_area_pct=0.70,
        target_bins=160,
    )
    assert result["status"] == "PASS"


def test_volume_reason_codes_not_forbidden():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "1")]
    vp = build_volume_profile_from_trades(
        trades,
        session_start=start,
        anchor=anchor,
        cl=_mock_cl(),
        symbol="BTCUSDT",
        compute_prefix=False,
    )
    pf = anchor_profile_facts(
        anchor,
        79000.0,
        tpo_profile=_mock_tpo(poc=0, vah=2, val=0),
        volume_profile=vp,
    )
    codes = derive_factual_reason_codes(pf, [], {}, [], {}, vp)
    assert any(c["code"] == "VOLUME_PROFILE_COMPUTED_FROM_TRADES" for c in codes)
    assert not any(c["code"] in FORBIDDEN_REASON_CODES for c in codes)


def test_primary_volume_basis():
    assert PRIMARY_VOLUME_BASIS == "base_volume"


def test_session_start_inclusive():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start, 79000.0, 1.0, "Buy", "exact_start"),
        _trade(start + timedelta(minutes=1), 79010.0, 1.0, "Sell", "later"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["coverage"]["deduped_trade_rows_used"] == 2
    assert vp["coverage"]["min_trade_ts"] == vp["coverage"]["session_start_utc"]


def test_anchor_exclusive():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(anchor - timedelta(microseconds=1), 79000.0, 1.0, "Buy", "before"),
        _trade(anchor, 79000.0, 1.0, "Buy", "at_anchor"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["coverage"]["deduped_trade_rows_used"] == 1
    assert vp["coverage"]["max_trade_ts"] < vp["coverage"]["cutoff_utc"]


def test_trade_before_session_excluded():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start - timedelta(seconds=1), 79000.0, 1.0, "Buy", "early"),
        _trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "ok"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["coverage"]["deduped_trade_rows_used"] == 1


def test_dedup_by_trade_id():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "dup"),
        _trade(start + timedelta(minutes=2), 79000.0, 2.0, "Buy", "dup"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["coverage"]["raw_trade_rows_in_session"] == 2
    assert vp["coverage"]["dedup_removed_duplicates"] == 1
    assert vp["coverage"]["deduped_trade_rows_used"] == 1
    assert vp["integrity"]["checks"]["trade_count_conservation"] is True


def test_buy_sell_assignment():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 3.0, "Buy", "b"),
        _trade(start + timedelta(minutes=2), 79000.0, 2.0, "Sell", "s"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    row = next(r for r in vp["rows"] if r["base_volume"] > 0)
    assert row["taker_buy_base_volume"] == 3.0
    assert row["taker_sell_base_volume"] == 2.0


def test_quote_notional_conservation():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=i), 78900.0 + i * 10, 0.25, "Buy" if i % 2 else "Sell", f"t{i}")
        for i in range(12)
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["integrity"]["checks"]["quote_notional_conservation"] is True


def test_buy_sell_sum_equals_total():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 1.5, "Buy", "1"),
        _trade(start + timedelta(minutes=2), 79050.0, 2.5, "Sell", "2"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["integrity"]["checks"]["buy_sell_sum_equals_total"] is True


def test_delta_calculation():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 4.0, "Buy", "1"),
        _trade(start + timedelta(minutes=2), 79000.0, 1.0, "Sell", "2"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    row = next(r for r in vp["rows"] if r["base_volume"] == 5.0)
    assert row["delta_base_volume"] == 3.0
    assert vp["integrity"]["checks"]["delta_equals_buy_minus_sell"] is True


def test_unique_vpoc():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 5.0, "Buy", "1"),
        _trade(start + timedelta(minutes=2), 79100.0, 1.0, "Buy", "2"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["vpoc"]["vpoc_tie_count"] == 1


def test_vpoc_tie_break_center_bin():
    bins = [
        ProfileBin(0, 100.0, 110.0, 105.0, 10.0, 5.0, 5.0, 1, 1000.0),
        ProfileBin(1, 110.0, 120.0, 115.0, 10.0, 5.0, 5.0, 1, 1100.0),
        ProfileBin(2, 120.0, 130.0, 125.0, 10.0, 5.0, 5.0, 1, 1200.0),
    ]
    va = compute_value_area(bins, 0.70)
    assert va.poc_bin_index == 1


def test_value_area_tie_break_expansion():
    bins = [
        ProfileBin(0, 100.0, 110.0, 105.0, 5.0, 0.0, 0.0, 1, 0.0),
        ProfileBin(1, 110.0, 120.0, 115.0, 10.0, 0.0, 0.0, 1, 0.0),
        ProfileBin(2, 120.0, 130.0, 125.0, 4.0, 0.0, 0.0, 1, 0.0),
    ]
    va = compute_value_area(bins, 0.70)
    assert va.volume_share >= 0.70
    assert va.poc_bin_index == 1


def test_single_bin_profile():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 2.0, "Buy", "only")]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["volume_profile_status"] == "COMPUTED_SEPARATELY"
    assert vp["integrity"]["checks"]["value_area_level_order"] is True
    assert sum(1 for r in vp["rows"] if r["base_volume"] > 0) >= 1


def test_row_trade_timestamps():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    t1 = start + timedelta(minutes=1)
    t2 = start + timedelta(minutes=5)
    trades = [
        _trade(t1, 79000.0, 1.0, "Buy", "1"),
        _trade(t2, 79000.0, 1.0, "Sell", "2"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    row = next(r for r in vp["rows"] if r["trade_count"] == 2)
    assert row["first_trade_ts"] == t1.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert row["last_trade_ts"] == t2.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert row["last_trade_ts"] < anchor.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_rows_sorted_ascending():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=i), 78900.0 + i * 50, 1.0, "Buy", f"t{i}")
        for i in range(5)
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    ticks = [r["price_bin_tick"] for r in vp["rows"]]
    assert ticks == sorted(ticks)


def test_hvn_lvn_deterministic():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=i), 78800.0 + i * 20, 1.0 + (i % 3), "Buy", f"t{i}")
        for i in range(30)
    ]
    vp1 = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    vp2 = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp1["hvn_candidates"] == vp2["hvn_candidates"]
    assert vp1["lvn_candidates"] == vp2["lvn_candidates"]
    assert all(n.get("status") == "UNFROZEN_HEURISTIC" for n in vp1["hvn_candidates"] + vp1["lvn_candidates"])


def test_integrity_failure_status():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    vp = build_volume_profile_from_trades(
        [], session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp["volume_profile_status"] == "INTEGRITY_FAILED"
    assert vp["integrity"]["status"] == "FAIL"


def test_future_trade_invariance():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    base = [
        _trade(start + timedelta(minutes=i), 79000.0 + i, 1.0, "Buy", f"b{i}")
        for i in range(10)
    ]
    future = [
        _trade(anchor + timedelta(minutes=j), 79500.0, 10.0, "Sell", f"f{j}")
        for j in range(20)
    ]
    vp_base = build_volume_profile_from_trades(
        base, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    vp_ext = build_volume_profile_from_trades(
        base + future, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert vp_base["vpoc"] == vp_ext["vpoc"]
    assert vp_base["value_area"] == vp_ext["value_area"]


def test_oa_parity_classification(monkeypatch):
    from research.btc_ob_fight.volume_profile import compare_with_oa_profile

    class FakeVA:
        poc = 79000.0
        vah = 79100.0
        val = 78900.0

    class FakeProf:
        value_area = FakeVA()
        price_step = 50.0

    monkeypatch.setattr(
        "orderbook_analyse.market_profile.build.build_profile",
        lambda *a, **k: FakeProf(),
    )
    monkeypatch.setattr(
        "orderbook_analyse.market_profile.anchor.build_windows",
        lambda **k: [object()],
    )
    local = {
        "vpoc": {"vpoc_price": 79000.0},
        "value_area": {"vvah": 79100.0, "vval": 78900.0},
        "provenance": {"price_increment": 50.0},
    }
    result = compare_with_oa_profile(
        object(),
        "BTCUSDT",
        datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        local,
    )
    assert result["status"] == "EXACT"


def test_volume_levels_in_episode_engine():
    from research.btc_ob_fight.level_events import compute_level_events
    from research.btc_ob_fight.volume_profile import volume_anchor_levels

    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 78900.0, 1.0, "Buy", "1"),
        _trade(start + timedelta(minutes=2), 79050.0, 1.0, "Buy", "2"),
        _trade(start + timedelta(minutes=3), 79150.0, 1.0, "Buy", "3"),
    ]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    levels = volume_anchor_levels(vp)
    ids = {lvl["level_id"] for lvl in levels}
    assert "VOLUME_VPOC" in ids
    assert "VOLUME_VVAH" in ids
    assert "VOLUME_VVAL" in ids
    window_start = anchor - timedelta(minutes=30)
    window_end = anchor + timedelta(minutes=30)
    events = compute_level_events(trades, levels, window_start, window_end, anchor=anchor)
    assert any(e["level_id"] == "VOLUME_VVAH" for e in events)


def test_confluence_invalid_reference_levels():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    pf = anchor_profile_facts(
        anchor,
        79000.0,
        tpo_profile=_mock_tpo(poc=0, vah=79050, val=78950),
        volume_profile={
            "volume_profile_status": "COMPUTED_SEPARATELY",
            "vpoc": {"vpoc_price": 79000.0},
            "value_area": {"vvah": 79100.0, "vval": 78900.0},
            "rows": [],
        },
    )
    poc_conf = next(c for c in pf["tpo_volume_level_confluence"] if c["tpo_kind"] == "poc")
    assert poc_conf["evaluation_status"] == "INVALID_OR_MISSING_REFERENCE_LEVEL"
    assert poc_conf["distance_bps"] is None
    text = json.dumps(json_safe(pf))
    assert "NaN" not in text and "Infinity" not in text


def test_confluence_valid_levels_evaluated():
    pf = anchor_profile_facts(
        datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        79000.0,
        tpo_profile=_mock_tpo(poc=79010, vah=79100, val=78900),
        volume_profile={
            "volume_profile_status": "COMPUTED_SEPARATELY",
            "vpoc": {"vpoc_price": 79020.0},
            "value_area": {"vvah": 79150.0, "vval": 78850.0},
            "rows": [{"price": 79020.0}],
        },
    )
    poc_conf = next(c for c in pf["tpo_volume_level_confluence"] if c["tpo_kind"] == "poc")
    assert poc_conf["evaluation_status"] == "EVALUATED"
    assert poc_conf["distance_bps"] is not None


def test_not_separately_computed_without_volume():
    pf = anchor_profile_facts(
        datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        79000.0,
        tpo_profile=_mock_tpo(),
    )
    assert pf["volume_profile_status"] == "NOT_SEPARATELY_COMPUTED"
    assert pf["volume_poc"] is None


def test_volume_profile_computed_separately_status():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "1")]
    vp = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    pf = anchor_profile_facts(anchor, 79000.0, tpo_profile=_mock_tpo(), volume_profile=vp)
    assert pf["volume_profile_status"] == "COMPUTED_SEPARATELY"


def test_report_mentions_separate_computation():
    from research.btc_ob_fight.templates_de import render_all_german

    reasons = [
        {
            "code": "VOLUME_PROFILE_COMPUTED_FROM_TRADES",
            "fields": {
                "deduped_trade_rows_used": 100,
                "session_start_utc": "2026-08-31T13:30:00Z",
                "cutoff_utc": "2026-08-31T19:00:00Z",
            },
        }
    ]
    german = render_all_german(reasons)
    combined = " ".join(g.get("text_de", "") for g in german)
    assert "separat" in combined.lower()
    assert "kopiert" not in combined.lower()


def test_deterministic_repeat_run():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start, _, _ = profile_session_window(anchor)
    trades = [
        _trade(start + timedelta(minutes=i), 78900.0 + i * 7, 0.3, "Buy" if i % 2 else "Sell", f"x{i}")
        for i in range(25)
    ]
    vp1 = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    vp2 = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert json.dumps(json_safe(vp1["vpoc"])) == json.dumps(json_safe(vp2["vpoc"]))
    assert json.dumps(json_safe(vp1["value_area"])) == json.dumps(json_safe(vp2["value_area"]))
    assert vp1["rows"] == vp2["rows"]


def test_confluence_non_finite_level():
    pf = anchor_profile_facts(
        datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        79000.0,
        tpo_profile=_mock_tpo(poc=float("inf"), vah=79100, val=78900),
        volume_profile={
            "volume_profile_status": "COMPUTED_SEPARATELY",
            "vpoc": {"vpoc_price": 79000.0},
            "value_area": {"vvah": 79100.0, "vval": 78900.0},
            "rows": [],
        },
    )
    poc_conf = next(c for c in pf["tpo_volume_level_confluence"] if c["tpo_kind"] == "poc")
    assert poc_conf["evaluation_status"] == "INVALID_OR_MISSING_REFERENCE_LEVEL"


def test_cli_integrity_failure_exit(tmp_path, monkeypatch):
    from research.btc_ob_fight.cli import run_analysis
    from research.btc_ob_fight.config import RunConfig, resolve_ob_root

    ob_root = resolve_ob_root()
    if ob_root is None:
        pytest.skip("OB root unavailable")
    cfg = RunConfig(
        symbol="BTCUSDT",
        anchor=datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        before_minutes=30,
        after_minutes=30,
        ob_root=ob_root,
        out_root=tmp_path,
    )

    def _fail_volume(*args, **kwargs):
        return {
            "volume_profile_status": "INTEGRITY_FAILED",
            "integrity": {"status": "FAIL"},
            "contract_version": "volume_profile_facts_v1",
            "provenance": {},
            "coverage": {},
            "vpoc": {},
            "value_area": {},
            "rows": [],
            "future_trade_count_used": 0,
        }

    monkeypatch.setattr("research.btc_ob_fight.cli.build_volume_profile_from_trades", _fail_volume)
    monkeypatch.setattr("research.btc_ob_fight.cli.compare_with_oa_profile", lambda *a, **k: {"status": "SKIP"})
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.clickhouse_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.coverage_public_trades",
        lambda *a, **k: {"count": 100},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.coverage_candles",
        lambda *a, **k: {"complete": True},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.coverage_open_interest",
        lambda *a, **k: {"count": 1},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.coverage_liquidations",
        lambda *a, **k: {"count": 0},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.load_public_trades",
        lambda *a, **k: ([], {"deduped_count": 0}),
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.load_open_interest",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.load_liquidations",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.build_tpo_profile_from_trades",
        lambda *a, **k: {
            "tpo_profile_status": "COMPUTED_SEPARATELY",
            "integrity": {"status": "PASS"},
            "contract_version": "tpo_profile_facts_v1",
            "provenance": {},
            "coverage": {},
            "tpoc": {"tpoc_price": 79000.0},
            "value_area": {"tpoc_vah": 79100.0, "tpoc_val": 78900.0},
            "rows": [],
            "bracket_rows": [],
            "future_trade_count_used": 0,
        },
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.verify_tpo_trade_size_invariance",
        lambda *a, **k: {"status": "PASS"},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.verify_tpo_prefix_parity",
        lambda *a, **k: {"status": "PASS"},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.build_session_profile_metadata",
        lambda *a, **k: {"profiles": {}},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.sample_ob_snapshots",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.build_wall_fact_pipeline",
        lambda *a, **k: {"legacy_wall_facts": []},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.audit_ob_coverage",
        lambda *a, **k: {"all_hours_ok": True},
    )
    monkeypatch.setattr(
        "research.btc_ob_fight.cli.replay_as_of",
        lambda *a, **k: {
            "as_of": cfg.anchor,
            "bid_levels": 200,
            "ask_levels": 200,
            "genuine_200": True,
            "segment": "mock",
        },
    )
    code = run_analysis(cfg)
    assert code == 3
    run_dirs = sorted((tmp_path / "btc_ob_fight_cases" / "20260831T190000Z").glob("run_*"))
    summary = json.loads((run_dirs[-1] / "summary.json").read_text())
    assert summary["analysis_status"] == "VOLUME_PROFILE_INTEGRITY_FAILED"
