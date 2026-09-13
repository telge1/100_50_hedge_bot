"""Focused synthetic tests for the frozen causal episode builder."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.liquidity_destination_bias.builder import (
    BuildConfig,
    build_episodes,
    deterministic_episode_id,
    write_artifacts,
)
from research.liquidity_destination_bias.contract import (
    CONTRACT_VERSION,
    ExclusionReason,
    Outcome,
)
from research.liquidity_destination_bias.eligibility import evaluate_candidate
from research.liquidity_destination_bias.models import FrozenTarget, PriceBucket
from research.liquidity_destination_bias.path_label import label_first_touch
from research.liquidity_destination_bias.sources import TradeSecondSeries
from research.liquidity_destination_bias.targets import select_frozen_targets

UTC = timezone.utc
T0 = datetime(2026, 8, 30, 15, 0, tzinfo=UTC)


def target(side: str, lo: float, hi: float, *, ident: str | None = None) -> FrozenTarget:
    return FrozenTarget(
        target_id=ident or side.lower(),
        side=side,
        lower_price=lo,
        upper_price=hi,
        touch_price=lo if side == "ASK" else hi,
        available_at=T0 - timedelta(minutes=2),
        source="LLD_POOL",
        contract_version="liquidity_pool_signal/canonical_v1",
    )


UPPER = target("ASK", 105, 106)
LOWER = target("BID", 94, 95)


def bucket(offset: int, *, low: float = 99, high: float = 101, close: float = 100):
    return PriceBucket(T0 + timedelta(seconds=offset), low, high, close, 1)


def label(path):
    return label_first_touch(
        path,
        t0=T0,
        horizon_end=T0 + timedelta(minutes=1),
        upper=UPPER,
        lower=LOWER,
    )


def test_upper_first():
    assert label([bucket(2, high=105), bucket(4, low=94)])["outcome"] == Outcome.UPPER_FIRST.value


def test_lower_first():
    assert label([bucket(2, low=95), bucket(4, high=106)])["outcome"] == Outcome.LOWER_FIRST.value


def test_neither_within_horizon():
    assert label([bucket(2), bucket(59)])["outcome"] == Outcome.NEITHER.value


def test_simultaneous_touch():
    got = label([bucket(2, low=94, high=106)])
    assert got["outcome"] == Outcome.AMBIGUOUS.value
    assert got["upper_touch"] == got["lower_touch"]


def test_target_already_touched_at_t0():
    reason = evaluate_candidate(
        t0=T0,
        price_t0=105,
        upper=UPPER,
        lower=LOWER,
        warmup_complete=True,
        source_complete=True,
        horizon_closed=True,
        require_complete=True,
    )
    assert reason is ExclusionReason.TARGET_ALREADY_TOUCHED_AT_T0


@pytest.mark.parametrize(
    ("upper", "lower", "expected"),
    [
        (None, LOWER, ExclusionReason.MISSING_UPPER_TARGET),
        (UPPER, None, ExclusionReason.MISSING_LOWER_TARGET),
    ],
)
def test_missing_target(upper, lower, expected):
    assert evaluate_candidate(
        t0=T0,
        price_t0=100,
        upper=upper,
        lower=lower,
        warmup_complete=True,
        source_complete=True,
        horizon_closed=True,
        require_complete=True,
    ) is expected


def test_overlapping_targets():
    assert evaluate_candidate(
        t0=T0,
        price_t0=100,
        upper=target("ASK", 100, 106),
        lower=target("BID", 94, 101),
        warmup_complete=True,
        source_complete=True,
        horizon_closed=True,
        require_complete=True,
    ) is ExclusionReason.TARGETS_OVERLAP


def test_invalid_target_order_rejected_by_immutable_model():
    with pytest.raises(ValueError, match="lower_price"):
        target("ASK", 106, 105)


def test_frozen_target_prices_do_not_follow_source_mutation():
    row = {"pool_id": "u", "side": "ASK", "lower": 105.0, "upper": 106.0,
           "available_at": "2026-08-30T14:58:00Z", "active_as_of": True}
    snap = {"active_canonical_pools": [row, {
        "pool_id": "l", "side": "BID", "lower": 94.0, "upper": 95.0,
        "available_at": "2026-08-30T14:58:00Z", "active_as_of": True,
    }]}
    upper, _ = select_frozen_targets(snap, price_t0=100, t0=T0)
    row["lower"] = 999
    assert upper is not None and upper.touch_price == 105.0


def test_future_pool_cannot_change_t0_target_choice():
    snap = {"active_canonical_pools": [
        {"pool_id": "known-u", "side": "ASK", "lower": 105, "upper": 106,
         "available_at": "2026-08-30T14:58:00Z", "active_as_of": True},
        {"pool_id": "future-u", "side": "ASK", "lower": 101, "upper": 102,
         "available_at": "2026-08-30T15:01:00Z", "active_as_of": True},
        {"pool_id": "known-l", "side": "BID", "lower": 94, "upper": 95,
         "available_at": "2026-08-30T14:58:00Z", "active_as_of": True},
    ]}
    upper, lower = select_frozen_targets(snap, price_t0=100, t0=T0)
    assert upper is not None and upper.target_id == "known-u"
    assert lower is not None and lower.target_id == "known-l"


def test_future_price_path_only_changes_label_not_targets():
    a = label([bucket(2, high=105)])
    b = label([bucket(2, low=95)])
    assert a["outcome"] != b["outcome"]
    assert (UPPER.touch_price, LOWER.touch_price) == (105, 95)


def test_incomplete_horizon_is_fail_closed():
    assert evaluate_candidate(
        t0=T0,
        price_t0=100,
        upper=UPPER,
        lower=LOWER,
        warmup_complete=True,
        source_complete=False,
        horizon_closed=False,
        require_complete=True,
    ) is ExclusionReason.HORIZON_NOT_CLOSED


class Provider:
    def __init__(self, rows=None):
        self.rows = rows or [
            {"pool_id": "u", "side": "ASK", "lower": 105, "upper": 106,
             "available_at": "2026-08-30T14:58:00Z", "active_as_of": True},
            {"pool_id": "l", "side": "BID", "lower": 94, "upper": 95,
             "available_at": "2026-08-30T14:58:00Z", "active_as_of": True},
        ]
        self.calls: list[datetime] = []

    def snapshot(self, symbol, t0):
        self.calls.append(t0)
        return {
            "active_canonical_pools": self.rows,
            "canonical_provider_version": "liquidity_pool_signal/canonical_v1",
            "canonical_snapshot_sha256": "abc",
        }


def series(*, minutes=4, touch_at=None, missing_offset=None, source_complete=True):
    start = T0 - timedelta(minutes=5)
    end = T0 + timedelta(minutes=minutes)
    rows = {}
    cursor = start
    while cursor < end:
        offset = int((cursor - T0).total_seconds())
        if offset != missing_offset:
            high = 105 if touch_at == offset else 101
            rows[cursor] = PriceBucket(cursor, 99, high, 100, 1)
        cursor += timedelta(seconds=1)
    return TradeSecondSeries(
        rows, min(rows), max(rows) + timedelta(seconds=1), source_complete
    )


def config(*, end_minutes=2, horizon=2, require=True):
    return BuildConfig(
        symbol="BTCUSDT",
        start=T0,
        end=T0 + timedelta(minutes=end_minutes),
        horizon_minutes=horizon,
        require_complete=require,
    )


def test_no_overlapping_episodes():
    result = build_episodes(config(), trades=series(), provider=Provider())
    assert len(result.episodes) == 1
    assert result.summary["exclusion_counts"][
        ExclusionReason.DUPLICATE_OR_OVERLAPPING_EPISODE.value
    ] == 1


def test_deterministic_episode_id():
    args = dict(symbol="BTCUSDT", t0=T0, upper_id="u", lower_id="l", horizon_minutes=60)
    assert deterministic_episode_id(**args) == deterministic_episode_id(**args)
    assert len(deterministic_episode_id(**args)) == 64


def test_idempotent_core_outputs(tmp_path):
    result = build_episodes(config(), trades=series(), provider=Provider())
    a = write_artifacts(result, tmp_path / "a", generated_at=T0)
    b = write_artifacts(result, tmp_path / "b", generated_at=T0 + timedelta(days=1))
    assert a == b
    assert (tmp_path / "a" / "episodes.csv").read_bytes() == (
        tmp_path / "b" / "episodes.csv"
    ).read_bytes()


def test_require_complete_rejects_missing_trade_second():
    result = build_episodes(
        config(end_minutes=1, horizon=1),
        trades=series(minutes=2, missing_offset=10, source_complete=False),
        provider=Provider(),
    )
    assert not result.episodes
    assert ExclusionReason.SOURCE_COVERAGE_INCOMPLETE.value in result.summary["exclusion_counts"]


def test_prefix_target_parity_with_later_future_pool():
    prefix = Provider().snapshot("BTCUSDT", T0)
    extended = Provider(rows=Provider().rows + [{
        "pool_id": "future", "side": "ASK", "lower": 101, "upper": 102,
        "available_at": "2026-08-30T15:05:00Z", "active_as_of": True,
    }]).snapshot("BTCUSDT", T0)
    a = select_frozen_targets(prefix, price_t0=100, t0=T0)
    b = select_frozen_targets(extended, price_t0=100, t0=T0)
    assert a == b


def test_all_required_exclusion_codes_are_machine_readable():
    required = {
        "ORDERBOOK_DEPTH_UNPROVEN", "SEQUENCE_GAP",
        "PUBLIC_TRADES_MISSING_IF_MANDATORY", "SIMULTANEOUS_TOUCH",
        "DUPLICATE_OR_OVERLAPPING_EPISODE", "UNSUPPORTED_TARGET_SOURCE",
    }
    assert required <= {r.value for r in ExclusionReason}
    assert CONTRACT_VERSION == "liquidity_destination_episode_contract_v1"
