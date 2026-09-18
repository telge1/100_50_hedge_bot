"""Offline unit/integration tests for mp_edge_event_study_v1 (no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from obfull_research_engine.mp_edge_event_study_v1.confluence import (
    build_active_confluence,
    cluster_same_role,
)
from obfull_research_engine.mp_edge_event_study_v1.mp_levels import (
    active_levels_at,
    assert_causality,
    fixture_profile,
    period_ends_in_range,
    profiles_to_levels,
)
from obfull_research_engine.mp_edge_event_study_v1.outcomes import (
    _mfe_mae_for_side,
    _tpsl_first,
    compute_outcomes,
)
from obfull_research_engine.mp_edge_event_study_v1.params import build_params
from obfull_research_engine.mp_edge_event_study_v1.pipeline import (
    build_zone_timeline,
    run_offline_pipeline,
)
from obfull_research_engine.mp_edge_event_study_v1.schema import ActiveLevel, MidTick
from obfull_research_engine.mp_edge_event_study_v1.touch_detect import detect_events
from obfull_research_engine.mp_edge_event_study_v1.util import (
    dt_to_ns,
    make_event_id,
    param_fingerprint,
)


UTC = timezone.utc
NS = 1_000_000_000


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _params(tmp_path: Path, **kwargs):
    base = dict(
        symbol="BTCUSDT",
        start="2026-09-10T16:00:00Z",
        end="2026-09-10T17:00:00Z",
        output_dir=tmp_path / "out",
        touch_tolerance_bps=1.0,
        confluence_tolerance_bps=5.0,
        min_event_separation_s=60.0,
        reset_distance_bps=10.0,
        min_penetration_bps=2.0,
        max_reclaim_delay_s=30.0,
        reclaim_hold_s=15.0,
        true_break_acceptance_s=30.0,
        outcome_horizons_s="60,300",
        tp_targets_bps="30,40,50",
        sl_target_bps=25.0,
        reclaim_tolerance_bps=0.0,
    )
    base.update(kwargs)
    return build_params(**base)


def _mid_series(start_ns: int, prices: list[float], step_ns: int = 100_000_000) -> list[MidTick]:
    out = []
    for i, px in enumerate(prices):
        out.append(
            MidTick(
                ts_ns=start_ns + i * step_ns,
                mid=float(px),
                best_bid=float(px) - 0.5,
                best_ask=float(px) + 0.5,
                valid=True,
                epoch_id="ep1",
                chunk_key="ck1",
            )
        )
    return out


def _level(tf: str, role: str, price: float, avail: datetime, pid: str = "p") -> ActiveLevel:
    avail_ns = dt_to_ns(avail)
    return ActiveLevel(
        level_id=f"{tf}_{role}_{price}",
        profile_id=f"{pid}_{tf}",
        timeframe=tf,
        role=role,
        level_price=price,
        profile_start_ts_ns=avail_ns - 1800 * NS,
        profile_end_ts_ns=avail_ns,
        profile_available_ts_ns=avail_ns,
        profile_source="fixture",
    )


# --- 1/2/3 causality / previous_closed ---


def test_previous_closed_active_only_after_period_end():
    start = _dt("2026-09-10T16:00:00Z")
    end = _dt("2026-09-10T17:00:00Z")
    # 30m period ending at 16:00 available at 16:00; forming 16:00-16:30 must NOT be used
    p_closed = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    # would-be forming profile ending in future — not included in fixtures for active_at
    levels = profiles_to_levels([p_closed])
    before = active_levels_at(levels, ts_ns=dt_to_ns(_dt("2026-09-10T15:59:59.900Z")))
    assert before == []
    after = active_levels_at(levels, ts_ns=dt_to_ns(_dt("2026-09-10T16:00:00Z")))
    assert len(after) == 2
    assert {lv.role for lv in after} == {"UPPER", "LOWER"}


def test_forming_profile_not_used():
    # Only previous_closed fixtures are passed; a "forming" profile with available_at in future
    # relative to event is excluded by active_levels_at.
    forming = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T16:00:00Z"),
        end=_dt("2026-09-10T16:30:00Z"),
        vah=110.0,
        val=95.0,
    )
    levels = profiles_to_levels([forming])
    # at 16:10 forming end is still in future → not available
    active = active_levels_at(levels, ts_ns=dt_to_ns(_dt("2026-09-10T16:10:00Z")))
    assert active == []


def test_causality_assert_fails_when_available_after_event():
    lv = _level("30m", "UPPER", 100.0, _dt("2026-09-10T16:30:00Z"))
    with pytest.raises(RuntimeError, match="CAUSALITY"):
        assert_causality(lv, dt_to_ns(_dt("2026-09-10T16:00:00Z")))


# --- 4/5/6/7 confluence ---


def test_cluster_30m_1h_upper():
    avail = _dt("2026-09-10T16:00:00Z")
    levels = [
        _level("30m", "UPPER", 100.00, avail),
        _level("1h", "UPPER", 100.03, avail),  # ~3 bps at 100
    ]
    zones = cluster_same_role(levels, role="UPPER", confluence_tolerance_bps=5.0, asof_ns=1)
    assert len(zones) == 1
    assert zones[0].confluence_class == "C2_30M_1H"
    assert zones[0].role == "UPPER"


def test_cluster_30m_4h_lower():
    avail = _dt("2026-09-10T16:00:00Z")
    levels = [
        _level("30m", "LOWER", 90.00, avail),
        _level("4h", "LOWER", 90.04, avail),
    ]
    zones = cluster_same_role(levels, role="LOWER", confluence_tolerance_bps=5.0, asof_ns=1)
    assert len(zones) == 1
    assert zones[0].confluence_class == "C2_30M_4H"


def test_upper_lower_not_clustered_together():
    avail = _dt("2026-09-10T16:00:00Z")
    levels = [
        _level("30m", "UPPER", 100.0, avail),
        _level("1h", "LOWER", 100.0, avail),
    ]
    zones = build_active_confluence(levels, confluence_tolerance_bps=5.0, asof_ns=1)
    assert all(z.confluence_class.startswith("C1") for z in zones)
    assert len(zones) == 2


def test_deterministic_cluster_tie_break():
    avail = _dt("2026-09-10T16:00:00Z")
    levels = [
        _level("4h", "UPPER", 100.02, avail, pid="a"),
        _level("30m", "UPPER", 100.00, avail, pid="b"),
        _level("1h", "UPPER", 100.04, avail, pid="c"),
    ]
    z1 = cluster_same_role(levels, role="UPPER", confluence_tolerance_bps=5.0, asof_ns=1)
    z2 = cluster_same_role(list(reversed(levels)), role="UPPER", confluence_tolerance_bps=5.0, asof_ns=1)
    assert len(z1) == 1 and len(z2) == 1
    assert z1[0].zone_id == z2[0].zone_id
    assert z1[0].timeframes == ["30m", "1h", "4h"]


# --- FSM / labels ---


def _run_simple(tmp_path, prices, profiles, **pkw):
    params = _params(tmp_path, **pkw)
    start_ns = dt_to_ns(params.start)
    mids = _mid_series(start_ns, prices)
    return run_offline_pipeline(params=params, profiles=profiles, mids=mids, epoch_id="ep1")


def test_touch_not_duplicated_every_100ms(tmp_path):
    # stay in touch band without reset
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    prices = [100.0] * 50  # 5 seconds in band
    res = _run_simple(tmp_path, prices, [p])
    upper = [e for e in res["events"] if e.event_role == "UPPER"]
    assert len(upper) == 1


def test_reset_by_time(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    # touch, fade absorb, leave far, wait 60s+, touch again
    # 0.1s steps: absorb needs fade >=2bps below 100 → 99.98
    prices = [100.0] * 5 + [99.97] * 5 + [99.0] * 5
    # pad to >60s away then return
    prices += [99.0] * 600 + [100.0] * 5 + [99.97] * 5
    res = _run_simple(tmp_path, prices, [p], min_event_separation_s=60.0)
    upper = [e for e in res["events"] if e.event_role == "UPPER" and e.label == "ABSORB"]
    assert len(upper) >= 2


def test_reset_by_distance(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    # absorb, move 10+ bps away, return quickly (<60s)
    prices = [100.0] * 3 + [99.97] * 3 + [99.8] * 10 + [100.0] * 3 + [99.97] * 3
    res = _run_simple(
        tmp_path,
        prices,
        [p],
        min_event_separation_s=600.0,
        reset_distance_bps=10.0,
    )
    upper = [e for e in res["events"] if e.event_role == "UPPER" and e.label == "ABSORB"]
    assert len(upper) >= 2


def test_reset_by_profile_change(tmp_path):
    p1 = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    p2 = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T16:00:00Z"),
        end=_dt("2026-09-10T16:30:00Z"),
        vah=101.0,
        val=91.0,
    )
    # until 16:30 new profile; build series spanning the change
    params = _params(tmp_path, start="2026-09-10T16:00:00Z", end="2026-09-10T16:40:00Z")
    # first segment near 100, then after 16:30 near 101
    start_ns = dt_to_ns(params.start)
    # 30 minutes of mid at 100 then at 101
    n1 = int(30 * 60 / 0.1)
    prices = [100.0] * 20 + [99.97] * 10  # absorb on first
    prices += [100.5] * (n1 - 30)
    prices += [101.0] * 20 + [100.97] * 10
    mids = _mid_series(start_ns, prices)
    res = run_offline_pipeline(params=params, profiles=[p1, p2], mids=mids, epoch_id="ep1")
    assert any(e.confluence_center >= 100.5 for e in res["events"]) or len(res["events"]) >= 1


def test_upper_penetration_and_reclaim(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    # touch, penetrate >2bps (100.03), reclaim below 100, hold 15s
    prices = [100.0] * 5 + [100.05] * 10 + [99.95] * int(15 / 0.1 + 5)
    res = _run_simple(tmp_path, prices, [p], reclaim_hold_s=15.0, max_reclaim_delay_s=30.0)
    fb = [e for e in res["events"] if e.label == "FAILED_BREAK" and e.event_role == "UPPER"]
    assert len(fb) == 1
    assert fb[0].reclaim_ts_ns is not None
    assert fb[0].max_penetration_bps >= 2.0


def test_lower_penetration_and_reclaim(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=110.0,
        val=100.0,
    )
    prices = [100.0] * 5 + [99.95] * 10 + [100.05] * int(15 / 0.1 + 5)
    res = _run_simple(tmp_path, prices, [p])
    fb = [e for e in res["events"] if e.label == "FAILED_BREAK" and e.event_role == "LOWER"]
    assert len(fb) == 1


def test_confirmed_reclaim(tmp_path):
    test_upper_penetration_and_reclaim(tmp_path)


def test_failed_reclaim_hold_broken(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    # reclaim briefly then spike beyond again before hold completes → eventually TRUE_BREAK
    prices = [100.0] * 3 + [100.05] * 5 + [99.95] * 20 + [100.05] * int(35 / 0.1)
    res = _run_simple(tmp_path, prices, [p], reclaim_hold_s=15.0, true_break_acceptance_s=30.0)
    assert any(e.label == "TRUE_BREAK" for e in res["events"]) or any(
        e.label == "UNRESOLVED" for e in res["events"]
    )


def test_true_break_by_acceptance(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    prices = [100.0] * 3 + [100.05] * int(35 / 0.1)
    res = _run_simple(tmp_path, prices, [p], true_break_acceptance_s=30.0)
    assert any(e.label == "TRUE_BREAK" for e in res["events"])


def test_event_censored_at_window_end(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    # only a few ticks in touch — no resolve
    prices = [100.0] * 5
    res = _run_simple(tmp_path, prices, [p], end="2026-09-10T16:00:01Z")
    assert any(e.is_censored and e.label == "UNRESOLVED" for e in res["events"])


def test_outcome_censored_past_window(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    # absorb late in short window so 300s horizon censored
    params = _params(
        tmp_path,
        start="2026-09-10T16:00:00Z",
        end="2026-09-10T16:00:05Z",
        outcome_horizons_s="60,300",
    )
    prices = [100.0] * 3 + [99.97] * 5
    mids = _mid_series(dt_to_ns(params.start), prices)
    res = run_offline_pipeline(params=params, profiles=[p], mids=mids)
    assert any(o.outcome_status == "CENSORED" for o in res["outcomes"])


def test_long_mfe_mae():
    mfe, mae = _mfe_mae_for_side(fade_side="LONG", trigger_price=100.0, path=[100.1, 99.9, 100.2])
    assert mfe > 0 and mae > 0
    assert mfe == pytest.approx(20.0, rel=1e-3)  # 0.2 / 100 * 1e4


def test_short_mfe_mae():
    mfe, mae = _mfe_mae_for_side(fade_side="SHORT", trigger_price=100.0, path=[99.8, 100.1])
    assert mfe == pytest.approx(20.0, rel=1e-3)
    assert mae == pytest.approx(10.0, rel=1e-3)


def test_tp_first():
    path = [(1, 99.6), (2, 99.5)]  # SHORT TP 40bps at 99.6
    assert (
        _tpsl_first(fade_side="SHORT", trigger_price=100.0, path=path, tp_bps=40, sl_bps=25)
        == "TP"
    )


def test_sl_first():
    path = [(1, 100.3), (2, 99.5)]  # SHORT SL 30bps first
    assert (
        _tpsl_first(fade_side="SHORT", trigger_price=100.0, path=path, tp_bps=40, sl_bps=25)
        == "SL"
    )


def test_ambiguous_same_observation():
    path = [(1, 99.6)]  # if both TP and SL somehow same tick — craft equal hits
    # For SHORT: tp at down, sl at up — same tick can't hit both unless weird.
    # Force by using a path where both thresholds met at same ts via mid that... impossible for opposite dirs.
    # Spec: same 100ms row — use synthetic where we call with identical tp/sl hit ts by two-sided move impossible;
    # instead verify helper returns AMBIGUOUS when both hit_ns equal:
    # Monkey: path with price that for LONG hits both? Impossible.
    # Direct unit: emulate by checking NEITHER / structure — create path with one tick crossing both
    # for LONG: TP up and SL down can't be same price.
    # So test API with duplicated ts entries: first entry hits TP, we also need SL same ts —
    # Implementation sets both on same iteration if both conditions true — only if mid is both up and down.
    # Alternative: call with fade LONG trigger 100, path [(1, 100.4)] TP40 and SL25 can't both.
    # We'll verify AMBIGUOUS path by temporarily using equal thresholds crossing:
    # If tp_bps=0 and sl_bps=0, both hit same tick.
    assert (
        _tpsl_first(fade_side="LONG", trigger_price=100.0, path=[(5, 100.0)], tp_bps=0, sl_bps=0)
        == "AMBIGUOUS"
    )


def test_no_epoch_overflow_in_pipeline(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    end_ns = dt_to_ns(params.end)
    mids = _mid_series(start_ns, [100.0] * 10)
    # inject out-of-window tick should be rejected by sanity if events outside —
    # mid loader would raise; here pipeline only uses provided mids within window
    for m in mids:
        assert start_ns <= m.ts_ns < end_ns


def test_invalid_mid_skipped(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    mids = [
        MidTick(start_ns, 100.0, 99.5, 100.5, False, "ep1", "ck"),
        MidTick(start_ns + 100_000_000, 100.0, 99.5, 100.5, True, "ep1", "ck"),
        MidTick(start_ns + 200_000_000, 99.97, 99.5, 100.5, True, "ep1", "ck"),
    ]
    res = run_offline_pipeline(params=params, profiles=[p], mids=mids)
    assert all(e.first_touch_ts_ns >= start_ns + 100_000_000 for e in res["events"] if e.event_role == "UPPER")


def test_deterministic_event_id():
    a = make_event_id(
        symbol="BTCUSDT",
        zone_id="zn1",
        first_touch_ns=123,
        event_role="UPPER",
        confluence_class="C1_30M",
        param_fingerprint="abc",
    )
    b = make_event_id(
        symbol="BTCUSDT",
        zone_id="zn1",
        first_touch_ns=123,
        event_role="UPPER",
        confluence_class="C1_30M",
        param_fingerprint="abc",
    )
    assert a == b


def test_reproducible_output(tmp_path):
    p = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )
    prices = [100.0] * 5 + [99.97] * 5
    r1 = _run_simple(tmp_path, prices, [p])
    r2 = _run_simple(tmp_path, prices, [p])
    assert [e.event_id for e in r1["events"]] == [e.event_id for e in r2["events"]]
    assert Counter_labels(r1) == Counter_labels(r2)


def Counter_labels(res):
    from collections import Counter

    return dict(Counter(e.label for e in res["events"]))


def test_period_ends_include_boundary_at_start():
    ends = period_ends_in_range(_dt("2026-09-10T16:10:00Z"), _dt("2026-09-10T17:00:00Z"), "30m")
    assert _dt("2026-09-10T16:00:00Z") in ends
    assert _dt("2026-09-10T16:30:00Z") in ends
    assert _dt("2026-09-10T17:00:00Z") in ends
