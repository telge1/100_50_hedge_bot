from research_charts.live_micro_lld_v2_ch_test.phase11i_parallel_metrics import (
    max_simultaneous_workers,
    parallel_wall_seconds,
)


def test_max_simultaneous_three_overlapping():
    intervals = [(0.0, 10.0), (1.0, 11.0), (2.0, 12.0)]
    assert max_simultaneous_workers(intervals) == 3
    assert parallel_wall_seconds(intervals) == 12.0


def test_max_simultaneous_staggered():
    intervals = [(0.0, 4.9), (5.0, 9.9), (10.0, 14.9)]
    assert max_simultaneous_workers(intervals) == 1
