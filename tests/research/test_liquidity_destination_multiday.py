"""Operational safety and chunk-parity tests for Phase 1C."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research.liquidity_destination_bias.builder import BuildConfig, build_episodes
from research.liquidity_destination_bias.contract import Outcome, contract_dict
from research.liquidity_destination_bias.models import PriceBucket
from research.liquidity_destination_bias.sources import TradeSecondSeries

UTC = timezone.utc
T0 = datetime(2026, 8, 25, 0, 5, tzinfo=UTC)


class StaticProvider:
    def snapshot(self, symbol, t0):
        available = (t0 - timedelta(minutes=2)).isoformat().replace("+00:00", "Z")
        return {
            "active_canonical_pools": [
                {
                    "pool_id": "upper",
                    "side": "ASK",
                    "lower": 105,
                    "upper": 106,
                    "available_at": available,
                    "active_as_of": True,
                },
                {
                    "pool_id": "lower",
                    "side": "BID",
                    "lower": 94,
                    "upper": 95,
                    "available_at": available,
                    "active_as_of": True,
                },
            ],
            "canonical_provider_version": "liquidity_pool_signal/canonical_v1",
            "canonical_snapshot_sha256": f"snapshot-{t0.isoformat()}",
        }


class SlicingLoader:
    def __init__(self, buckets):
        self.buckets = buckets
        self.calls = []

    def __call__(self, *, symbol, start, end):
        self.calls.append((symbol, start, end))
        selected = {ts: row for ts, row in self.buckets.items() if start <= ts < end}
        return TradeSecondSeries(
            selected,
            min(selected) if selected else None,
            max(selected) + timedelta(seconds=1) if selected else None,
            True,
        )


def price_buckets(*, minutes=12, upper_touch_second=None):
    rows = {}
    cursor = T0 - timedelta(minutes=5)
    end = T0 + timedelta(minutes=minutes)
    while cursor < end:
        offset = int((cursor - T0).total_seconds())
        high = 105 if offset == upper_touch_second else 101
        rows[cursor] = PriceBucket(cursor, 99, high, 100, 1)
        cursor += timedelta(seconds=1)
    return rows


def long_config(*, minutes=6, chunk_seconds=120, max_hours=1):
    return BuildConfig(
        symbol="BTCUSDT",
        start=T0,
        end=T0 + timedelta(minutes=minutes),
        horizon_minutes=5,
        require_complete=True,
        allow_bounded_expand=True,
        max_window_hours=max_hours,
        trade_chunk_seconds=chunk_seconds,
    )


def build_chunked(*, minutes=6, chunk_seconds=120, upper_touch_second=180):
    loader = SlicingLoader(
        price_buckets(
            minutes=minutes + 5,
            upper_touch_second=upper_touch_second,
        )
    )
    result = build_episodes(
        long_config(minutes=minutes, chunk_seconds=chunk_seconds),
        provider=StaticProvider(),
        trade_loader=loader,
    )
    return result, loader


def test_two_hour_default_remains_active():
    config = BuildConfig(
        symbol="BTCUSDT",
        start=T0,
        end=T0 + timedelta(hours=2),
        horizon_minutes=60,
        require_complete=True,
    )
    assert config.allow_bounded_expand is False


def test_longer_run_without_explicit_flag_is_blocked():
    with pytest.raises(ValueError, match="limited to two hours"):
        BuildConfig(
            symbol="BTCUSDT",
            start=T0,
            end=T0 + timedelta(hours=3),
            horizon_minutes=60,
            require_complete=True,
        )


@pytest.mark.parametrize("limit", [None, 0, -1, float("nan"), float("inf"), 169])
def test_expand_flag_requires_valid_max_limit(limit):
    with pytest.raises(ValueError, match="max-window-hours"):
        BuildConfig(
            symbol="BTCUSDT",
            start=T0,
            end=T0 + timedelta(hours=3),
            horizon_minutes=60,
            require_complete=True,
            allow_bounded_expand=True,
            max_window_hours=limit,
        )


def test_requested_window_above_explicit_limit_is_blocked():
    with pytest.raises(ValueError, match="exceeds max-window-hours"):
        BuildConfig(
            symbol="BTCUSDT",
            start=T0,
            end=T0 + timedelta(hours=7),
            horizon_minutes=60,
            require_complete=True,
            allow_bounded_expand=True,
            max_window_hours=6,
        )


def test_valid_longer_window_is_accepted():
    config = BuildConfig(
        symbol="BTCUSDT",
        start=T0,
        end=T0 + timedelta(hours=6),
        horizon_minutes=60,
        require_complete=True,
        allow_bounded_expand=True,
        max_window_hours=6,
    )
    assert config.max_window_hours == 6


def test_frozen_contract_output_is_unchanged():
    encoded = (
        json.dumps(contract_dict(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
    ).encode()
    assert hashlib.sha256(encoded).hexdigest() == (
        "2aad3b9bc2ba949f72f16a11b04ad53d7428d94bdec13ca55497aa30b450f6c3"
    )
    contract_path = Path(
        "results/liquidity_destination_bias_phase_1/contract_frozen_v1.yaml"
    )
    assert hashlib.sha256(contract_path.read_bytes()).hexdigest() == (
        "66ab82f4ac5e1b416594d17fa50116ed8a6daadae01e9fe29a29586072a91a51"
    )


def test_episode_crosses_chunk_boundary_without_duplicate_or_lost_outcome():
    result, loader = build_chunked(upper_touch_second=180)
    first = result.episodes[0].values
    assert len(loader.calls) == 3
    assert first["t0_utc"] == "2026-08-25T00:05:00.000Z"
    assert first["outcome"] == Outcome.UPPER_FIRST.value
    assert first["first_touch_utc"] == "2026-08-25T00:08:00.000Z"
    assert len({row.values["episode_id"] for row in result.episodes}) == len(
        result.episodes
    )
    assert result.excluded[1]["t0_utc"] == "2026-08-25T00:07:00.000Z"


def test_neither_waits_for_closed_horizon_across_boundary():
    result, _ = build_chunked(upper_touch_second=None)
    first = result.episodes[0].values
    assert first["outcome"] == Outcome.NEITHER.value
    assert first["horizon_end_utc"] == "2026-08-25T00:10:00.000Z"
    assert result.episodes[1].values["t0_utc"] == "2026-08-25T00:10:00.000Z"


def test_outputs_and_ids_do_not_depend_on_internal_chunk_size():
    two, _ = build_chunked(chunk_seconds=120)
    three, _ = build_chunked(chunk_seconds=180)
    assert [row.to_dict() for row in two.episodes] == [
        row.to_dict() for row in three.episodes
    ]
    assert two.excluded == three.excluded
    assert two.summary == three.summary


def test_prefix_parity_for_completed_semantic_rows():
    full, _ = build_chunked(minutes=6, chunk_seconds=120)
    prefix, _ = build_chunked(minutes=4, chunk_seconds=120)
    cutoff = "2026-08-25T00:09:00.000Z"
    common = [row.to_dict() for row in full.episodes if row.values["t0_utc"] < cutoff]
    prefix_rows = [row.to_dict() for row in prefix.episodes]
    for rows in (common, prefix_rows):
        for row in rows:
            row.pop("source_coverage_end_utc")
    assert common == prefix_rows


def test_chunked_build_is_idempotent():
    first, _ = build_chunked(chunk_seconds=120)
    second, _ = build_chunked(chunk_seconds=120)
    assert first == second


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (T0, T0),
        (T0, T0 - timedelta(seconds=1)),
    ],
)
def test_invalid_time_windows_fail_closed(start, end):
    with pytest.raises(ValueError, match="end must be after start"):
        BuildConfig(
            symbol="BTCUSDT",
            start=start,
            end=end,
            horizon_minutes=60,
            require_complete=True,
        )


def test_chunked_targets_and_t0_fields_never_use_future_availability():
    result, _ = build_chunked()
    for episode in result.episodes:
        row = episode.values
        assert row["knowledge_cutoff_utc"] == row["t0_utc"]
        assert row["upper_target_available_at"] <= row["t0_utc"]
        assert row["lower_target_available_at"] <= row["t0_utc"]
