"""Tests for market profile v1: bin grid, value area, nodes, shape, anchoring.

Pure-function coverage only — no ClickHouse access.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from orderbook_analyse.market_profile import SESSIONS
from orderbook_analyse.market_profile.anchor import build_windows
from orderbook_analyse.market_profile.contracts import ProfileBin, ShapeThresholds
from orderbook_analyse.market_profile.loader import densify_bins, resolve_price_step
from orderbook_analyse.market_profile.profile import compute_value_area, find_nodes
from orderbook_analyse.market_profile.shape import classify_shape


def mk_bins(volumes, *, step=1.0, start_index=0, buy_frac=0.5):
    out = []
    for i, v in enumerate(volumes):
        idx = start_index + i
        lo = idx * step
        out.append(
            ProfileBin(
                bin_index=idx,
                price_low=lo,
                price_high=lo + step,
                price_mid=lo + step / 2.0,
                volume=float(v),
                buy_volume=float(v) * buy_frac,
                sell_volume=float(v) * (1.0 - buy_frac),
                trades=int(v),
                notional=float(v) * (lo + step / 2.0),
            )
        )
    return out


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# price step
# --------------------------------------------------------------------------


def test_price_step_is_nice_number_and_hits_target_bin_count():
    step = resolve_price_step(76824.3, 81499.9, 160)
    assert step == 50.0
    bins = (81499.9 - 76824.3) / step
    assert 60 <= bins <= 200


def test_price_step_scales_to_low_priced_symbol():
    step = resolve_price_step(0.1712, 0.1904, 160)
    assert step == pytest.approx(0.0002, rel=1e-9)
    assert 60 <= (0.1904 - 0.1712) / step <= 200


def test_price_step_rejects_degenerate_range():
    with pytest.raises(ValueError):
        resolve_price_step(100.0, 100.0, 160)
    with pytest.raises(ValueError):
        resolve_price_step(100.0, 200.0, 0)


# --------------------------------------------------------------------------
# densify
# --------------------------------------------------------------------------


def test_densify_fills_gaps_with_zero_volume_bins():
    sparse = [mk_bins([10.0])[0], mk_bins([20.0], start_index=4)[0]]
    dense = densify_bins(sparse, 1.0)
    assert [b.bin_index for b in dense] == [0, 1, 2, 3, 4]
    assert [b.volume for b in dense] == [10.0, 0.0, 0.0, 0.0, 20.0]
    # Edges stay consistent with the grid so the histogram has no drift.
    assert dense[3].price_low == 3.0
    assert dense[3].price_high == 4.0


def test_densify_empty_input():
    assert densify_bins([], 1.0) == []


# --------------------------------------------------------------------------
# value area
# --------------------------------------------------------------------------


def test_value_area_on_symmetric_distribution_is_centred():
    bins = mk_bins([1, 2, 5, 10, 20, 10, 5, 2, 1])
    va = compute_value_area(bins, 0.70)
    assert va.poc == pytest.approx(4.5)
    assert va.poc_volume == 20.0
    assert va.volume_share >= 0.70
    assert va.val < va.poc < va.vah


def test_value_area_expands_toward_the_heavier_neighbour():
    # POC at index 2 (vol 100 of 174); the mass above it dwarfs the mass below.
    bins = mk_bins([1, 1, 100, 40, 30, 1, 1])

    # 70% (target 121.8) is reached after a single upward step onto bin 3.
    va = compute_value_area(bins, 0.70)
    assert va.poc == pytest.approx(2.5)
    assert va.val == pytest.approx(2.0)
    assert va.vah == pytest.approx(4.0)

    # 95% (target 165.3) needs two steps, and both still go up rather than
    # onto the sparse bins below the POC.
    wide = compute_value_area(bins, 0.95)
    assert wide.val == pytest.approx(2.0)
    assert wide.vah == pytest.approx(5.0)


def test_value_area_reaches_requested_share_and_reports_realised():
    bins = mk_bins([5, 5, 5, 5, 5, 5, 5, 5, 5, 5])
    va = compute_value_area(bins, 0.70)
    assert va.volume_share >= 0.70
    # Uniform distribution: 7 of 10 bins are needed for 70%.
    assert va.bin_count == 7


def test_value_area_full_share_covers_every_bin():
    bins = mk_bins([3, 9, 4, 7])
    va = compute_value_area(bins, 1.0)
    assert va.bin_count == 4
    assert va.volume_share == pytest.approx(1.0)
    assert va.val == pytest.approx(0.0)
    assert va.vah == pytest.approx(4.0)


def test_value_area_never_exceeds_available_bins():
    bins = mk_bins([1, 2, 3])
    va = compute_value_area(bins, 0.99)
    assert va.bin_count <= 3
    assert va.val >= bins[0].price_low
    assert va.vah <= bins[-1].price_high


def test_value_area_zero_volume_window_is_degenerate_not_an_error():
    bins = mk_bins([0, 0, 0])
    va = compute_value_area(bins, 0.70)
    assert va.poc_volume == 0.0
    assert va.volume_share == 0.0
    assert va.bin_count == 1


def test_value_area_rejects_bad_inputs():
    with pytest.raises(ValueError):
        compute_value_area([], 0.7)
    with pytest.raises(ValueError):
        compute_value_area(mk_bins([1, 2]), 0.0)
    with pytest.raises(ValueError):
        compute_value_area(mk_bins([1, 2]), 1.5)


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------


def test_hvn_and_lvn_detected_on_double_peak():
    volumes = [1, 2, 30, 40, 30, 2, 1, 1, 30, 45, 30, 2, 1]
    bins = mk_bins(volumes)
    va = compute_value_area(bins, 0.70)
    nodes = find_nodes(
        bins,
        hvn_factor=1.35,
        lvn_factor=0.55,
        min_separation_bins=3,
        single_print_frac=0.04,
        poc_volume=va.poc_volume,
    )
    assert len(nodes.hvn) == 2
    assert any(6.0 <= lvn <= 9.0 for lvn in nodes.lvn), nodes.lvn


def test_single_print_ranges_are_contiguous_and_merged():
    # Zero-volume gap of three bins between two clusters.
    bins = mk_bins([50, 50, 0, 0, 0, 50, 50])
    nodes = find_nodes(
        bins,
        hvn_factor=1.35,
        lvn_factor=0.55,
        min_separation_bins=3,
        single_print_frac=0.04,
        poc_volume=50.0,
    )
    assert nodes.single_print_ranges == ((2.0, 5.0),)


def test_nodes_on_zero_volume_profile_are_empty():
    nodes = find_nodes(
        mk_bins([0, 0, 0]),
        hvn_factor=1.35,
        lvn_factor=0.55,
        min_separation_bins=3,
        single_print_frac=0.04,
        poc_volume=0.0,
    )
    assert nodes.hvn == () and nodes.lvn == () and nodes.single_print_ranges == ()


# --------------------------------------------------------------------------
# shape
# --------------------------------------------------------------------------


def _classify(volumes, *, open_price, close_price, va_pct=0.70):
    bins = mk_bins(volumes)
    va = compute_value_area(bins, va_pct)
    th = ShapeThresholds()
    nodes = find_nodes(
        bins,
        hvn_factor=th.hvn_factor,
        lvn_factor=th.lvn_factor,
        min_separation_bins=th.node_min_separation_bins,
        single_print_frac=th.single_print_frac,
        poc_volume=va.poc_volume,
    )
    return classify_shape(
        value_area=va,
        nodes=nodes,
        price_low=bins[0].price_low,
        price_high=bins[-1].price_high,
        open_price=open_price,
        close_price=close_price,
        total_volume=sum(b.volume for b in bins),
        bin_count=len(bins),
        bins=bins,
        thresholds=th,
    )


def test_balance_profile_is_classified_as_balance_with_letter_d():
    # Sharp central peak, price closes where it opened.
    volumes = [1, 2, 6, 20, 60, 20, 6, 2, 1]
    v = _classify(volumes, open_price=4.2, close_price=4.6)
    assert v.kind == "BALANCE"
    assert v.letter == "D"
    assert 0.35 <= v.poc_position <= 0.65
    assert v.va_range_share <= 0.58


def test_trend_profile_is_classified_by_direction():
    # Volume spread thinly across the whole range, close far above open.
    volumes = [8, 9, 10, 9, 10, 11, 10, 9, 10]
    up = _classify(volumes, open_price=0.4, close_price=8.6)
    assert up.kind == "TREND_UP"
    assert up.directional_share > 0

    down = _classify(volumes, open_price=8.6, close_price=0.4)
    assert down.kind == "TREND_DOWN"
    assert down.directional_share < 0


def test_double_distribution_wins_over_trend_and_balance():
    volumes = [1, 40, 50, 40, 1, 0, 0, 1, 40, 55, 40, 1]
    v = _classify(volumes, open_price=1.5, close_price=9.5)
    assert v.kind == "DOUBLE_DISTRIBUTION"
    assert v.letter == "B"


def test_bumpy_single_distribution_is_not_a_double_distribution():
    # Two separated peaks, but the dip between them is far too shallow to be
    # a real gap: min valley 30 vs weaker peak 50 is 0.60 > 0.35.
    volumes = [2, 30, 50, 40, 35, 30, 32, 38, 50, 45, 30, 2]
    v = _classify(volumes, open_price=5.5, close_price=6.5)
    assert v.kind != "DOUBLE_DISTRIBUTION"
    assert v.letter != "B"


def test_poc_near_top_gives_p_and_near_bottom_gives_b():
    top = [1, 1, 1, 1, 2, 3, 20, 60, 25]
    assert _classify(top, open_price=0.5, close_price=7.5).letter == "P"
    bottom = [25, 60, 20, 3, 2, 1, 1, 1, 1]
    assert _classify(bottom, open_price=7.5, close_price=0.5).letter == "b"


def test_shape_reports_metrics_even_when_unclear():
    # Wide value area but no direction: neither trend nor balance.
    volumes = [10, 10, 10, 10, 10, 10, 10, 10, 10]
    v = _classify(volumes, open_price=4.4, close_price=4.6)
    assert v.kind == "UNCLEAR"
    assert v.va_range_share > 0
    assert any("va_range_share" in r for r in v.reasons)


def test_degenerate_window_is_unclear_not_a_crash():
    from orderbook_analyse.market_profile.contracts import NodeSet, ValueArea

    v = classify_shape(
        value_area=ValueArea(
            poc=1.0,
            poc_volume=0.0,
            poc_bin_index=0,
            vah=1.0,
            val=1.0,
            requested_share=0.7,
            volume_share=0.0,
            bin_count=1,
        ),
        nodes=NodeSet(hvn=(), lvn=(), single_print_ranges=()),
        price_low=1.0,
        price_high=1.0,
        open_price=1.0,
        close_price=1.0,
        total_volume=0.0,
        bin_count=1,
    )
    assert v.kind == "UNCLEAR"
    assert v.letter == "-"


# --------------------------------------------------------------------------
# anchoring
# --------------------------------------------------------------------------


def test_day_anchor_emits_one_window_per_utc_day():
    ws = build_windows(
        anchor_mode="day", start=utc(2026, 8, 24), end=utc(2026, 8, 27)
    )
    assert [w.label for w in ws] == ["2026-08-24", "2026-08-25", "2026-08-26"]
    assert all(w.anchor_mode == "day" for w in ws)
    assert ws[0].start == utc(2026, 8, 24)
    assert ws[0].end == utc(2026, 8, 25)


def test_day_anchor_clips_partial_edges_without_phantom_windows():
    ws = build_windows(
        anchor_mode="day",
        start=utc(2026, 8, 24, 18, 0),
        end=utc(2026, 8, 26, 3, 0),
    )
    assert len(ws) == 3
    assert ws[0].start == utc(2026, 8, 24, 18, 0)
    assert ws[-1].end == utc(2026, 8, 26, 3, 0)
    assert all(w.end > w.start for w in ws)


def test_composite_anchor_is_a_single_window_spanning_the_range():
    ws = build_windows(
        anchor_mode="composite", start=utc(2026, 8, 20), end=utc(2026, 8, 31)
    )
    assert len(ws) == 1
    assert ws[0].start == utc(2026, 8, 20)
    assert ws[0].end == utc(2026, 8, 31)
    assert ws[0].window_id == "composite"


def test_session_anchor_covers_the_day_without_overlap():
    ws = build_windows(
        anchor_mode="session", start=utc(2026, 8, 25), end=utc(2026, 8, 26)
    )
    assert len(ws) == len(SESSIONS)
    ws_sorted = sorted(ws, key=lambda w: w.start)
    # Contiguous and non-overlapping across the day.
    for a, b in zip(ws_sorted, ws_sorted[1:]):
        assert a.end == b.start
    assert ws_sorted[0].start == utc(2026, 8, 25)
    assert ws_sorted[-1].end == utc(2026, 8, 26)


def test_session_anchor_can_select_a_subset():
    ws = build_windows(
        anchor_mode="session",
        start=utc(2026, 8, 25),
        end=utc(2026, 8, 27),
        sessions=("us",),
    )
    assert len(ws) == 2
    assert all("US" in w.label for w in ws)
    assert ws[0].start == utc(2026, 8, 25, 13, 30)
    assert ws[0].end == utc(2026, 8, 25, 20, 0)


def test_session_anchor_includes_late_session_crossing_midnight():
    # A window starting at 22:00 sits inside the previous day's `late` session.
    ws = build_windows(
        anchor_mode="session",
        start=utc(2026, 8, 25, 22, 0),
        end=utc(2026, 8, 26, 2, 0),
        sessions=("late", "asia"),
    )
    labels = [w.label for w in ws]
    assert any("2026-08-25 LATE" in x for x in labels)
    assert any("2026-08-26 ASIA" in x for x in labels)
    assert ws[0].start == utc(2026, 8, 25, 22, 0)


def test_period_anchor_emits_utc_aligned_blocks():
    ws = build_windows(
        anchor_mode="1h",
        start=utc(2026, 8, 25, 10, 0),
        end=utc(2026, 8, 25, 13, 0),
    )
    assert len(ws) == 3
    assert [w.start for w in ws] == [
        utc(2026, 8, 25, 10, 0),
        utc(2026, 8, 25, 11, 0),
        utc(2026, 8, 25, 12, 0),
    ]
    assert ws[-1].end == utc(2026, 8, 25, 13, 0)
    assert all(w.anchor_mode == "1h" for w in ws)


def test_period_anchor_clips_partial_edges_and_keeps_forming():
    ws = build_windows(
        anchor_mode="4h",
        start=utc(2026, 8, 25, 5, 0),
        end=utc(2026, 8, 25, 14, 0),
    )
    # 04:00–08:00 (clipped), 08:00–12:00, 12:00–16:00 (clipped to 14:00)
    assert len(ws) == 3
    assert ws[0].start == utc(2026, 8, 25, 5, 0)
    assert ws[0].end == utc(2026, 8, 25, 8, 0)
    assert ws[1].start == utc(2026, 8, 25, 8, 0)
    assert ws[-1].start == utc(2026, 8, 25, 12, 0)
    assert ws[-1].end == utc(2026, 8, 25, 14, 0)


def test_period_anchor_15m_aligns_to_clock():
    ws = build_windows(
        anchor_mode="15m",
        start=utc(2026, 8, 25, 10, 7),
        end=utc(2026, 8, 25, 10, 45),
    )
    assert [w.start for w in ws] == [
        utc(2026, 8, 25, 10, 7),   # clipped from 10:00
        utc(2026, 8, 25, 10, 15),
        utc(2026, 8, 25, 10, 30),
    ]
    assert ws[0].end == utc(2026, 8, 25, 10, 15)
    assert ws[-1].end == utc(2026, 8, 25, 10, 45)


def test_anchor_rejects_bad_input():
    with pytest.raises(ValueError):
        build_windows(anchor_mode="day", start=utc(2026, 8, 26), end=utc(2026, 8, 25))
    with pytest.raises(ValueError):
        build_windows(anchor_mode="nope", start=utc(2026, 8, 25), end=utc(2026, 8, 26))
    with pytest.raises(ValueError):
        build_windows(
            anchor_mode="session",
            start=utc(2026, 8, 25),
            end=utc(2026, 8, 26),
            sessions=("nope",),
        )


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------


def test_no_execution_or_write_paths_imported():
    import pathlib

    pkg = pathlib.Path(
        __import__("orderbook_analyse.market_profile", fromlist=["x"]).__file__
    ).parent
    banned = ("pybit", "order_create", "place_order", "insert(", "trading_api", "api_key")
    for py in pkg.glob("*.py"):
        src = py.read_text(encoding="utf-8").lower()
        for token in banned:
            assert token not in src, f"{py.name} must not reference {token}"
