"""Tests for genuine 30-minute bracket TPO profile."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from research.btc_ob_fight.formatting import json_safe
from research.btc_ob_fight.profiles import anchor_profile_facts
from research.btc_ob_fight.tpo_profile import (
    DEFAULT_BRACKET_MINUTES,
    TPO_PROFILE_CONTRACT,
    assess_tpo_volume_independence,
    build_tpo_profile_from_trades,
    price_to_bin_index,
    tpo_anchor_levels,
    tpo_provenance_contract,
    verify_tpo_prefix_parity,
    verify_tpo_trade_size_invariance,
)
from research.btc_ob_fight.volume_profile import build_volume_profile_from_trades, profile_session_window


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


@pytest.fixture(autouse=True)
def _patch_ohlc(monkeypatch):
    monkeypatch.setattr(
        "orderbook_analyse.market_profile.loader.fetch_window_ohlc",
        lambda *args, **kwargs: (78700.0, 79200.0, 78600.0, 79000.0),
    )


def _session_start(anchor: datetime) -> datetime:
    start, _, _ = profile_session_window(anchor)
    return start


def test_brackets_aligned_to_session_start():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=5), 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    brackets = tpo["bracket_rows"]
    assert brackets[0]["bracket_start"].startswith(start.strftime("%Y-%m-%dT%H:%M:%S"))


def test_30m_bracket_boundary():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    t0 = start + timedelta(minutes=29, seconds=59)
    t1 = start + timedelta(minutes=30, seconds=1)
    trades = [
        _trade(t0, 78000.0, 1.0, "Buy", "a"),
        _trade(t1, 79000.0, 1.0, "Buy", "b"),
    ]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert len(tpo["bracket_rows"]) >= 2
    assert tpo["bracket_rows"][0]["trade_count"] == 1
    assert tpo["bracket_rows"][1]["trade_count"] == 1


def test_anchor_exclusive():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a"),
        _trade(anchor, 79000.0, 1.0, "Buy", "b"),
    ]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert tpo["integrity"]["checks"]["no_trade_after_anchor"] is True
    assert tpo["bracket_rows"][0]["trade_count"] == 1


def test_session_start_inclusive():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start, 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert tpo["bracket_rows"][0]["trade_count"] == 1


def test_partial_bracket_causal():
    anchor = datetime(2026, 8, 31, 13, 45, tzinfo=timezone.utc)
    start = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
    trades = [
        _trade(start + timedelta(minutes=5), 78000.0, 1.0, "Buy", "a"),
        _trade(start + timedelta(minutes=14), 79000.0, 1.0, "Buy", "b"),
    ]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    last = tpo["bracket_rows"][-1]
    assert last["is_partial"] is True
    assert last["observed_until"] == anchor.strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    assert last["trade_count"] == 2


def test_future_trades_do_not_change_tpo():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a")]
    base = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    extended = trades + [
        _trade(anchor + timedelta(hours=1), 99999.0, 100.0, "Buy", "future"),
    ]
    again = build_tpo_profile_from_trades(
        extended, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert base["rows"] == again["rows"]
    assert base["tpoc"]["tpoc_price"] == again["tpoc"]["tpoc_price"]


def test_trade_size_does_not_change_tpo():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=i), 78900 + i * 10, 1.0, "Buy", f"t{i}") for i in range(5)]
    check = verify_tpo_trade_size_invariance(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert check["status"] == "PASS"


def test_trade_count_in_same_bin_does_not_change_tpo():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    one = [_trade(start + timedelta(minutes=1), 79005.0, 1.0, "Buy", "a")]
    many = [
        _trade(start + timedelta(minutes=1, seconds=i), 79005.0, 1.0, "Buy", f"a{i}")
        for i in range(20)
    ]
    t1 = build_tpo_profile_from_trades(one, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT")
    t2 = build_tpo_profile_from_trades(many, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT")
    assert t1["rows"] == t2["rows"]


def test_same_bin_once_per_bracket():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [
        _trade(start + timedelta(minutes=1, seconds=i), 79000.0 + i * 0.01, 1.0, "Buy", f"t{i}")
        for i in range(10)
    ]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    row = next(r for r in tpo["rows"] if r["tpo_count"] > 0)
    assert row["tpo_count"] == 1
    assert row["bracket_count"] == 1


def test_low_high_bins_inclusive():
    step = 10.0
    assert price_to_bin_index(79005.0, step) == price_to_bin_index(79009.9, step)
    lo = price_to_bin_index(79005.0, step)
    hi = price_to_bin_index(79025.0, step)
    assert hi - lo == 2


def test_integer_decimal_binning():
    step = 10.0
    idx = price_to_bin_index(79009.999999, step)
    assert idx == 7900


def test_poc_unique_synthetic():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = []
    for bi, minutes in enumerate([5, 35, 65]):
        for p in (78000 + bi * 100, 78500):
            trades.append(_trade(start + timedelta(minutes=minutes), p, 1.0, "Buy", f"{bi}-{p}"))
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert tpo["tpoc"]["tpoc_price"] is not None


def test_deterministic_poc_tie_break():
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.profile import compute_value_area

    step = 10.0
    bins = []
    for idx, cnt in ((100, 5.0), (101, 5.0), (102, 5.0)):
        lo = idx * step
        bins.append(
            ProfileBin(
                bin_index=idx,
                price_low=lo,
                price_high=lo + step,
                price_mid=lo + step / 2,
                volume=cnt,
                buy_volume=0,
                sell_volume=0,
                trades=0,
                notional=0,
            )
        )
    va = compute_value_area(bins, 0.70)
    assert va.poc_bin_index == 101


def test_value_area_70_percent():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = []
    for i in range(11):
        trades.append(_trade(start + timedelta(minutes=i * 30 + 1), 78000 + i * 50, 1.0, "Buy", f"b{i}"))
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    share = tpo["value_area"]["actual_value_area_share"]
    assert share >= 0.70 - 1e-9


def test_value_area_tie_break_deterministic():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=i * 30 + 1), 79000.0, 1.0, "Buy", f"t{i}") for i in range(6)]
    a = build_tpo_profile_from_trades(trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT")
    b = build_tpo_profile_from_trades(trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT")
    assert a["value_area"] == b["value_area"]


def test_tpo_value_area_share_field():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert tpo["value_area"]["actual_value_area_share"] >= 0.70 - 1e-9


def test_empty_profile_insufficient():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    tpo = build_tpo_profile_from_trades([], session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT")
    assert tpo["tpo_profile_status"] == "TPO_PROFILE_DATA_INSUFFICIENT"


def test_missing_bracket_zero_trades():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=65), 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    empty = [b for b in tpo["bracket_rows"] if b["trade_count"] == 0]
    assert empty


def test_partial_bracket_metadata():
    anchor = datetime(2026, 8, 31, 13, 45, tzinfo=timezone.utc)
    start = datetime(2026, 8, 31, 13, 30, tzinfo=timezone.utc)
    trades = [_trade(start + timedelta(minutes=10), 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    partial = [b for b in tpo["bracket_rows"] if b["is_partial"]]
    assert len(partial) == 1
    assert partial[0]["bracket_end_contract"] != partial[0]["observed_until"]


def test_row_sort_ascending():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a"),
        _trade(start + timedelta(minutes=31), 79100.0, 1.0, "Buy", "b"),
    ]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    prices = [r["price_bin_index"] for r in tpo["rows"]]
    assert prices == sorted(prices)


def test_json_no_nan_infinity():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    text = json.dumps(json_safe(tpo))
    assert "NaN" not in text and "Infinity" not in text


def test_tpo_provenance_contract():
    prov = tpo_provenance_contract()
    assert prov["profile_kind"] == "TPO_BRACKET"
    assert prov["weighting"] == "DISTINCT_BRACKET_PRESENCE"
    assert prov["trade_size_used_as_weight"] is False
    assert prov["bracket_minutes"] == DEFAULT_BRACKET_MINUTES


def test_no_oa_volume_fallback():
    import research.btc_ob_fight.tpo_profile as mod

    src = open(mod.__file__, encoding="utf-8").read()
    assert "build_profile" not in src
    assert "fetch_volume_at_price" not in src


def test_synthetic_tpo_poc_ne_volume_poc():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [
        _trade(start + timedelta(minutes=1), 78000.0, 100.0, "Buy", "heavy-low"),
        _trade(start + timedelta(minutes=31), 79000.0, 0.01, "Buy", "b1"),
        _trade(start + timedelta(minutes=61), 79000.0, 0.01, "Buy", "b2"),
    ]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    vol = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert tpo["tpoc"]["tpoc_price"] != vol["vpoc"]["vpoc_price"]


def test_volume_profile_unchanged_by_tpo():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=i), 78900 + i * 7, 0.3, "Buy", f"x{i}") for i in range(25)]
    before = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    build_tpo_profile_from_trades(trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT")
    after = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    assert before["vpoc"] == after["vpoc"]


def test_confluence_valid_independent_measures():
    tpo = {
        "tpo_profile_status": "COMPUTED_SEPARATELY",
        "tpoc": {"tpoc_price": 78545.0},
        "provenance": tpo_provenance_contract(),
        "rows": [{"price": 1}],
    }
    vol = {
        "volume_profile_status": "COMPUTED_SEPARATELY",
        "vpoc": {"vpoc_price": 78565.0},
        "provenance": {"primary_volume_basis": "base_volume"},
        "rows": [{"price": 2}],
    }
    ind = assess_tpo_volume_independence(tpo, vol)
    assert ind["status"] == "VALID_INDEPENDENT_MEASURES"


def test_tpo_levels_in_episode_engine():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    levels = tpo_anchor_levels(tpo)
    ids = {lvl["level_id"] for lvl in levels}
    assert "TPO_POC" in ids
    assert "TPO_VAH" in ids
    assert "TPO_VAL" in ids


def test_console_report_separation():
    from research.btc_ob_fight.reporting import print_console_summary

    summary = {
        "anchor_timestamp_utc": "2026-08-31T19:00:00Z",
        "window": {},
        "symbol": "BTCUSDT",
        "schema_version": "btc_ob_fight_facts_v2_0",
        "data_quality": "PASS",
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "profile_facts": {
            "price_at_anchor": 79000,
            "tpo_poc": 78545,
            "tpo_vah": 79080,
            "tpo_val": 78230,
            "tpo_profile_status": "COMPUTED_SEPARATELY",
            "tpo_value_area_share": 0.71,
            "inside_tpo_value_area": True,
            "inside_volume_value_area": True,
            "nearest_tpo_levels": [{"kind": "poc", "price": 78545}],
            "nearest_volume_levels": [{"kind": "vpoc", "price": 78565}],
            "tpo_volume_confluence_status": "VALID_INDEPENDENT_MEASURES",
        },
        "tpo_profile": {"status": "COMPUTED_SEPARATELY", "bracket_minutes": 30, "full_brackets": 11, "partial_brackets": 0, "total_brackets": 11, "integrity": "PASS"},
        "volume_profile": {"status": "COMPUTED_SEPARATELY", "primary_volume_basis": "base_volume", "vpoc": 78565, "vvah": 79140, "vval": 78190, "value_area_share": 0.701, "integrity": "PASS"},
        "level_events": [],
        "trade_facts": {},
        "wall_summary": {},
        "oi_liquidation_facts": {},
    }
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        print_console_summary(summary, MagicMock(), {"profile_settings": {"tpo_bracket_minutes": 30}})
    out = buf.getvalue()
    assert "TPO PROFILE — 30m BRACKET PRESENCE" in out
    assert "VOLUME PROFILE — BASE VOLUME" in out
    assert "78545" in out
    assert "78565" in out


def test_chart_timeframe_invariance():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=i), 78900 + i * 5, 1.0, "Buy", f"t{i}") for i in range(10)]
    a = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    b = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert a == b


def test_deterministic_repeat():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=i * 30 + 1), 78000 + i * 100, 1.0, "Buy", f"b{i}") for i in range(5)]
    a = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    b = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert json.dumps(json_safe(a["tpoc"])) == json.dumps(json_safe(b["tpoc"]))
    assert a["rows"] == b["rows"]


def test_prefix_parity():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a")]
    check = verify_tpo_prefix_parity(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert check["status"] == "PASS"


def test_anchor_profile_facts_integration():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=i * 30 + 1), 79000.0, 1.0, "Buy", f"t{i}") for i in range(4)]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    vol = build_volume_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT", compute_prefix=False
    )
    pf = anchor_profile_facts(anchor, 79000.0, tpo_profile=tpo, volume_profile=vol)
    assert pf["tpo_volume_confluence_status"] == "VALID_INDEPENDENT_MEASURES"
    level_ids = {lvl["level_id"] for lvl in pf["all_anchor_levels"]}
    assert "TPO_POC" in level_ids
    assert "VOLUME_VPOC" in level_ids


def test_contract_version():
    anchor = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    start = _session_start(anchor)
    trades = [_trade(start + timedelta(minutes=1), 79000.0, 1.0, "Buy", "a")]
    tpo = build_tpo_profile_from_trades(
        trades, session_start=start, anchor=anchor, cl=_mock_cl(), symbol="BTCUSDT"
    )
    assert tpo["contract_version"] == TPO_PROFILE_CONTRACT
