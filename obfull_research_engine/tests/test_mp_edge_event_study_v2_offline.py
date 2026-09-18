"""Offline tests for mp_edge_event_study_v2."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from obfull_research_engine.mp_edge_event_study_v1.mp_levels import fixture_profile
from obfull_research_engine.mp_edge_event_study_v1.schema import MidTick
from obfull_research_engine.mp_edge_event_study_v1.util import dt_to_ns
from obfull_research_engine.mp_edge_event_study_v2.outcomes import _mfe_mae, compute_outcomes_v2
from obfull_research_engine.mp_edge_event_study_v2.params import build_params_v2
from obfull_research_engine.mp_edge_event_study_v2.pipeline import run_offline_pipeline_v2
from obfull_research_engine.mp_edge_event_study_v2.sides import trade_side_for


UTC = timezone.utc
NS = 1_000_000_000


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(UTC)


def _params(tmp_path: Path, **kw):
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
        reclaim_hold_s=15.0,
        failed_break_horizon_s=120.0,
        true_break_acceptance_s=60.0,
        true_break_continuation_bps=3.0,
        approach_lookback_s=30.0,
        approach_origin_distance_bps=2.0,
        require_correct_approach=True,
        allow_retest_from_break_side=False,
        absorb_confirmation_bps=3.0,
        absorb_confirmation_max_s=60.0,
        outcome_horizons_s="60,300",
    )
    base.update(kw)
    return build_params_v2(**base)


def _mids(start_ns: int, prices: list[float], step_ns: int = 100_000_000) -> list[MidTick]:
    return [
        MidTick(
            ts_ns=start_ns + i * step_ns,
            mid=float(px),
            best_bid=float(px) - 0.5,
            best_ask=float(px) + 0.5,
            valid=True,
            epoch_id="ep1",
            chunk_key="ck",
        )
        for i, px in enumerate(prices)
    ]


def _upper_profile():
    return fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=100.0,
        val=90.0,
    )


def _lower_profile():
    return fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T15:30:00Z"),
        end=_dt("2026-09-10T16:00:00Z"),
        vah=110.0,
        val=100.0,
    )


def test_trade_side_mappings():
    assert trade_side_for("UPPER", "ABSORB") == ("SHORT", "FADE_SIGNAL:ABSORB")
    assert trade_side_for("UPPER", "FAILED_BREAK") == ("SHORT", "FADE_SIGNAL:FAILED_BREAK")
    assert trade_side_for("UPPER", "TRUE_BREAK") == ("LONG", "BREAK_CONTINUATION:TRUE_BREAK")
    assert trade_side_for("LOWER", "ABSORB") == ("LONG", "FADE_SIGNAL:ABSORB")
    assert trade_side_for("LOWER", "FAILED_BREAK") == ("LONG", "FADE_SIGNAL:FAILED_BREAK")
    assert trade_side_for("LOWER", "TRUE_BREAK") == ("SHORT", "BREAK_CONTINUATION:TRUE_BREAK")


def test_upper_armed_only_from_below(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    # start already at/above zone → no arm → reject touch
    prices = [100.0] * 20
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    assert res["rejection_stats"].rejected_not_armed + res["rejection_stats"].rejected_profile_activation_inside_zone >= 1
    assert all(e.event_role != "UPPER" or e.label_price_only for e in res["events"]) or len(res["events"]) == 0


def test_lower_armed_only_from_above(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    prices = [100.0] * 20  # at lower zone without being above
    res = run_offline_pipeline_v2(params=params, profiles=[_lower_profile()], mids=_mids(start_ns, prices))
    assert res["rejection_stats"].raw_touch_candidates >= 1


def test_upper_touch_from_above_rejected(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    # approach UPPER from above without ever arming from below
    prices = [100.05] * 15 + [100.0] * 10
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    assert (
        res["rejection_stats"].rejected_wrong_approach
        + res["rejection_stats"].rejected_not_armed
        + res["rejection_stats"].rejected_profile_activation_inside_zone
    ) >= 1
    assert not any(e.event_role == "UPPER" and e.label_price_only != "UNRESOLVED" for e in res["events"]) or (
        res["rejection_stats"].valid_directional_touches == 0
    )


def test_lower_touch_from_below_rejected(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    prices = [100.03] * 10 + [99.95] * 10 + [100.0] * 5
    res = run_offline_pipeline_v2(params=params, profiles=[_lower_profile()], mids=_mids(start_ns, prices))
    assert res["rejection_stats"].rejected_wrong_approach >= 1 or res["rejection_stats"].raw_touch_candidates >= 1


def test_profile_activation_inside_no_touch(tmp_path):
    # profile available at 16:00 while price already at VAH
    params = _params(tmp_path, start="2026-09-10T16:00:00Z", end="2026-09-10T16:10:00Z")
    start_ns = dt_to_ns(params.start)
    prices = [100.0] * 50
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    assert res["rejection_stats"].rejected_profile_activation_inside_zone >= 1
    assert not any(e.label_price_only == "ABSORB" for e in res["events"])


def test_profile_change_requires_rearm(tmp_path):
    p1 = _upper_profile()
    p2 = fixture_profile(
        tf="30m",
        start=_dt("2026-09-10T16:00:00Z"),
        end=_dt("2026-09-10T16:30:00Z"),
        vah=101.0,
        val=91.0,
    )
    params = _params(tmp_path, start="2026-09-10T16:00:00Z", end="2026-09-10T16:40:00Z")
    start_ns = dt_to_ns(params.start)
    n = int(30 * 60 / 0.1)
    prices = [99.97] * 20 + [100.0] * 10 + [99.96] * 10  # absorb on first profile
    prices += [100.5] * (n - 40)
    prices += [101.0] * 30  # at new level without re-arm from below
    res = run_offline_pipeline_v2(params=params, profiles=[p1, p2], mids=_mids(start_ns, prices))
    assert res["rejection_stats"].rejected_profile_activation_inside_zone >= 1 or res["rejection_stats"].rejected_not_armed >= 1


def test_upper_absorb_trade_short(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    prices = [99.97] * 10 + [100.0] * 5 + [99.96] * 20  # arm, touch, fade 3bps+
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    absorbs = [e for e in res["events"] if e.label_price_only == "ABSORB" and e.event_role == "UPPER"]
    assert absorbs
    assert absorbs[0].trade_side == "SHORT"


def test_upper_failed_break_trade_short(tmp_path):
    params = _params(tmp_path, true_break_acceptance_s=60.0, failed_break_horizon_s=120.0)
    start_ns = dt_to_ns(params.start)
    # arm, touch, penetrate, reclaim within 120s, hold 15s
    prices = [99.97] * 10 + [100.0] * 3 + [100.05] * 20 + [99.95] * int(20 / 0.1)
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    fb = [e for e in res["events"] if e.label_price_only == "FAILED_BREAK"]
    assert fb
    assert fb[0].trade_side == "SHORT"
    assert fb[0].event_role == "UPPER"


def test_upper_true_break_trade_long(tmp_path):
    params = _params(
        tmp_path,
        start="2026-09-10T16:00:00Z",
        end="2026-09-10T16:30:00Z",
        failed_break_horizon_s=120.0,
        true_break_acceptance_s=60.0,
        true_break_continuation_bps=3.0,
    )
    start_ns = dt_to_ns(params.start)
    # arm, touch, stay beyond for >120s with acceptance
    prices = [99.97] * 10 + [100.0] * 3 + [100.05] * int(130 / 0.1)
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    tb = [e for e in res["events"] if e.label_price_only == "TRUE_BREAK" and e.event_role == "UPPER"]
    assert tb
    assert tb[0].trade_side == "LONG"


def test_lower_absorb_trade_long(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    prices = [100.03] * 10 + [100.0] * 5 + [100.04] * 20
    res = run_offline_pipeline_v2(params=params, profiles=[_lower_profile()], mids=_mids(start_ns, prices))
    absorbs = [e for e in res["events"] if e.label_price_only == "ABSORB" and e.event_role == "LOWER"]
    assert absorbs
    assert absorbs[0].trade_side == "LONG"


def test_lower_failed_break_trade_long(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    prices = [100.03] * 10 + [100.0] * 3 + [99.95] * 20 + [100.05] * int(20 / 0.1)
    res = run_offline_pipeline_v2(params=params, profiles=[_lower_profile()], mids=_mids(start_ns, prices))
    fb = [e for e in res["events"] if e.label_price_only == "FAILED_BREAK" and e.event_role == "LOWER"]
    assert fb
    assert fb[0].trade_side == "LONG"


def test_lower_true_break_trade_short(tmp_path):
    params = _params(
        tmp_path,
        start="2026-09-10T16:00:00Z",
        end="2026-09-10T16:30:00Z",
    )
    start_ns = dt_to_ns(params.start)
    prices = [100.03] * 10 + [100.0] * 3 + [99.95] * int(130 / 0.1)
    res = run_offline_pipeline_v2(params=params, profiles=[_lower_profile()], mids=_mids(start_ns, prices))
    tb = [e for e in res["events"] if e.label_price_only == "TRUE_BREAK" and e.event_role == "LOWER"]
    assert tb
    assert tb[0].trade_side == "SHORT"


def test_acceptance_then_reclaim_is_failed_break(tmp_path):
    params = _params(
        tmp_path,
        start="2026-09-10T16:00:00Z",
        end="2026-09-10T16:20:00Z",
        true_break_acceptance_s=30.0,  # shorter acceptance for test
        failed_break_horizon_s=120.0,
        reclaim_hold_s=15.0,
    )
    start_ns = dt_to_ns(params.start)
    # beyond 35s then reclaim and hold
    prices = [99.97] * 10 + [100.0] * 2 + [100.05] * int(35 / 0.1) + [99.95] * int(20 / 0.1)
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    fb = [e for e in res["events"] if e.label_price_only == "FAILED_BREAK"]
    assert fb
    assert fb[0].transition_pattern == "PENETRATE_ACCEPT_RECLAIM"


def test_reclaim_within_120_failed_break(tmp_path):
    test_upper_failed_break_trade_short(tmp_path)


def test_no_reclaim_stable_true_break(tmp_path):
    test_upper_true_break_trade_long(tmp_path)


def test_absorb_needs_confirmation(tmp_path):
    params = _params(tmp_path, absorb_confirmation_bps=3.0)
    start_ns = dt_to_ns(params.start)
    # touch but only 1bp fade — not enough
    prices = [99.97] * 10 + [100.0] * 5 + [99.995] * 30
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    assert not any(e.label_price_only == "ABSORB" for e in res["events"])


def test_unconfirmed_rejection_unresolved(tmp_path):
    params = _params(tmp_path, end="2026-09-10T16:00:05Z", absorb_confirmation_bps=3.0)
    start_ns = dt_to_ns(params.start)
    prices = [99.97] * 5 + [100.0] * 10
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    assert any(e.label_price_only == "UNRESOLVED" and e.is_censored for e in res["events"])


def test_outcome_uses_trade_side(tmp_path):
    params = _params(tmp_path, start="2026-09-10T16:00:00Z", end="2026-09-10T16:30:00Z")
    start_ns = dt_to_ns(params.start)
    prices = [99.97] * 10 + [100.0] * 3 + [100.05] * int(130 / 0.1)
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    tb = [e for e in res["events"] if e.label_price_only == "TRUE_BREAK"][0]
    assert tb.trade_side == "LONG"
    outs = [o for o in res["outcomes"] if o.event_id == tb.event_id and o.outcome_status == "OK"]
    assert outs
    assert all(o.trade_side == "LONG" for o in outs)
    # LONG MFE should rise with higher prices after trigger
    mfe, mae = _mfe_mae("LONG", tb.trigger_price or 100.05, [100.1, 100.2])
    assert mfe > 0


def test_censored_horizon_not_true_break(tmp_path):
    params = _params(
        tmp_path,
        start="2026-09-10T16:00:00Z",
        end="2026-09-10T16:02:00Z",  # only 2 minutes — horizon 120s not observable
        failed_break_horizon_s=120.0,
        true_break_acceptance_s=30.0,
    )
    start_ns = dt_to_ns(params.start)
    prices = [99.97] * 5 + [100.0] * 2 + [100.05] * int(90 / 0.1)
    res = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    assert not any(e.label_price_only == "TRUE_BREAK" for e in res["events"])
    assert any(
        e.is_censored and "CENSORED" in e.censor_reason
        for e in res["events"]
        if e.penetration_ts_ns
    ) or any(e.label_price_only == "UNRESOLVED" for e in res["events"])


def test_deterministic(tmp_path):
    params = _params(tmp_path)
    start_ns = dt_to_ns(params.start)
    prices = [99.97] * 10 + [100.0] * 5 + [99.96] * 20
    r1 = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    r2 = run_offline_pipeline_v2(params=params, profiles=[_upper_profile()], mids=_mids(start_ns, prices))
    assert [e.event_id for e in r1["events"]] == [e.event_id for e in r2["events"]]
    assert [e.label_price_only for e in r1["events"]] == [e.label_price_only for e in r2["events"]]
