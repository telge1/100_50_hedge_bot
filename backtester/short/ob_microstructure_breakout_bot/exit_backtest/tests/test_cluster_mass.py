"""Unit tests for cluster-mass grouping (no market data required)."""

from __future__ import annotations

import pytest

from ob_microstructure_breakout_bot.exit_backtest.cluster_mass import (
    PoolSnap,
    group_pools_into_clusters,
    reachability_bucket,
)


def test_group_nearby_pools_into_one_cluster() -> None:
    entry = 0.10
    pools = [
        PoolSnap(0.1010, 0.1012, 1.0),
        PoolSnap(0.10125, 0.1014, 2.0),  # ~0.05% gap -> merge
        PoolSnap(0.1030, 0.1035, 5.0),  # far -> new cluster
    ]
    clusters = group_pools_into_clusters(pools, entry, gap_pct=0.10)
    assert len(clusters) == 2
    assert clusters[0].n_pools == 2
    assert clusters[0].strength_sum == 3.0
    assert clusters[1].n_pools == 1
    assert clusters[1].strength_sum == 5.0
    assert clusters[1].dist_from_entry_pct == pytest.approx(3.0)


def test_nested_slices_merge_into_envelope() -> None:
    """Thin inner bands must not split a wide compact pool."""
    entry = 0.08976
    pools = [
        PoolSnap(0.086655, 0.086920, 0.0),
        PoolSnap(0.086755, 0.088140, 10.0),
        PoolSnap(0.087230, 0.087310, 0.0),
        PoolSnap(0.087495, 0.087830, 1.439),
        PoolSnap(0.088175, 0.088440, 2.218),
        PoolSnap(0.088430, 0.088640, 3.494),
    ]
    clusters = group_pools_into_clusters(pools, entry, gap_pct=0.10)
    assert len(clusters) == 1
    assert clusters[0].bottom == pytest.approx(0.086655)
    assert clusters[0].top == pytest.approx(0.088640)
    assert clusters[0].n_pools == 6


def test_thin_gap_inside_compact_stack_merges() -> None:
    """A 0.23% hole between two bands is still one pool for the stop."""
    entry = 0.09043
    pools = [
        PoolSnap(0.091050, 0.091575, 6.2),
        PoolSnap(0.091790, 0.092105, 6.4),
        PoolSnap(0.090500, 0.090795, 3.7),
    ]
    clusters = group_pools_into_clusters(pools, entry)
    assert len(clusters) == 2
    upper = max(clusters, key=lambda c: c.top)
    assert upper.bottom == pytest.approx(0.091050)
    assert upper.top == pytest.approx(0.092105)
    assert upper.top * 1.002 == pytest.approx(0.09228921)


def test_overlapping_pools_merge() -> None:
    entry = 1.0
    pools = [
        PoolSnap(1.01, 1.02, 1.0),
        PoolSnap(1.015, 1.025, 2.0),
    ]
    clusters = group_pools_into_clusters(pools, entry, gap_pct=0.10)
    assert len(clusters) == 1
    assert clusters[0].bottom == 1.01
    assert clusters[0].top == 1.025
    assert clusters[0].width_pct == pytest.approx(1.5)


def test_reachability_bucket_far_needs_strong_delta() -> None:
    assert (
        reachability_bucket(
            dist_pct=2.0,
            strength_sum=5.0,
            confirm_delta=100_000,
            entry_ob=1.2,
            reached=False,
        )
        == "praktisch_nicht_erreichbar"
    )
    assert (
        reachability_bucket(
            dist_pct=0.3,
            strength_sum=2.0,
            confirm_delta=100_000,
            entry_ob=1.2,
            reached=True,
        )
        == "leicht_erreichbar"
    )
