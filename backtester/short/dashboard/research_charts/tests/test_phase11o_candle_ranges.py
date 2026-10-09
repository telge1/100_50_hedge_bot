"""Sparse candle range index used by Phase 11O."""

from research_charts.live_micro_lld_v2_ch_test.phase11o_candle_ranges import CandleRangeIndex


def test_range_max_min_matches_scan():
    unix = [300 * i for i in range(64)]
    highs = [float(i % 7) for i in range(64)]
    lows = [float(-(i % 5)) for i in range(64)]
    idx = CandleRangeIndex(unix, highs, lows)
    for left in (0, 3, 10, 40):
        for right in (left, left + 1, 20, 63):
            if right >= 64 or right < left:
                continue
            got = idx.query(left, right)
            assert got == (max(highs[left : right + 1]), min(lows[left : right + 1]))


def test_eligible_span_excludes_known_close():
    unix = [0, 300, 600, 900]
    idx = CandleRangeIndex(unix, [1, 2, 3, 4], [1, 2, 3, 4])
    assert idx.eligible_index_span(300, 900) == (2, 3)
    assert idx.eligible_index_span(900, 900) is None
