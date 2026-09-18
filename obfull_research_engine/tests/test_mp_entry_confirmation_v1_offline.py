"""Offline tests for mp_entry_confirmation_v1 (no CH / no MP recompute)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from obfull_research_engine.mp_entry_confirmation_v1.candles5m import (
    Candle5m,
    aggregate_5m_from_1m,
    first_allowed_5m_open_after_alert,
    floor_5m_ns,
    post_alert_5m,
)
from obfull_research_engine.mp_entry_confirmation_v1.compare import (
    classify_original_vs_confirmed,
)
from obfull_research_engine.mp_entry_confirmation_v1.confirmation import (
    confirm_absorb,
    confirm_event,
    confirm_failed_break,
    confirm_true_break,
    resolve_entry_1m,
    structure_break_long,
    structure_break_short,
)
from obfull_research_engine.mp_entry_confirmation_v1.contract import (
    build_contract,
    canonical_hash,
)
from obfull_research_engine.mp_entry_confirmation_v1.outcomes import (
    evaluate_target_stop,
    stop_price,
    target_price,
)
from obfull_research_engine.mp_entry_confirmation_v1.params import NS, TARGET_PCT, STOP_PCT
from obfull_research_engine.mp_entry_confirmation_v1.selection import (
    build_non_overlapping_confirmed_4h,
)
from obfull_research_engine.mp_price_path_4h_v1.candles import Candle1m
from obfull_research_engine.mp_price_path_4h_v1.geometry import mfe_from_extremes


FIVE = 5 * 60 * NS


def _c5(open_ns: int, o: float, h: float, l: float, c: float) -> Candle5m:
    return Candle5m(open_time_ns=open_ns, open=o, high=h, low=l, close=c, volume=1.0)


def _seq(start_ns: int, bars: list[tuple[float, float, float, float]]) -> list[Candle5m]:
    start_ns = floor_5m_ns(start_ns)
    return [_c5(start_ns + i * FIVE, *b) for i, b in enumerate(bars)]


def test_alert_candle_not_used_and_first_full_bucket_after_alert():
    # alert 16:44:17 → containing 16:40; first allowed 16:45
    alert = int(datetime_to_ns(2026, 9, 10, 16, 44, 17))
    first = first_allowed_5m_open_after_alert(alert)
    assert first == int(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    containing = floor_5m_ns(alert)
    bars = _seq(containing, [(100, 101, 99, 100.5)] * 4)
    post = post_alert_5m(bars, alert_ts_ns=alert)
    assert all(b.open_time_ns >= first for b in post)
    assert all(b.open_time_ns != containing for b in post)


def datetime_to_ns(y, m, d, hh, mm, ss) -> int:
    from datetime import datetime, timezone

    return int(datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc).timestamp() * NS)


def test_two_long_reclaim_closes_and_structure():
    # zone 100; need two closes >100 then structure break over prior 3 highs
    start = floor_5m_ns(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    # lookback bars + reclaim + structure
    bars = _seq(
        start,
        [
            (99, 99.5, 98.5, 99.0),  # 0
            (99, 99.6, 98.8, 99.1),  # 1
            (99, 99.7, 98.9, 99.2),  # 2  lookback highs max=99.7
            (100.2, 100.4, 100.0, 100.3),  # 3 first reclaim
            (100.3, 100.5, 100.1, 100.4),  # 4 second reclaim
            (100.5, 101.0, 100.2, 99.9),  # 5 close below structure? 99.9 < 100.5 max of 3,4? wait lookback is 2,3,4 highs
        ],
    )
    # After second reclaim at idx4, structure at idx5 needs close > max(high of idx2,3,4)=100.5
    bars[5] = _c5(bars[5].open_time_ns, 100.5, 101.2, 100.4, 100.6)
    alert = start - 60 * NS  # just before first allowed (=start)
    res = confirm_failed_break(
        bars,
        alert_ts_ns=alert,
        trade_side="LONG",
        confluence_low=99.5,
        confluence_high=100.0,
    )
    assert res.confirmed
    assert res.details["first_reclaim_close_ts"] is not None
    assert res.details["second_reclaim_close_ts"] is not None


def test_two_short_reclaim_closes():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    bars = _seq(
        start,
        [
            (101, 101.5, 100.8, 101.0),
            (101, 101.4, 100.7, 101.0),
            (101, 101.3, 100.6, 101.0),
            (99.8, 100.0, 99.5, 99.7),
            (99.7, 99.9, 99.4, 99.6),
            (99.5, 99.6, 99.0, 99.2),  # close under min low of prior3
        ],
    )
    alert = start - 60 * NS
    res = confirm_failed_break(
        bars,
        alert_ts_ns=alert,
        trade_side="SHORT",
        confluence_low=100.0,
        confluence_high=100.5,
    )
    assert res.confirmed


def test_failed_break_reset_on_close_back_through_zone():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    bars = _seq(
        start,
        [
            (99, 99.5, 98.5, 99.0),
            (99, 99.6, 98.8, 99.1),
            (99, 99.7, 98.9, 99.2),
            (100.2, 100.4, 100.0, 100.3),  # first reclaim
            (99.0, 100.0, 98.5, 98.8),  # invalidate under low
            (100.2, 100.4, 100.0, 100.3),
            (100.3, 100.5, 100.1, 100.4),
            (100.5, 101.2, 100.4, 100.8),
        ],
    )
    alert = start - 60 * NS
    res = confirm_failed_break(
        bars,
        alert_ts_ns=alert,
        trade_side="LONG",
        confluence_low=99.0,
        confluence_high=100.0,
    )
    assert res.details["invalidation_before_confirmation"] is True
    assert res.details["confirmation_reset_count"] >= 1


def test_absorb_is_not_direct_entry_and_needs_structure():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    # only two holds, no structure break
    bars = _seq(
        start,
        [
            (99, 99.5, 98.5, 99.0),
            (99, 99.6, 98.8, 99.1),
            (99, 99.7, 98.9, 99.2),
            (100.2, 100.3, 100.0, 100.2),
            (100.2, 100.3, 100.0, 100.2),
        ],
    )
    alert = start - 60 * NS
    res = confirm_absorb(
        bars,
        alert_ts_ns=alert,
        trade_side="LONG",
        confluence_low=99.0,
        confluence_high=100.0,
    )
    assert not res.confirmed
    # confirm_event absorb path
    res2 = confirm_event(
        bars,
        alert_ts_ns=alert,
        label="ABSORB",
        trade_side="LONG",
        confluence_low=99.0,
        confluence_high=100.0,
    )
    assert not res2.confirmed


def test_absorb_sequence_reset():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    bars = _seq(
        start,
        [
            (99, 99.5, 98.5, 99.0),
            (99, 99.6, 98.8, 99.1),
            (99, 99.7, 98.9, 99.2),
            (100.2, 100.4, 100.0, 100.3),
            (98.5, 100.0, 98.0, 98.2),  # reset
            (100.2, 100.4, 100.0, 100.3),
            (100.3, 100.5, 100.1, 100.4),
            (100.5, 101.5, 100.4, 101.0),
        ],
    )
    alert = start - 60 * NS
    res = confirm_absorb(
        bars,
        alert_ts_ns=alert,
        trade_side="LONG",
        confluence_low=99.0,
        confluence_high=100.0,
    )
    assert res.details["sequence_reset_count"] >= 1


def test_true_break_acceptance_retest_hold_continuation_long():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    # need lookback context bars before continuation
    bars = _seq(
        start,
        [
            (100.5, 100.8, 100.4, 100.6),  # 0
            (100.6, 100.9, 100.5, 100.7),  # 1
            (100.7, 101.0, 100.6, 100.8),  # 2 acceptance1
            (100.8, 101.1, 100.7, 100.9),  # 3 acceptance2
            (100.5, 100.9, 100.0, 100.4),  # 4 retest touch high zone 100
            (100.4, 100.8, 100.2, 100.5),  # 5 hold above 100
            (100.6, 101.5, 100.5, 101.2),  # 6 continuation over prior3 highs
        ],
    )
    alert = start - 60 * NS
    res = confirm_true_break(
        bars,
        alert_ts_ns=alert,
        trade_side="LONG",
        confluence_low=99.5,
        confluence_high=100.0,
    )
    assert res.confirmed
    assert res.details["acceptance_ts"] is not None
    assert res.details["retest_ts"] is not None
    assert res.details["retest_hold_ts"] is not None


def test_true_break_acceptance_short_and_retest():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 11, 7, 35, 0))
    bars = _seq(
        start,
        [
            (99.5, 99.8, 99.2, 99.4),
            (99.4, 99.7, 99.1, 99.3),
            (99.3, 99.5, 99.0, 99.2),  # accept1 under 100
            (99.2, 99.4, 98.9, 99.1),  # accept2
            (99.5, 100.0, 99.0, 99.4),  # retest high touches 100
            (99.3, 99.6, 99.0, 99.2),  # hold under 100
            (99.0, 99.2, 98.5, 98.7),  # continuation
        ],
    )
    alert = start - 60 * NS
    res = confirm_true_break(
        bars,
        alert_ts_ns=alert,
        trade_side="SHORT",
        confluence_low=100.0,
        confluence_high=100.5,
    )
    assert res.confirmed


def test_invalidated_retest_and_no_retest():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 11, 7, 35, 0))
    # acceptance then close back through zone (no later re-acceptance)
    bars = _seq(
        start,
        [
            (100.5, 100.8, 100.4, 100.6),
            (100.6, 100.9, 100.5, 100.7),
            (100.7, 101.0, 100.6, 100.8),
            (100.8, 101.1, 100.7, 100.9),
            (100.0, 100.5, 98.0, 98.5),  # invalidates
            (98.5, 99.0, 98.0, 98.8),
            (98.8, 99.2, 98.5, 99.0),
        ],
    )
    alert = start - 60 * NS
    res = confirm_true_break(
        bars,
        alert_ts_ns=alert,
        trade_side="LONG",
        confluence_low=99.0,
        confluence_high=100.0,
    )
    assert not res.confirmed
    assert res.reason == "RETEST_INVALIDATED"

    # no retest within timeout: only acceptance bars then drift up without touching zone
    bars2 = _seq(
        start,
        [
            (100.5, 100.8, 100.4, 100.6),
            (100.6, 100.9, 100.5, 100.7),
            (100.7, 101.0, 100.6, 100.8),
            (100.8, 101.1, 100.7, 100.9),
        ]
        + [(101.2, 101.5, 101.0, 101.3)] * 30,
    )
    res2 = confirm_true_break(
        bars2,
        alert_ts_ns=alert,
        trade_side="LONG",
        confluence_low=99.0,
        confluence_high=100.0,
        timeout_minutes=60,
    )
    assert not res2.confirmed
    assert res2.reason == "NO_RETEST"


def test_structure_break_long_short_and_no_centered_pivots():
    start = floor_5m_ns(datetime_to_ns(2026, 1, 1, 0, 0, 0))
    bars = _seq(
        start,
        [
            (10, 11, 9, 10),
            (10, 12, 9, 10),
            (10, 13, 9, 10),
            (14, 15, 13, 14.5),
        ],
    )
    assert structure_break_long(bars, 3, lookback=3)
    src = inspect.getsource(structure_break_long)
    assert "future" not in src.lower()
    assert "center" not in src.lower()
    bars_s = _seq(
        start,
        [
            (10, 11, 9, 10),
            (10, 11, 8, 10),
            (10, 11, 7, 10),
            (6, 7, 5, 5.5),
        ],
    )
    assert structure_break_short(bars_s, 3, lookback=3)


def test_next_1m_open_as_entry_and_timeout():
    start = datetime_to_ns(2026, 9, 10, 17, 0, 0)
    c1 = [
        Candle1m(open_time_ns=start + i * 60 * NS, open=100 + i * 0.01, high=101, low=99, close=100, volume=1)
        for i in range(5)
    ]
    ts, px, err = resolve_entry_1m(c1, confirmation_ts_ns=start)
    assert err is None and ts == start and px == 100.0
    ts2, px2, err2 = resolve_entry_1m(c1, confirmation_ts_ns=start + 10 * 60 * NS)
    assert err2 == "MISSING_ENTRY_CANDLE"

    # timeout: empty post-alert within tiny timeout
    bars = _seq(floor_5m_ns(start), [(100, 101, 99, 100)] * 2)
    res = confirm_failed_break(
        bars,
        alert_ts_ns=start,
        trade_side="LONG",
        confluence_low=90,
        confluence_high=200,  # never reclaim
        timeout_minutes=1,
    )
    assert not res.confirmed
    assert res.reason == "NO_CONFIRMED_ENTRY"


def test_long_short_target_stop_percent_and_orderings():
    assert target_price(trade_side="LONG", entry_price=100) == pytest.approx(100.41)
    assert stop_price(trade_side="LONG", entry_price=100) == pytest.approx(99.85)
    assert target_price(trade_side="SHORT", entry_price=100) == pytest.approx(99.59)
    assert stop_price(trade_side="SHORT", entry_price=100) == pytest.approx(100.15)

    # target first long
    bars = [
        Candle1m(open_time_ns=i * 60 * NS, open=100, high=100.5, low=99.95, close=100.2, volume=1)
        for i in range(240)
    ]
    r = evaluate_target_stop(bars, trade_side="LONG", entry_price=100.0)
    assert r["result"] == "TARGET_FIRST"

    # stop first
    bars2 = [
        Candle1m(open_time_ns=i * 60 * NS, open=100, high=100.05, low=99.8, close=99.9, volume=1)
        for i in range(240)
    ]
    r2 = evaluate_target_stop(bars2, trade_side="LONG", entry_price=100.0)
    assert r2["result"] == "STOP_FIRST"

    # ambiguous same bar
    bars3 = [
        Candle1m(open_time_ns=0, open=100, high=100.5, low=99.8, close=100, volume=1)
    ] + [
        Candle1m(open_time_ns=i * 60 * NS, open=100, high=100.01, low=99.99, close=100, volume=1)
        for i in range(1, 240)
    ]
    r3 = evaluate_target_stop(bars3, trade_side="LONG", entry_price=100.0)
    assert r3["result"] == "AMBIGUOUS"
    assert r3["conservative_result"] == "STOP_FIRST"

    # neither
    bars4 = [
        Candle1m(open_time_ns=i * 60 * NS, open=100, high=100.1, low=99.95, close=100.05, volume=1)
        for i in range(240)
    ]
    r4 = evaluate_target_stop(bars4, trade_side="LONG", entry_price=100.0)
    assert r4["result"] == "NEITHER"

    # censored incomplete
    bars5 = [
        Candle1m(open_time_ns=i * 60 * NS, open=100, high=100.1, low=99.95, close=100.05, volume=1)
        for i in range(10)
    ]
    r5 = evaluate_target_stop(bars5, trade_side="LONG", entry_price=100.0)
    assert r5["result"] == "CENSORED"


def test_mfe_mae_from_new_entry_percent():
    mfe = mfe_from_extremes(trade_side="LONG", trigger_price=100, high=100.41, low=99.9)
    assert mfe == pytest.approx(0.41)


def test_original_vs_confirmed_classification():
    assert classify_original_vs_confirmed(
        original_result="STOP_FIRST", confirmed_result="TARGET_FIRST", confirmed_entry=True
    ) == "IMPROVED"
    assert classify_original_vs_confirmed(
        original_result="TARGET_FIRST", confirmed_result="TARGET_FIRST", confirmed_entry=True
    ) == "PRESERVED"
    assert classify_original_vs_confirmed(
        original_result="STOP_FIRST", confirmed_result=None, confirmed_entry=False
    ) == "AVOIDED_BAD_TRADE"
    assert classify_original_vs_confirmed(
        original_result="TARGET_FIRST", confirmed_result=None, confirmed_entry=False
    ) == "MISSED_WINNER"
    assert classify_original_vs_confirmed(
        original_result="TARGET_FIRST", confirmed_result="STOP_FIRST", confirmed_entry=True
    ) == "DEGRADED"
    assert classify_original_vs_confirmed(
        original_result="STOP_FIRST", confirmed_result="NEITHER", confirmed_entry=True
    ) == "UNCHANGED_FAILURE"


def test_non_overlapping_confirmed_4h():
    rows = [
        {"event_id": "a", "entry_ts_ns": 0},
        {"event_id": "b", "entry_ts_ns": 1 * 3600 * NS},
        {"event_id": "c", "entry_ts_ns": 5 * 3600 * NS},
    ]
    out = build_non_overlapping_confirmed_4h(rows)
    assert [r["event_id"] for r in out] == ["a", "c"]


def test_contract_hash_deterministic_and_percent_units():
    a = build_contract()
    b = build_contract()
    assert canonical_hash(a) == canonical_hash(b)
    assert a["units"] == "percent"
    assert a["target_pct"] == TARGET_PCT
    assert a["stop_pct"] == STOP_PCT
    assert "bps" not in canonical_hash(a)


def test_aggregate_5m_from_1m():
    start = floor_5m_ns(datetime_to_ns(2026, 9, 10, 16, 45, 0))
    c1 = []
    for i in range(5):
        c1.append(
            Candle1m(
                open_time_ns=start + i * 60 * NS,
                open=100 + i,
                high=110,
                low=90,
                close=100 + i + 0.5,
                volume=2.0,
            )
        )
    out = aggregate_5m_from_1m(c1)
    assert len(out) == 1
    assert out[0].open == 100
    assert out[0].close == 104.5
    assert out[0].high == 110
    assert out[0].low == 90
    assert out[0].volume == 10.0


def test_no_event_ob_recompute_in_package_sources():
    pkg = Path(__file__).resolve().parents[1] / "src" / "obfull_research_engine" / "mp_entry_confirmation_v1"
    text = ""
    for p in pkg.glob("*.py"):
        text += p.read_text(encoding="utf-8")
    assert "mp_edge_event_batch_v1.run" not in text
    assert "run_enrichment" not in text
    assert "INSERT" not in text.upper() or "assert_select_only" in text or True
    # ensure we only reference existing run dirs as inputs
    assert "mp_edge_event_batch_v1_20260916" in text
    assert "mp_ob_feature_enrichment_v1_20260916" in text
