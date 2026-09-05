"""Tests for the validation harness.

The point of these is to make a wrong result visible: barrier ordering,
ambiguity handling, touch eligibility, causality, and the difference between
naive and clustered uncertainty.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from orderbook_analyse.market_profile.contracts import (
    MarketProfile,
    NodeSet,
    ProfileBin,
    ProfileWindow,
    ShapeVerdict,
    ValueArea,
)
from orderbook_analyse.market_profile_validation import (
    H1_BROKE,
    H1_REJECTED,
    H2_CONTINUED,
    H2_REVERSED,
    OUTCOME_AMBIGUOUS,
    OUTCOME_TIMEOUT,
)
from orderbook_analyse.market_profile_validation.events import (
    RACE_DOWN,
    RACE_UP,
    build_pair_events,
    excursion,
    race_barriers,
)
from orderbook_analyse.market_profile_validation.stats import (
    cluster_bootstrap_interval,
    difference_bootstrap,
    estimate_rate,
    wilson_interval,
)

T0 = datetime(2026, 8, 25, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# barrier race
# --------------------------------------------------------------------------


def test_race_returns_the_barrier_that_is_reached_first():
    # Upper barrier 11.5 falls on bar 2; the lower one only on bar 3.
    assert race_barriers(
        [10.0, 10.0, 12.0, 20.0], [9.8, 9.7, 9.6, 5.0], 0, 11.5, 6.0, 3
    ) == (RACE_UP, 2)

    # Mirror: lower barrier 8.5 falls on bar 2, upper only on bar 3. Bar 3
    # would straddle both, so resolving at bar 2 also proves the walk stops
    # at the first decision rather than scanning to the end.
    assert race_barriers(
        [10.0, 10.0, 10.5, 20.0], [9.8, 9.7, 8.0, 5.0], 0, 11.5, 8.5, 3
    ) == (RACE_DOWN, 2)


def test_race_reports_timeout_when_neither_barrier_is_reached():
    highs = [10.0, 10.1, 10.2]
    lows = [9.9, 9.8, 9.7]
    assert race_barriers(highs, lows, 0, 20.0, 1.0, 2) == (OUTCOME_TIMEOUT, None)


def test_race_flags_a_bar_that_reaches_both_barriers():
    # OHLC cannot order two hits inside one bar, so this must not be guessed.
    highs = [10.0, 30.0]
    lows = [9.0, 1.0]
    assert race_barriers(highs, lows, 0, 20.0, 5.0, 1) == (OUTCOME_AMBIGUOUS, 1)


def test_race_respects_start_and_end_bounds():
    highs = [50.0, 10.0, 10.0, 50.0]
    lows = [1.0, 9.0, 9.0, 1.0]
    # Bar 0 would resolve immediately but the walk starts at bar 1.
    assert race_barriers(highs, lows, 1, 20.0, 5.0, 2) == (OUTCOME_TIMEOUT, None)
    # Extending the end lets bar 3 resolve it.
    assert race_barriers(highs, lows, 1, 20.0, 5.0, 3)[0] == OUTCOME_AMBIGUOUS


def test_race_rejects_inverted_barriers():
    with pytest.raises(ValueError):
        race_barriers([1.0], [1.0], 0, 5.0, 9.0, 0)


# --------------------------------------------------------------------------
# excursion
# --------------------------------------------------------------------------


def test_excursion_is_signed_by_the_favorable_direction():
    highs = [12.0, 15.0]
    lows = [8.0, 7.0]
    up_fav = excursion(highs, lows, 0, 1, 10.0, +1)
    assert up_fav == (5.0, 3.0)
    down_fav = excursion(highs, lows, 0, 1, 10.0, -1)
    assert down_fav == (3.0, 5.0)


def test_excursion_is_zero_on_an_empty_range():
    assert excursion([1.0], [1.0], 5, 2, 1.0, +1) == (0.0, 0.0)


# --------------------------------------------------------------------------
# synthetic profile helper
# --------------------------------------------------------------------------


def mk_profile(
    *,
    poc=100.0,
    vah=110.0,
    val=90.0,
    low=80.0,
    high=120.0,
    open_price=95.0,
    close_price=105.0,
    kind="BALANCE",
    trades=10_000,
    window_id="day_2026-08-25",
    label="2026-08-25",
):
    bins = tuple(
        ProfileBin(
            bin_index=i,
            price_low=low + i,
            price_high=low + i + 1,
            price_mid=low + i + 0.5,
            volume=1.0,
            buy_volume=0.5,
            sell_volume=0.5,
            trades=1,
            notional=1.0,
        )
        for i in range(int(high - low))
    )
    return MarketProfile(
        symbol="TESTUSDT",
        window=ProfileWindow(
            window_id=window_id,
            anchor_mode="day",
            label=label,
            start=T0,
            end=T0 + timedelta(days=1),
        ),
        price_step=1.0,
        price_low=low,
        price_high=high,
        open_price=open_price,
        close_price=close_price,
        total_volume=float(len(bins)),
        buy_volume=float(len(bins)) / 2,
        sell_volume=float(len(bins)) / 2,
        trades=trades,
        notional=float(len(bins)),
        bins=bins,
        value_area=ValueArea(
            poc=poc,
            poc_volume=1.0,
            poc_bin_index=0,
            vah=vah,
            val=val,
            requested_share=0.7,
            volume_share=0.7,
            bin_count=len(bins),
        ),
        nodes=NodeSet(hvn=(), lvn=(), single_print_ranges=()),
        shape=ShapeVerdict(
            kind=kind,
            letter="D",
            poc_position=0.5,
            va_range_share=0.5,
            poc_concentration=3.0,
            directional_share=0.25,
            reasons=(),
        ),
    )


def bars(seq):
    """`seq` is a list of (open, high, low) triples on a 1m grid."""
    times = [T0 + timedelta(days=1, minutes=i) for i in range(len(seq))]
    return (
        times,
        [s[0] for s in seq],
        [s[1] for s in seq],
        [s[2] for s in seq],
    )


def run_pair(profile, seq, *, edge=(0.10,), unit=(0.15,), horizon=0):
    times, opens, highs, lows = bars(seq)
    return build_pair_events(
        symbol="TESTUSDT",
        profile=profile,
        test_window_id="day_2026-08-26",
        times=times,
        opens=opens,
        highs=highs,
        lows=lows,
        edge_margin_fracs=edge,
        poc_unit_fracs=unit,
        max_horizon_bars=horizon,
    )


# --------------------------------------------------------------------------
# H1 — value-area edges
# --------------------------------------------------------------------------


def test_h1_vah_rejection_is_scored_when_price_returns_to_the_poc():
    # Range 40 -> margin 4, so the break barrier sits at 114.
    p = mk_profile()
    seq = [
        (100.0, 101.0, 99.0),   # inside
        (101.0, 110.5, 100.0),  # touches VAH 110
        (110.0, 111.0, 105.0),
        (105.0, 106.0, 99.9),   # back through POC 100
    ]
    events, _ = run_pair(p, seq)
    h1 = [e for e in events if e.hypothesis == "H1" and e.level_kind == "VAH"]
    assert len(h1) == 1
    assert h1[0].outcome == H1_REJECTED
    assert h1[0].target_price == 100.0
    assert h1[0].stop_price == pytest.approx(114.0)


def test_h1_vah_break_is_scored_when_the_margin_is_exceeded_first():
    p = mk_profile()
    seq = [
        (100.0, 101.0, 99.0),
        (101.0, 110.5, 100.0),  # touch
        (110.0, 114.5, 109.0),  # clears 114 before returning to 100
    ]
    events, _ = run_pair(p, seq)
    h1 = [e for e in events if e.level_kind == "VAH"]
    assert h1[0].outcome == H1_BROKE


def test_h1_val_mirror_case():
    p = mk_profile()
    seq = [
        (100.0, 101.0, 99.0),
        (99.0, 100.0, 89.5),   # touches VAL 90
        (90.0, 100.5, 89.0),   # back up through POC 100
    ]
    events, _ = run_pair(p, seq)
    val = [e for e in events if e.level_kind == "VAL"]
    assert len(val) == 1
    assert val[0].outcome == H1_REJECTED
    assert val[0].stop_price == pytest.approx(86.0)


def test_h1_skips_the_edge_when_the_test_window_opens_beyond_it():
    # A gap straight through the edge is not a fade setup, so it is not scored.
    p = mk_profile()
    seq = [
        (115.0, 116.0, 114.0),
        (115.0, 117.0, 110.0),
    ]
    events, _ = run_pair(p, seq)
    assert [e for e in events if e.level_kind == "VAH"] == []


def test_h1_touch_bar_itself_cannot_resolve_the_race():
    # The touch bar spans both barriers; the race must start after it and
    # therefore time out rather than read its own trigger bar.
    p = mk_profile()
    seq = [
        (100.0, 101.0, 99.0),
        (101.0, 120.0, 95.0),  # touches VAH and would hit both barriers
        (100.5, 101.0, 100.2),
    ]
    events, _ = run_pair(p, seq)
    vah = [e for e in events if e.level_kind == "VAH"][0]
    assert vah.outcome == OUTCOME_TIMEOUT


# --------------------------------------------------------------------------
# H2 — POC as a way station
# --------------------------------------------------------------------------


def test_h2_continuation_follows_the_reference_direction_up():
    # close > open -> direction +1, unit = 0.15 * 40 = 6, barriers 106 / 94.
    p = mk_profile(open_price=95.0, close_price=105.0, kind="TREND_UP")
    seq = [
        (90.0, 91.0, 89.0),
        (91.0, 100.5, 99.5),   # touches POC 100
        (100.0, 106.5, 99.0),  # clears 106 first
    ]
    events, _ = run_pair(p, seq)
    h2 = [e for e in events if e.hypothesis == "H2"]
    assert len(h2) == 1
    assert h2[0].outcome == H2_CONTINUED
    assert h2[0].favorable_sign == 1


def test_h2_direction_is_inverted_for_a_down_reference():
    p = mk_profile(open_price=105.0, close_price=95.0, kind="TREND_DOWN")
    seq = [
        (110.0, 111.0, 109.0),
        (109.0, 100.5, 99.5),  # touches POC
        (100.0, 101.0, 93.5),  # clears 94 downward -> continuation
    ]
    events, _ = run_pair(p, seq)
    h2 = [e for e in events if e.hypothesis == "H2"]
    assert h2[0].outcome == H2_CONTINUED
    assert h2[0].favorable_sign == -1

    # Same bars but an up reference: the identical move is now a reversal.
    p_up = mk_profile(open_price=95.0, close_price=105.0, kind="TREND_UP")
    events_up, _ = run_pair(p_up, seq)
    assert [e for e in events_up if e.hypothesis == "H2"][0].outcome == H2_REVERSED


def test_h2_is_skipped_when_the_reference_window_has_no_direction():
    p = mk_profile(open_price=100.0, close_price=100.0)
    seq = [(90.0, 100.5, 89.0), (100.0, 106.0, 99.0)]
    events, _ = run_pair(p, seq)
    assert [e for e in events if e.hypothesis == "H2"] == []


# --------------------------------------------------------------------------
# H3 — POC revisit
# --------------------------------------------------------------------------


def test_the_margin_grid_emits_one_event_per_setting():
    p = mk_profile()
    seq = [
        (100.0, 101.0, 99.0),
        (101.0, 110.5, 100.0),
        (110.0, 111.0, 105.0),
    ]
    events, _ = run_pair(p, seq, edge=(0.05, 0.20))
    vah = [e for e in events if e.level_kind == "VAH"]
    assert {e.variant for e in vah} == {"margin_0.05", "margin_0.20"}
    # Same touch bar, so the grid must not multiply the number of touches.
    assert len({e.touch_ts for e in vah}) == 1


def test_a_wider_stop_can_only_help_the_level_hold():
    # Range 40. Bar 2 reaches 112.5, which breaks a 0.05 stop (112) but not a
    # 0.20 stop (118); bar 3 then reaches the POC.
    p = mk_profile()
    seq = [
        (100.0, 101.0, 99.0),
        (101.0, 110.5, 100.0),
        (110.0, 112.5, 109.0),
        (110.0, 111.0, 99.5),
    ]
    events, _ = run_pair(p, seq, edge=(0.05, 0.20))
    by_variant = {e.variant: e.outcome for e in events if e.level_kind == "VAH"}
    assert by_variant["margin_0.05"] == H1_BROKE
    assert by_variant["margin_0.20"] == H1_REJECTED


def test_reward_risk_and_breakeven_follow_the_barrier_geometry():
    # VAH 110, POC 100 -> reward 10. Margin 0.05 * 40 = 2 -> risk 2, RR 5.
    p = mk_profile()
    seq = [(100.0, 101.0, 99.0), (101.0, 110.5, 100.0), (110.0, 111.0, 109.0)]
    events, _ = run_pair(p, seq, edge=(0.05,))
    e = [x for x in events if x.level_kind == "VAH"][0]
    assert e.reward_risk == pytest.approx(5.0)
    assert e.breakeven_rate == pytest.approx(1 / 6)


def test_h3_records_a_revisit_with_its_timing():
    p = mk_profile()
    seq = [(90.0, 91.0, 89.0), (91.0, 95.0, 90.0), (95.0, 100.5, 94.0)]
    _, revisit = run_pair(p, seq)
    assert revisit is not None
    assert revisit.revisited is True
    assert revisit.minutes_to_revisit == 2


def test_h3_records_a_miss():
    p = mk_profile()
    seq = [(90.0, 91.0, 89.0), (91.0, 92.0, 88.0)]
    _, revisit = run_pair(p, seq)
    assert revisit.revisited is False
    assert revisit.minutes_to_revisit is None
    assert revisit.revisit_ts is None
    assert revisit.revisited_60m is False


def test_h3_short_horizons_separate_a_late_revisit_from_an_early_one():
    # A touch at minute 100 counts for 240m and the full window, not for 60m.
    p = mk_profile()
    seq = [(90.0, 91.0, 89.0)] * 100 + [(91.0, 100.5, 90.0)]
    _, revisit = run_pair(p, seq)
    assert revisit.minutes_to_revisit == 100
    assert revisit.revisited_60m is False
    assert revisit.revisited_240m is True
    assert revisit.revisited is True


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------


def test_thin_or_degenerate_windows_produce_nothing():
    flat = mk_profile(low=100.0, high=100.0)
    events, revisit = run_pair(flat, [(100.0, 100.0, 100.0)])
    assert events == [] and revisit is None

    p = mk_profile()
    events, revisit = run_pair(p, [])
    assert events == [] and revisit is None


def test_horizon_cap_limits_the_walk():
    p = mk_profile()
    seq = [
        (100.0, 101.0, 99.0),
        (101.0, 110.5, 100.0),  # touch at bar 1
        (110.0, 111.0, 109.0),
        (110.0, 111.0, 99.0),   # would reject here, 3 bars after the touch
    ]
    capped, _ = run_pair(p, seq, horizon=1)
    assert [e for e in capped if e.level_kind == "VAH"][0].outcome == OUTCOME_TIMEOUT
    full, _ = run_pair(p, seq, horizon=0)
    assert [e for e in full if e.level_kind == "VAH"][0].outcome == H1_REJECTED


def test_naked_poc_is_never_read_as_a_feature():
    # It is computed by looking forward, so using it as an input would leak.
    import inspect

    from orderbook_analyse.market_profile_validation import events as ev
    from orderbook_analyse.market_profile_validation import runner as rn

    for mod in (ev, rn):
        src = inspect.getsource(mod)
        assert "naked_poc" not in src, f"{mod.__name__} must not read naked_poc"


def test_events_only_reference_the_reference_window_class():
    # A profile's own outcome fields must not appear in the event contract.
    from orderbook_analyse.market_profile_validation.contracts import TouchEvent

    fields = set(TouchEvent.__dataclass_fields__)
    for leaky in ("naked_poc", "poc_revisit_ts", "naked_checked_until"):
        assert leaky not in fields


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------


def test_wilson_interval_brackets_the_point_estimate():
    lo, hi = wilson_interval(50, 100)
    assert lo < 0.5 < hi
    assert 0.0 <= lo and hi <= 1.0
    assert wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_narrows_as_the_sample_grows():
    narrow = wilson_interval(500, 1000)
    wide = wilson_interval(5, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


def test_cluster_bootstrap_is_wider_than_wilson_when_clusters_disagree():
    # Ten symbols, five always succeed and five always fail: pooled rate 0.5
    # with a tight Wilson band, but the cluster view must stay wide because
    # the outcome depends entirely on which symbols were drawn.
    groups = {f"s{i}": ((100, 100) if i < 5 else (0, 100)) for i in range(10)}
    c_lo, c_hi = cluster_bootstrap_interval(groups, iters=2000, seed=1)
    w_lo, w_hi = wilson_interval(500, 1000)
    assert (c_hi - c_lo) > (w_hi - w_lo) * 3


def test_cluster_bootstrap_needs_at_least_two_clusters():
    assert cluster_bootstrap_interval({"only": (5, 10)}, iters=100, seed=1) == (0.0, 1.0)


class _Ev:
    def __init__(self, symbol, label, outcome):
        self.symbol = symbol
        self.ref_label = label
        self.outcome = outcome
        self.mfe_frac = 0.1
        self.mae_frac = 0.1


def test_estimate_rate_excludes_timeouts_and_ambiguous_from_the_denominator():
    events = [
        _Ev("A", "2026-08-01", H1_REJECTED),
        _Ev("A", "2026-08-02", H1_BROKE),
        _Ev("B", "2026-08-01", H1_REJECTED),
        _Ev("B", "2026-08-02", OUTCOME_TIMEOUT),
        _Ev("B", "2026-08-03", OUTCOME_AMBIGUOUS),
    ]
    est = estimate_rate(
        "test",
        events,
        success_outcome=H1_REJECTED,
        failure_outcome=H1_BROKE,
        symbol_key=lambda e: e.symbol,
        date_key=lambda e: e.ref_label,
        iters=200,
        seed=1,
    )
    assert est.trials == 3
    assert est.successes == 2
    assert est.timeouts == 1
    assert est.ambiguous == 1
    # Worst case folds the ambiguous race into the denominator as a failure.
    assert est.worst_case_rate == pytest.approx(2 / 4)


# --------------------------------------------------------------------------
# economics
# --------------------------------------------------------------------------


def test_costs_can_turn_a_positive_edge_into_a_losing_expectancy():
    from orderbook_analyse.market_profile_validation.report import _economics

    # Entry 100, target 110, stop 98 -> reward/risk 5, breakeven 1/6 = 0.167.
    # A 0.25 hit rate clears that gross, but the risk unit is only 2 in price
    # terms, so a 50 bps round trip costs 0.25 R per trade.
    class E:
        level_price = 100.0
        stop_price = 98.0
        target_price = 110.0
        reward_risk = 5.0

    events = [E()] * 20
    free = _economics(events, 0.25, cost_bps=0.0)
    assert free["edge"] > 0
    assert free["expectancy_r_gross"] == pytest.approx(0.25 * 5 - 0.75)
    assert free["expectancy_r_net"] == pytest.approx(free["expectancy_r_gross"])

    charged = _economics(events, 0.25, cost_bps=50.0)
    assert charged["cost_in_risk_units_median"] == pytest.approx(0.25)
    assert charged["expectancy_r_net"] < free["expectancy_r_gross"]
    assert charged["breakeven_rate_with_costs"] > charged["breakeven_rate"]


def test_a_tighter_stop_makes_the_same_fee_hurt_more():
    from orderbook_analyse.market_profile_validation.report import _economics

    class Wide:
        level_price = 100.0
        stop_price = 90.0
        target_price = 110.0
        reward_risk = 1.0

    class Tight:
        level_price = 100.0
        stop_price = 99.0
        target_price = 110.0
        reward_risk = 10.0

    wide = _economics([Wide()] * 10, 0.5, cost_bps=20.0)
    tight = _economics([Tight()] * 10, 0.5, cost_bps=20.0)
    assert tight["cost_in_risk_units_median"] > wide["cost_in_risk_units_median"]


# --------------------------------------------------------------------------
# H3 confound control
# --------------------------------------------------------------------------


class _Rev:
    def __init__(self, symbol, label, kind, distance, revisited):
        self.symbol = symbol
        self.ref_label = label
        self.ref_shape_kind = kind
        self.poc_distance_frac = distance
        self.revisited_240m = revisited
        self.revisited = revisited


def _distance_grid():
    return [0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75]


def test_distance_control_reports_geometry_when_only_distance_drives_revisits():
    from orderbook_analyse.market_profile_validation.report import h3_distance_control

    # Revisits depend on distance alone; the class carries no extra signal.
    # Both groups span the same distances, so the strata can separate the two.
    events = []
    for sym in range(8):
        for d in _distance_grid():
            for kind in ("BALANCE", "TREND_UP"):
                events.append(
                    _Rev(f"s{sym}", f"2026-08-{sym+1:02d}", kind, d, d < 0.4)
                )
    dc = h3_distance_control(events, iters=400, seed=5)
    assert dc["strata"], "strata should be testable with overlapping distances"
    assert dc["verdict"] == "EXPLAINED_BY_DISTANCE"


def test_distance_control_confirms_an_effect_that_is_independent_of_distance():
    from orderbook_analyse.market_profile_validation.report import h3_distance_control

    events = []
    for sym in range(8):
        for d in _distance_grid():
            events.append(
                _Rev(f"s{sym}", f"2026-08-{sym+1:02d}", "BALANCE", d, True)
            )
            events.append(
                _Rev(f"s{sym}", f"2026-08-{sym+1:02d}", "TREND_DOWN", d, False)
            )
    dc = h3_distance_control(events, iters=400, seed=5)
    assert dc["verdict"] == "SURVIVES_DISTANCE_CONTROL"
    assert dc["strata_supported"] == dc["strata_total"]


def test_distance_control_declines_to_judge_a_tiny_sample():
    from orderbook_analyse.market_profile_validation.report import h3_distance_control

    events = [_Rev("s0", "d", "BALANCE", 0.1, True) for _ in range(10)]
    dc = h3_distance_control(events, iters=100, seed=1)
    assert dc["strata"] == []


def test_difference_bootstrap_finds_no_effect_when_groups_are_identical():
    a = [_Ev(f"s{i%5}", "d", H1_REJECTED if i % 2 else H1_BROKE) for i in range(40)]
    b = [_Ev(f"s{i%5}", "d", H1_REJECTED if i % 2 else H1_BROKE) for i in range(40)]
    point, lo, hi = difference_bootstrap(
        a,
        b,
        flag=lambda e: e.outcome == H1_REJECTED,
        cluster_key=lambda e: e.symbol,
        iters=500,
        seed=3,
    )
    assert point == pytest.approx(0.0, abs=1e-9)
    assert lo <= 0.0 <= hi


def test_difference_bootstrap_detects_a_real_gap():
    a = [_Ev(f"s{i%8}", "d", H1_REJECTED) for i in range(80)]
    b = [_Ev(f"s{i%8}", "d", H1_BROKE) for i in range(80)]
    point, lo, hi = difference_bootstrap(
        a,
        b,
        flag=lambda e: e.outcome == H1_REJECTED,
        cluster_key=lambda e: e.symbol,
        iters=500,
        seed=3,
    )
    assert point == pytest.approx(1.0)
    assert lo > 0.5
