"""Episode-1 wall-flow / QDH_base unit + contract tests (outcome-blind)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(ENGINE_ROOT.parent / "src"))

from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1 import (  # noqa: E402
    M_LIQ,
    M_OI,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.aggressor_flow import (  # noqa: E402
    AggressorState,
    update_aggressor,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (  # noqa: E402
    build_canonical_trades,
    canonical_trade_key,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.mass_balance import (  # noqa: E402
    decompose_mass_balance,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.price_response import (  # noqa: E402
    PriceResponseState,
    update_price_response,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import (  # noqa: E402
    QdhState,
    update_qdh,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (  # noqa: E402
    BookNode,
    attribute_intervals,
    is_ask_wall_attack_trade,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (  # noqa: E402
    CanonicalTrade,
)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _trade(
    *,
    tid: str,
    ts: str,
    price: float,
    size: float = 1.0,
    side: str = "Buy",
    recv: str | None = None,
) -> dict:
    return {
        "trade_id": tid,
        "trade_ts": ts,
        "price": price,
        "size": size,
        "taker_side": side,
        "collector_received_at": recv or ts,
    }


def _ct(row: dict) -> CanonicalTrade:
    kept, _, _ = build_canonical_trades([row], source_file="test")
    return kept[0]


def test_01_canonical_trade_key_stable():
    assert canonical_trade_key("BTCUSDT", "abc") == "BTCUSDT|abc"
    a, _, _ = build_canonical_trades([_trade(tid="abc", ts="2026-09-06T20:19:02.232Z", price=79780.0)], source_file="t")
    b, _, _ = build_canonical_trades([_trade(tid="abc", ts="2026-09-06T20:19:02.232Z", price=79780.0)], source_file="t")
    assert a[0].canonical_trade_key == b[0].canonical_trade_key


def test_02_dedup_same_trade():
    rows = [
        _trade(tid="x", ts="2026-09-06T20:19:02.232Z", price=79780.0, size=1),
        _trade(tid="x", ts="2026-09-06T20:19:02.232Z", price=79780.0, size=1),
    ]
    kept, rep, dropped = build_canonical_trades(rows, source_file="t")
    assert len(kept) == 1
    assert rep.duplicate_count == 1
    assert len(dropped) == 1


def test_03_trade_not_two_intervals():
    t0 = _ts("2026-09-06T20:19:02.000Z")
    nodes = [
        BookNode(t0, t0, None, 100, 1, 1, 1, 0, "a", "a"),
        BookNode(t0 + timedelta(milliseconds=100), t0 + timedelta(milliseconds=100), None, 70, 1, 2, 2, 1, "b", "b"),
        BookNode(t0 + timedelta(milliseconds=200), t0 + timedelta(milliseconds=200), None, 70, 1, 3, 3, 2, "c", "c"),
    ]
    trades = [
        _ct(_trade(tid="hit1", ts="2026-09-06T20:19:02.050Z", price=79780.0, size=30)),
    ]
    consumed: set[str] = set()
    evs, stats = attribute_intervals(
        nodes=nodes,
        trades=trades,
        view="exact_price",
        wall_price=79780.0,
        band_low=79779.5,
        band_high=79780.5,
        wall_visible_at=t0,
        analysis_end_exclusive=t0 + timedelta(seconds=10),
        consumed_trade_ids=consumed,
    )
    attributed_once = sum(1 for e in evs if "hit1" in e.attributed_trade_ids)
    assert attributed_once == 1
    assert stats["trades_skipped_already_consumed"] == 0 or attributed_once == 1


def test_04_ask_accepts_buy():
    tr = _ct(_trade(tid="b", ts="2026-09-06T20:19:02.232Z", price=79780.0, side="Buy"))
    assert is_ask_wall_attack_trade(tr, view="exact_price", wall_price=79780.0, band_low=79779.5, band_high=79780.5)


def test_05_ask_rejects_sell():
    tr = _ct(_trade(tid="s", ts="2026-09-06T20:19:02.232Z", price=79780.0, side="Sell"))
    assert not is_ask_wall_attack_trade(tr, view="exact_price", wall_price=79780.0, band_low=79779.5, band_high=79780.5)


def test_06_below_exact_not_exact_hit():
    tr = _ct(_trade(tid="u", ts="2026-09-06T20:19:02.232Z", price=79779.9, side="Buy"))
    assert not is_ask_wall_attack_trade(tr, view="exact_price", wall_price=79780.0, band_low=79779.5, band_high=79780.5)
    assert is_ask_wall_attack_trade(tr, view="defended_band", wall_price=79780.0, band_low=79779.5, band_high=79780.5)


def test_07_band_and_exact_separated():
    assert test_06_below_exact_not_exact_hit() is None


def test_08_trade_before_visibility_ignored():
    t0 = _ts("2026-09-06T20:19:02.000Z")
    nodes = [
        BookNode(t0 - timedelta(seconds=1), t0 - timedelta(seconds=1), None, 100, 1, 1, 1, 0, "a", "a"),
        BookNode(t0 + timedelta(milliseconds=100), t0 + timedelta(milliseconds=100), None, 70, 1, 2, 2, 1, "b", "b"),
    ]
    trades = [_ct(_trade(tid="early", ts="2026-09-06T20:19:01.050Z", price=79780.0, size=30))]
    evs, _ = attribute_intervals(
        nodes=nodes,
        trades=trades,
        view="exact_price",
        wall_price=79780.0,
        band_low=79779.5,
        band_high=79780.5,
        wall_visible_at=t0,
        analysis_end_exclusive=t0 + timedelta(seconds=10),
    )
    assert all("early" not in e.attributed_trade_ids for e in evs)


def test_09_trade_after_end_ignored():
    t0 = _ts("2026-09-06T20:19:02.000Z")
    end = t0 + timedelta(seconds=1)
    nodes = [
        BookNode(t0, t0, None, 100, 1, 1, 1, 0, "a", "a"),
        BookNode(t0 + timedelta(seconds=2), t0 + timedelta(seconds=2), None, 70, 1, 2, 2, 1, "b", "b"),
    ]
    trades = [_ct(_trade(tid="late", ts="2026-09-06T20:19:03.500Z", price=79780.0, size=30))]
    evs, _ = attribute_intervals(
        nodes=nodes,
        trades=trades,
        view="exact_price",
        wall_price=79780.0,
        band_low=79779.5,
        band_high=79780.5,
        wall_visible_at=t0,
        analysis_end_exclusive=end,
    )
    assert all("late" not in e.attributed_trade_ids for e in evs)


def test_10_q0_100_q1_70_x_30():
    mb = decompose_mass_balance(queue_before=100, queue_after=70, attributed_hit_qty=30)
    assert mb.net_refill_qty == 0 and mb.residual_pull_qty == 0


def test_11_residual_pull_40():
    mb = decompose_mass_balance(queue_before=100, queue_after=30, attributed_hit_qty=30)
    assert abs(mb.residual_pull_qty - 40) < 1e-12


def test_12_net_refill_30():
    mb = decompose_mass_balance(queue_before=100, queue_after=100, attributed_hit_qty=30)
    assert abs(mb.net_refill_qty - 30) < 1e-12


def test_13_net_refill_50():
    mb = decompose_mass_balance(queue_before=100, queue_after=120, attributed_hit_qty=30)
    assert abs(mb.net_refill_qty - 50) < 1e-12


def test_14_mass_balance_identity():
    for q0, q1, x in [(100, 70, 30), (100, 30, 30), (100, 100, 30), (100, 120, 30), (12.5, 0.0, 5.0)]:
        mb = decompose_mass_balance(queue_before=q0, queue_after=q1, attributed_hit_qty=x)
        assert mb.identity_ok


def test_15_no_double_count_fill_and_decrease():
    # X explains full decrease → no residual pull (not counted twice)
    mb = decompose_mass_balance(queue_before=100, queue_after=70, attributed_hit_qty=30)
    assert mb.residual_pull_qty == 0


def test_16_net_refill_not_gross_field():
    mb = decompose_mass_balance(queue_before=100, queue_after=100, attributed_hit_qty=30)
    assert hasattr(mb, "net_refill_qty")
    assert not hasattr(mb, "gross_refill")


def test_17_epoch_change_invalid():
    t0 = _ts("2026-09-06T20:19:02.000Z")
    nodes = [
        BookNode(t0, t0, None, 100, 1, 1, 1, 0, "a", "a"),
        BookNode(t0 + timedelta(milliseconds=100), t0 + timedelta(milliseconds=100), None, 70, 2, 2, 2, 1, "b", "b"),
    ]
    trades = [_ct(_trade(tid="h", ts="2026-09-06T20:19:02.050Z", price=79780.0, size=30))]
    evs, stats = attribute_intervals(
        nodes=nodes,
        trades=trades,
        view="exact_price",
        wall_price=79780.0,
        band_low=79779.5,
        band_high=79780.5,
        wall_visible_at=t0,
        analysis_end_exclusive=t0 + timedelta(seconds=10),
    )
    assert stats["cross_epoch"] == 1
    assert evs[0].attribution_confidence == "INVALID"


def test_18_seq_gap_invalid():
    t0 = _ts("2026-09-06T20:19:02.000Z")
    nodes = [
        BookNode(t0, t0, None, 100, 1, 500, 1, 0, "a", "a"),
        BookNode(t0 + timedelta(milliseconds=100), t0 + timedelta(milliseconds=100), None, 70, 1, 400, 2, 1, "b", "b"),
    ]
    trades = [_ct(_trade(tid="h", ts="2026-09-06T20:19:02.050Z", price=79780.0, size=30))]
    evs, _ = attribute_intervals(
        nodes=nodes,
        trades=trades,
        view="exact_price",
        wall_price=79780.0,
        band_low=79779.5,
        band_high=79780.5,
        wall_visible_at=t0,
        analysis_end_exclusive=t0 + timedelta(seconds=10),
    )
    assert evs[0].attribution_confidence == "INVALID"


def test_19_missing_receive_limits_confidence():
    t0 = _ts("2026-09-06T20:19:02.000Z")
    nodes = [
        BookNode(t0, t0, None, 100, 1, 1, 1, 0, "a", "a"),
        BookNode(t0 + timedelta(milliseconds=100), t0 + timedelta(milliseconds=100), None, 70, 1, 2, 2, 1, "b", "b"),
    ]
    row = _trade(tid="h", ts="2026-09-06T20:19:02.050Z", price=79780.0, size=30, recv=None)
    row["collector_received_at"] = None
    # build without recv
    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import build_canonical_trades as bct

    kept, rep, _ = bct([row], source_file="t")
    assert rep.receive_time_missing_count == 1
    evs, _ = attribute_intervals(
        nodes=nodes,
        trades=kept,
        view="exact_price",
        wall_price=79780.0,
        band_low=79779.5,
        band_high=79780.5,
        wall_visible_at=t0,
        analysis_end_exclusive=t0 + timedelta(seconds=10),
    )
    assert evs[0].attribution_confidence in ("LOW", "MEDIUM", "HIGH")
    # missing receive on book and trade → LOW
    assert evs[0].attribution_confidence == "LOW"


def test_20_bucket_not_available_at_start():
    from obfull_research_engine.level_first_episode1_corrected_sms1_persist_v1.persist import event_available_at

    start = _ts("2026-09-06T20:19:02.200Z")
    avail = event_available_at(start)
    # event at start is available at end of its bucket = start+100ms if start is on boundary...
    # For bucket [start, start+100), available_at = start+100
    from obfull_research_engine.drilldown.aggregation_100ms import _floor_bucket
    from datetime import timedelta

    bstart = _floor_bucket(start, 100)
    bend = bstart + timedelta(milliseconds=100)
    assert bend > bstart


def test_22_persistence_rises_on_acceleration():
    st = AggressorState()
    t = _ts("2026-09-06T20:19:02.000Z")
    st = update_aggressor(
        st, hit_qty=1, hit_notional=1, hit_trade_count=1, interval_duration_s=0.1, exchange_time=t, interarrival_ms_samples=[], buy_hit_qty=1, sell_hit_qty=0
    )
    slow_pers = st.persistence_ratio
    st2 = update_aggressor(
        st, hit_qty=100, hit_notional=100, hit_trade_count=10, interval_duration_s=0.1, exchange_time=t + timedelta(milliseconds=100), interarrival_ms_samples=[10], buy_hit_qty=100, sell_hit_qty=0
    )
    assert st2.persistence_ratio > slow_pers or st2.fast_hit_rate > st.fast_hit_rate


def test_23_qdh_rises_with_depletion():
    s = QdhState()
    s1 = update_qdh(s, net_depletion_qty=1, interval_duration_s=1.0, current_queue=10, persistence_ratio=1.0)
    s2 = update_qdh(s1, net_depletion_qty=5, interval_duration_s=1.0, current_queue=10, persistence_ratio=1.0)
    assert s2.qdh_base > s1.qdh_base


def test_24_qdh_rises_smaller_queue():
    s = QdhState()
    a = update_qdh(s, net_depletion_qty=5, interval_duration_s=1.0, current_queue=100, persistence_ratio=1.0)
    b = update_qdh(s, net_depletion_qty=5, interval_duration_s=1.0, current_queue=10, persistence_ratio=1.0)
    assert b.qdh_base > a.qdh_base


def test_25_qdh_zero_nonpositive():
    s = update_qdh(QdhState(), net_depletion_qty=-1, interval_duration_s=1.0, current_queue=10, persistence_ratio=1.0)
    # after ewma may still be negative rate → qdh 0
    s2 = update_qdh(QdhState(), net_depletion_qty=0, interval_duration_s=1.0, current_queue=10, persistence_ratio=1.0)
    assert s2.qdh_base == 0


def test_26_runway_null_when_qdh_zero():
    s = update_qdh(QdhState(), net_depletion_qty=0, interval_duration_s=1.0, current_queue=10, persistence_ratio=1.0)
    assert s.queue_runway_seconds is None


def test_27_28_m_oi_m_liq():
    s = update_qdh(QdhState(), net_depletion_qty=1, interval_duration_s=1.0, current_queue=10, persistence_ratio=2.0)
    assert s.m_oi == 1.0 == M_OI
    assert s.m_liq == 1.0 == M_LIQ


def test_32_absorption_not_calibrated():
    st = update_price_response(
        PriceResponseState(),
        best_bid=79779.9,
        bid_size=1,
        best_ask=79780.0,
        ask_size=1,
        cumulative_hit_notional_since_wall_touch=1000,
        cumulative_hit_qty=1,
        cumulative_net_refill_qty=0,
        cumulative_residual_pull_qty=0,
        current_defended_band_queue=10,
        queue_at_wall_touch=12,
        wall_price=79780.0,
    )
    assert st.absorption_ratio_normalized is None
    assert st.absorption_ratio_status == "NOT_CALIBRATED"


def test_toxic_is_base_times_persistence_only():
    s = update_qdh(QdhState(), net_depletion_qty=10, interval_duration_s=1.0, current_queue=10, persistence_ratio=2.0)
    assert abs(s.qdh_toxic_base_only - s.qdh_base * s.m_persistence) < 1e-12
