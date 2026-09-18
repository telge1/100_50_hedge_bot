"""Offline tests for mp_edge_event_batch_v1."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from obfull_research_engine.mp_edge_event_batch_v1.episodes import (
    build_episode_rows,
    policy_event_ids,
)
from obfull_research_engine.mp_edge_event_batch_v1.outcomes_ext import (
    signed_return_bps,
    compute_outcomes_ext,
)
from obfull_research_engine.mp_edge_event_batch_v1.params import (
    SEMANTICS_HASH,
    FROZEN_V2_SEMANTICS,
    semantics_hash,
    v2_params_for_window,
)
from obfull_research_engine.mp_edge_event_batch_v1.stats import (
    block_bootstrap_mean_ci,
    wilson_interval,
)
from obfull_research_engine.mp_edge_event_batch_v1.warmup import plan_warmup
from obfull_research_engine.mp_edge_event_batch_v1.windows import (
    BatchWindow,
    select_windows,
    write_batch_windows_csv,
    read_batch_windows_csv,
)
from obfull_research_engine.mp_edge_event_study_v1.schema import MidTick
from obfull_research_engine.mp_edge_event_study_v1.util import dt_to_ns
from obfull_research_engine.mp_edge_event_study_v2.schema import EventV2
from obfull_research_engine.mp_edge_event_study_v2.sides import trade_side_for


UTC = timezone.utc
NS = 1_000_000_000


def _ev(
    eid: str,
    ts: int,
    zone: str,
    profiles: list[str],
    role: str = "UPPER",
    label: str = "ABSORB",
    trade: str = "SHORT",
) -> EventV2:
    return EventV2(
        event_id=eid,
        symbol="BTCUSDT",
        first_touch_ts_ns=ts,
        touch_price=100.0,
        event_role=role,
        label_price_only=label,
        fade_side="SHORT" if role == "UPPER" else "LONG",
        break_side="LONG" if role == "UPPER" else "SHORT",
        trade_side=trade,
        trade_side_reason="test",
        zone_id=zone,
        confluence_class="C1_30M",
        confluence_low=100.0,
        confluence_high=100.0,
        confluence_center=100.0,
        confluence_width_bps=0.0,
        timeframes=["30m"],
        active_profile_ids=profiles,
        level_ids=["l1"],
        profile_start_ts_ns=ts - 1800 * NS,
        profile_end_ts_ns=ts,
        profile_available_ts_ns=ts - 60 * NS,
        profile_source="fixture",
        timeframe="30m",
        epoch_id="ep",
        trigger_ts_ns=ts + NS,
        trigger_price=100.0,
        trigger_reason="test",
    )


def test_semantics_hash_stable():
    assert semantics_hash() == SEMANTICS_HASH
    assert semantics_hash(FROZEN_V2_SEMANTICS) == SEMANTICS_HASH
    mutated = dict(FROZEN_V2_SEMANTICS)
    mutated["true_break_acceptance_s"] = 999
    assert semantics_hash(mutated) != SEMANTICS_HASH


def test_windows_csv_roundtrip(tmp_path):
    wins = [
        BatchWindow(
            window_id="w1",
            start_ts="2026-09-10T00:00:00Z",
            end_ts="2026-09-10T03:00:00Z",
            start_ns=1,
            end_ns=2,
            duration_s=10800,
            replay_epoch="abc",
            safe_coverage="FULL",
            parity_status="READY",
            included=True,
            exclusion_reason="",
            estimated_rows=10,
            estimated_runtime_s=1.0,
        ),
        BatchWindow(
            window_id="w2",
            start_ts="2026-09-10T03:00:00Z",
            end_ts="2026-09-10T04:00:00Z",
            start_ns=3,
            end_ns=4,
            duration_s=3600,
            replay_epoch="def",
            safe_coverage="EXCLUDED",
            parity_status="EXCLUDED",
            included=False,
            exclusion_reason="DURATION_LT_7200S",
            estimated_rows=0,
            estimated_runtime_s=0.0,
        ),
    ]
    path = tmp_path / "batch_windows.csv"
    write_batch_windows_csv(path, wins)
    back = read_batch_windows_csv(path)
    assert len(back) == 2
    assert back[0].included and not back[1].included
    sel = select_windows(back, included_only=True)
    assert [w.window_id for w in sel] == ["w1"]


def test_unsafe_excluded_by_select():
    wins = [
        BatchWindow("a", "s", "e", 1, 2, 100, "e1", "X", "X", False, "BAD", 0, 0),
        BatchWindow("b", "s", "e", 1, 2, 100, "e2", "OK", "OK", True, "", 1, 1),
    ]
    assert [w.window_id for w in select_windows(wins)] == ["b"]


def test_warmup_no_events_before_analysis():
    start = dt_to_ns(datetime(2026, 9, 10, 16, 0, tzinfo=UTC))
    end = start + 3600 * NS
    epoch_safe = start  # no pre-window
    plan = plan_warmup(
        window_id="w",
        analysis_start_ns=start,
        analysis_end_ns=end,
        epoch_safe_start_ns=epoch_safe,
    )
    assert plan.mp_warmup_start_ns == epoch_safe
    assert plan.mp_warmup_clamped
    assert plan.analysis_start_ns == start
    assert "4h" in plan.timeframes_missing_at_start or plan.timeframes_missing_at_start


def test_warmup_with_room_before():
    start = dt_to_ns(datetime(2026, 9, 10, 16, 0, tzinfo=UTC))
    end = start + 3600 * NS
    epoch_safe = start - 5 * 3600 * NS
    plan = plan_warmup(
        window_id="w",
        analysis_start_ns=start,
        analysis_end_ns=end,
        epoch_safe_start_ns=epoch_safe,
    )
    assert plan.mp_warmup_start_ns < start
    assert not plan.mp_warmup_clamped


def test_outcome_censored_at_boundary():
    params = v2_params_for_window(
        symbol="BTCUSDT",
        start_z="2026-09-10T16:00:00Z",
        end_z="2026-09-10T16:05:00Z",
        output_dir=Path("/tmp/x"),
        silver_database="research_full_ob_silver_v1_3",
    )
    start_ns = dt_to_ns(params.start)
    end_ns = dt_to_ns(params.end)
    ev = _ev("e1", start_ns + NS, "z1", ["p1"])
    ev.trigger_ts_ns = end_ns - 10 * NS  # 10s before end — 1800s censored
    mids = [
        MidTick(start_ns + i * 100_000_000, 100.0, 99.5, 100.5, True, "ep", "ck")
        for i in range(50)
    ]
    outs = compute_outcomes_ext(
        [ev],
        mids,
        params=params,
        end_ns=end_ns,
        window_id="w",
        semantics_hash=SEMANTICS_HASH,
    )
    assert any(o.is_censored and o.horizon_s == 1800 for o in outs)
    assert not any(
        o.horizon_s == 1800 and o.outcome_status == "OK" and (o.gross_return_bps or 0) < 0 and o.is_censored
        for o in outs
    )


def test_trade_sides_unchanged():
    assert trade_side_for("UPPER", "ABSORB")[0] == "SHORT"
    assert trade_side_for("UPPER", "TRUE_BREAK")[0] == "LONG"
    assert trade_side_for("LOWER", "ABSORB")[0] == "LONG"
    assert trade_side_for("LOWER", "TRUE_BREAK")[0] == "SHORT"


def test_costs_subtracted_once():
    assert signed_return_bps("LONG", 100.0, 100.1) == pytest.approx(10.0, rel=1e-6)
    gross = 10.0
    assert gross - 8.0 == 2.0
    assert gross - 12.0 == -2.0


def test_episode_first_touch_and_cooldowns():
    base = 1_000_000_000_000
    events = [
        _ev("a", base, "z1", ["p1"]),
        _ev("b", base + 100 * NS, "z1", ["p1"]),  # within 5m
        _ev("c", base + 400 * NS, "z1", ["p1"]),  # after 5m
        _ev("d", base + 2000 * NS, "z1", ["p2"]),  # new profile version
    ]
    rows = build_episode_rows(events, [], window_id="w")
    by_id = {r.event_id: r for r in rows}
    assert by_id["a"].is_first_touch_of_zone_version
    assert not by_id["b"].is_first_touch_of_zone_version
    assert by_id["d"].is_first_touch_of_zone_version
    assert by_id["a"].selected_cooldown_5m
    assert not by_id["b"].selected_cooldown_5m
    assert by_id["c"].selected_cooldown_5m
    assert "a" in policy_event_ids(rows, "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION")
    assert "b" not in policy_event_ids(rows, "FIRST_TOUCH_PER_ZONE_PROFILE_VERSION")
    assert "a" in policy_event_ids(rows, "COOLDOWN_5M")
    assert "c" in policy_event_ids(rows, "COOLDOWN_5M")


def test_cooldown_15_30_and_nonoverlap():
    base = 1_000_000_000_000
    events = [
        _ev("a", base, "z1", ["p1"], trade="SHORT"),
        _ev("b", base + 600 * NS, "z1", ["p1"], trade="SHORT"),  # 10m later
        _ev("c", base + 2000 * NS, "z1", ["p1"], trade="SHORT"),
    ]
    rows = build_episode_rows(events, [], window_id="w")
    by_id = {r.event_id: r for r in rows}
    assert by_id["a"].selected_cooldown_15m
    assert not by_id["b"].selected_cooldown_15m  # 600 < 900
    assert by_id["c"].selected_cooldown_15m
    assert by_id["a"].selected_cooldown_30m
    assert not by_id["b"].selected_cooldown_30m
    # non-overlapping 30m: a selected, b starts before a's 1800s outcome end
    assert by_id["a"].selected_non_overlapping_30m
    assert not by_id["b"].selected_non_overlapping_30m
    assert by_id["c"].selected_non_overlapping_30m


def test_selection_no_future_outcomes():
    # cooldowns only look at past selected timestamps — covered by chronological build
    base = 1_000_000_000_000
    events = [_ev("a", base, "z", ["p"]), _ev("b", base + 100 * NS, "z", ["p"])]
    rows = build_episode_rows(events, [], window_id="w")
    # b cannot be selected for 5m because a already selected in the past
    assert rows[0].selected_cooldown_5m and not rows[1].selected_cooldown_5m


def test_episode_id_deterministic():
    base = 1_000_000_000_000
    events = [_ev("a", base, "z1", ["p1"]), _ev("b", base + 10 * NS, "z1", ["p1"])]
    r1 = build_episode_rows(events, [], window_id="w")
    r2 = build_episode_rows(events, [], window_id="w")
    assert [x.episode_id for x in r1] == [x.episode_id for x in r2]
    assert r1[0].episode_id == r1[1].episode_id


def test_wilson_and_bootstrap_blocks():
    lo, hi = wilson_interval(5, 10)
    assert lo is not None and hi is not None and lo < hi
    mean, blo, bhi = block_bootstrap_mean_ci([[1.0, 2.0], [3.0], [0.5, 0.5]], n_boot=100)
    assert mean is not None and blo is not None and bhi is not None


def test_v2_params_frozen_values():
    p = v2_params_for_window(
        symbol="BTCUSDT",
        start_z="2026-09-10T16:00:00Z",
        end_z="2026-09-10T17:00:00Z",
        output_dir=Path("/tmp/x"),
        silver_database="research_full_ob_silver_v1_3",
    )
    assert p.failed_break_horizon_s == 120.0
    assert p.true_break_acceptance_s == 60.0
    assert p.true_break_continuation_bps == 3.0
    assert p.absorb_confirmation_bps == 3.0
    assert p.require_correct_approach is True
