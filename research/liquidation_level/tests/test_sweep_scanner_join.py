"""Tests for Phase A causal sweep↔scanner join."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.liquidation_control_validation import (
    EXPECTED_FULL,
    EXPECTED_IS,
    EXPECTED_OOS,
    WINNER_CONFIG_ID,
)
from research.liquidation_level.liquidation_levels import normalize_ohlcv_dataframe
from research.liquidation_level.sweep_scanner_join import (
    SOURCE_CONFIG_ID,
    SweepTriggerEvent,
    _forming_bucket,
    _last_closed_htf_index,
    decision_time_from_signal_open,
    ensure_utc,
    freeze_snapshot,
    join_all_events,
    join_sweep_event,
    precompute_scanner_feature_store,
    reproduce_winner_events,
    select_timeline_event_indices,
    snapshots_deterministic_hash,
    validation_events_to_triggers,
)
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta


REPO = Path(__file__).resolve().parents[2]
FEATHER = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather"
)
SCANNER_ROOT = Path(__file__).resolve().parents[2] / "regime_scanner"


def _ts(i: int, start: datetime | None = None) -> datetime:
    base = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return base + timedelta(minutes=5 * i)


def _synthetic_grid(n: int = 400) -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(n):
        c = px + (0.02 if i % 7 != 0 else -0.05)
        rows.append(
            {
                "timestamp": _ts(i),
                "open": px,
                "high": max(px, c) + 0.4,
                "low": min(px, c) - 0.4,
                "close": c,
                "volume": 200.0 + (i % 13),
            }
        )
        px = c
    return pd.DataFrame(rows)


def test_utc_normalization() -> None:
    naive = pd.Timestamp("2026-01-01 10:00:00")
    aware = ensure_utc(naive)
    assert str(aware.tz) == "UTC"
    assert ensure_utc("2026-01-01T10:00:00Z").hour == 10


def test_decision_time_from_signal_open() -> None:
    open_ts = ensure_utc("2026-01-01T10:05:00Z")
    assert decision_time_from_signal_open(open_ts) == ensure_utc("2026-01-01T10:10:00Z")


def test_15m_at_1010_close_excludes_forming_bucket() -> None:
    """Sweep close 10:10 → open 10:05; must not use 10:00–10:15."""
    data = _synthetic_grid(100)
    # Build contiguous day starting 09:00
    start = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    data = data.copy()
    data["timestamp"] = [_ts(i, start) for i in range(len(data))]
    decision = ensure_utc("2026-01-01T10:10:00Z")
    agg = aggregate_candles(data, "15m", decision)
    starts = set(pd.to_datetime(agg["timestamp"], utc=True))
    assert ensure_utc("2026-01-01T10:00:00Z") not in starts
    # prior complete bucket 09:45–10:00 closes at 10:00
    assert ensure_utc("2026-01-01T09:45:00Z") in starts
    form_start, form_end, _ = _forming_bucket(decision, "15m")
    assert form_start == ensure_utc("2026-01-01T10:00:00Z")
    assert form_end == ensure_utc("2026-01-01T10:15:00Z")
    assert form_end > decision


def test_15m_at_1025_uses_last_closed() -> None:
    """Sweep close 10:25 → open 10:20; last closed 15m is 10:00–10:15."""
    start = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    data = _synthetic_grid(80)
    data["timestamp"] = [_ts(i, start) for i in range(len(data))]
    decision = ensure_utc("2026-01-01T10:25:00Z")
    agg = aggregate_candles(data, "15m", decision)
    starts = list(pd.to_datetime(agg["timestamp"], utc=True))
    assert starts[-1] == ensure_utc("2026-01-01T10:00:00Z")
    avail = (pd.to_datetime(agg["timestamp"], utc=True) + timeframe_timedelta("15m")).to_numpy()
    idx = _last_closed_htf_index(avail, decision)
    assert idx is not None
    assert ensure_utc(agg.iloc[idx]["timestamp"]) == ensure_utc("2026-01-01T10:00:00Z")


def test_30m_before_close_uses_previous() -> None:
    start = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    data = _synthetic_grid(100)
    data["timestamp"] = [_ts(i, start) for i in range(len(data))]
    # Sweep close 10:20 — 10:00–10:30 still open
    decision = ensure_utc("2026-01-01T10:20:00Z")
    agg = aggregate_candles(data, "30m", decision)
    starts = list(pd.to_datetime(agg["timestamp"], utc=True))
    assert ensure_utc("2026-01-01T10:00:00Z") not in starts
    assert starts[-1] == ensure_utc("2026-01-01T09:30:00Z")


def test_no_ffill_forming_htf() -> None:
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    data = _synthetic_grid(6)  # 10:00 .. 10:25
    data["timestamp"] = [_ts(i, start) for i in range(len(data))]
    decision = ensure_utc("2026-01-01T10:20:00Z")
    agg15 = aggregate_candles(data, "15m", decision)
    # Only 10:00–10:15 if complete (opens 10:00,10:05,10:10)
    assert len(agg15) == 1
    assert ensure_utc(agg15.iloc[0]["timestamp"]) == ensure_utc("2026-01-01T10:00:00Z")


def test_no_bfill_from_future() -> None:
    start = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    data = _synthetic_grid(12)
    data["timestamp"] = [_ts(i, start) for i in range(len(data))]
    early = ensure_utc("2026-01-01T10:05:00Z")
    late = ensure_utc("2026-01-01T10:30:00Z")
    early_agg = aggregate_candles(data, "15m", early)
    late_agg = aggregate_candles(data, "15m", late)
    early_starts = set(pd.to_datetime(early_agg["timestamp"], utc=True))
    late_starts = set(pd.to_datetime(late_agg["timestamp"], utc=True))
    assert ensure_utc("2026-01-01T10:15:00Z") in late_starts
    assert ensure_utc("2026-01-01T10:15:00Z") not in early_starts


def test_same_candle_5m_join_and_no_future() -> None:
    data = _synthetic_grid(450)
    store = precompute_scanner_feature_store(data)
    # artificial event at index 300
    ts = ensure_utc(data.iloc[300]["timestamp"])
    ev = SweepTriggerEvent(
        event_id="T1",
        source_config_id=SOURCE_CONFIG_ID,
        signal_index=300,
        signal_timestamp=ts,
        side="upper",
        direction_context="short_context",
        primary_leverage=50,
        swept_leverages=(50,),
        swept_level_ids=(1,),
        swept_level_count=1,
        swept_total_strength=4,
        cluster_center_price=100.0,
        cluster_min_price=100.0,
        cluster_max_price=100.0,
        reclaim_status="immediate_reclaim",
        close_relative_to_level_pct=0.1,
        sweep_candle_open=float(data.iloc[300]["open"]),
        sweep_candle_high=float(data.iloc[300]["high"]),
        sweep_candle_low=float(data.iloc[300]["low"]),
        sweep_candle_close=float(data.iloc[300]["close"]),
        sweep_candle_volume=float(data.iloc[300]["volume"]),
        sample="in_sample",
    )
    snap = join_sweep_event(ev, store)
    assert snap.tf5_exact_match
    assert snap.tf5_timestamp == ts
    assert snap.decision_time == ts + pd.Timedelta(minutes=5)
    # not a future 5m
    assert snap.tf5_timestamp <= ev.signal_timestamp
    if snap.tf15_bucket_end is not None:
        assert snap.tf15_bucket_end <= snap.decision_time
    if snap.tf30_bucket_end is not None:
        assert snap.tf30_bucket_end <= snap.decision_time


def test_freeze_invariant_after_later_mutation() -> None:
    data = _synthetic_grid(450)
    store = precompute_scanner_feature_store(data)
    ts = ensure_utc(data.iloc[320]["timestamp"])
    ev = SweepTriggerEvent(
        event_id="T2",
        source_config_id=SOURCE_CONFIG_ID,
        signal_index=320,
        signal_timestamp=ts,
        side="upper",
        direction_context="short_context",
        primary_leverage=50,
        swept_leverages=(50,),
        swept_level_ids=(1,),
        swept_level_count=1,
        swept_total_strength=4,
        cluster_center_price=100.0,
        cluster_min_price=99.0,
        cluster_max_price=101.0,
        reclaim_status="immediate_reclaim",
        close_relative_to_level_pct=0.2,
        sweep_candle_open=1.0,
        sweep_candle_high=1.1,
        sweep_candle_low=0.9,
        sweep_candle_close=0.95,
        sweep_candle_volume=10.0,
        sample="in_sample",
    )
    snap = freeze_snapshot(join_sweep_event(ev, store))
    before = snap.features_5m.get("structure_bias")
    # mutate live store (simulates later candles / pivot confirmations / trend updates)
    store.structure_5m[320]["structure_bias"] = "MUTATED"
    store.structure_5m[320]["last_bos"] = "fake_future_bos"
    store.ind_5m.loc[320, "ema_9"] = -999.0
    after = freeze_snapshot(snap)
    assert after.features_5m.get("structure_bias") == before
    assert after.features_5m.get("last_bos") != "fake_future_bos" or before is None
    assert after.features_5m.get("ema_9") != -999.0


def test_missing_features_marked() -> None:
    data = _synthetic_grid(450)
    store = precompute_scanner_feature_store(data)
    ts = ensure_utc(data.iloc[400]["timestamp"])
    ev = SweepTriggerEvent(
        event_id="T3",
        source_config_id=SOURCE_CONFIG_ID,
        signal_index=400,
        signal_timestamp=ts,
        side="upper",
        direction_context="short_context",
        primary_leverage=50,
        swept_leverages=(50,),
        swept_level_ids=(1,),
        swept_level_count=1,
        swept_total_strength=4,
        cluster_center_price=1.0,
        cluster_min_price=1.0,
        cluster_max_price=1.0,
        reclaim_status="immediate_reclaim",
        close_relative_to_level_pct=0.0,
        sweep_candle_open=1.0,
        sweep_candle_high=1.0,
        sweep_candle_low=1.0,
        sweep_candle_close=1.0,
        sweep_candle_volume=1.0,
        sample="in_sample",
    )
    snap = join_sweep_event(ev, store)
    assert snap.features_5m.get("price_action_state") is None
    assert snap.features_5m.get("momentum_state") is None
    assert snap.diagnostics.get("pa_available") is False
    assert snap.diagnostics.get("momentum_available") is False
    assert snap.availability_flags.get("has_pa_5m") is False


def test_warmup_flags() -> None:
    data = _synthetic_grid(450)
    store = precompute_scanner_feature_store(data)
    early_ts = ensure_utc(data.iloc[20]["timestamp"])
    late_ts = ensure_utc(data.iloc[400]["timestamp"])

    def _ev(i: int, ts: pd.Timestamp) -> SweepTriggerEvent:
        return SweepTriggerEvent(
            event_id=f"W{i}",
            source_config_id=SOURCE_CONFIG_ID,
            signal_index=i,
            signal_timestamp=ts,
            side="upper",
            direction_context="short_context",
            primary_leverage=50,
            swept_leverages=(50,),
            swept_level_ids=(1,),
            swept_level_count=1,
            swept_total_strength=4,
            cluster_center_price=1.0,
            cluster_min_price=1.0,
            cluster_max_price=1.0,
            reclaim_status="immediate_reclaim",
            close_relative_to_level_pct=0.0,
            sweep_candle_open=1.0,
            sweep_candle_high=1.0,
            sweep_candle_low=1.0,
            sweep_candle_close=1.0,
            sweep_candle_volume=1.0,
            sample="in_sample",
        )

    early = join_sweep_event(_ev(20, early_ts), store)
    late = join_sweep_event(_ev(400, late_ts), store)
    assert early.diagnostics["warmup_complete_5m"] is False
    assert late.diagnostics["warmup_complete_5m"] is True


def test_deterministic_selection_and_hash() -> None:
    events = [
        SweepTriggerEvent(
            event_id=f"E{i}",
            source_config_id=SOURCE_CONFIG_ID,
            signal_index=i,
            signal_timestamp=ensure_utc(_ts(i)),
            side="upper",
            direction_context="short_context",
            primary_leverage=50,
            swept_leverages=(50,),
            swept_level_ids=(1,),
            swept_level_count=1,
            swept_total_strength=1,
            cluster_center_price=1.0,
            cluster_min_price=1.0,
            cluster_max_price=1.0,
            reclaim_status="immediate_reclaim",
            close_relative_to_level_pct=0.0,
            sweep_candle_open=1.0,
            sweep_candle_high=1.0,
            sweep_candle_low=1.0,
            sweep_candle_close=1.0,
            sweep_candle_volume=1.0,
            sample="in_sample" if i < 60 else "out_of_sample",
        )
        for i in range(80)
    ]
    a = select_timeline_event_indices(events, seed=42)
    b = select_timeline_event_indices(events, seed=42)
    assert a == b
    # hash stability with empty feature snapshots via join on short store
    data = _synthetic_grid(450)
    store = precompute_scanner_feature_store(data)
    subset = [
        SweepTriggerEvent(
            event_id=f"H{i}",
            source_config_id=SOURCE_CONFIG_ID,
            signal_index=200 + i,
            signal_timestamp=ensure_utc(data.iloc[200 + i]["timestamp"]),
            side="upper",
            direction_context="short_context",
            primary_leverage=50,
            swept_leverages=(50,),
            swept_level_ids=(1,),
            swept_level_count=1,
            swept_total_strength=1,
            cluster_center_price=1.0,
            cluster_min_price=1.0,
            cluster_max_price=1.0,
            reclaim_status="immediate_reclaim",
            close_relative_to_level_pct=0.0,
            sweep_candle_open=1.0,
            sweep_candle_high=1.0,
            sweep_candle_low=1.0,
            sweep_candle_close=1.0,
            sweep_candle_volume=1.0,
            sample="in_sample",
        )
        for i in range(5)
    ]
    s1 = join_all_events(subset, store)
    s2 = join_all_events(subset, store)
    h1 = snapshots_deterministic_hash(s1)
    h2 = snapshots_deterministic_hash(s2)
    assert h1 == h2
    assert len(h1) == 64


def test_identical_repeat_join() -> None:
    data = _synthetic_grid(450)
    store1 = precompute_scanner_feature_store(data)
    store2 = precompute_scanner_feature_store(data)
    ev = SweepTriggerEvent(
        event_id="R1",
        source_config_id=SOURCE_CONFIG_ID,
        signal_index=250,
        signal_timestamp=ensure_utc(data.iloc[250]["timestamp"]),
        side="upper",
        direction_context="short_context",
        primary_leverage=50,
        swept_leverages=(50,),
        swept_level_ids=(1,),
        swept_level_count=1,
        swept_total_strength=1,
        cluster_center_price=1.0,
        cluster_min_price=1.0,
        cluster_max_price=1.0,
        reclaim_status="immediate_reclaim",
        close_relative_to_level_pct=0.0,
        sweep_candle_open=1.0,
        sweep_candle_high=1.0,
        sweep_candle_low=1.0,
        sweep_candle_close=1.0,
        sweep_candle_volume=1.0,
        sample="in_sample",
    )
    assert join_sweep_event(ev, store1).to_dict() == join_sweep_event(ev, store2).to_dict()


def test_no_scanner_files_modified_by_phase_a_modules() -> None:
    """Fingerprint a few protected scanner modules; Phase A must not alter them."""
    protected = [
        SCANNER_ROOT / "timeframes.py",
        SCANNER_ROOT / "indicators.py",
        SCANNER_ROOT / "point_audit.py",
        SCANNER_ROOT / "pipeline_audit.py",
        SCANNER_ROOT / "price_action.py",
        SCANNER_ROOT / "momentum.py",
    ]
    digests = {}
    for path in protected:
        assert path.exists(), path
        digests[path.name] = hashlib.md5(path.read_bytes()).hexdigest()
    # Re-import join module (no writes)
    import research.liquidation_level.sweep_scanner_join as m  # noqa: F401
    import research.liquidation_level.sweep_scanner_timeline_audit as m2  # noqa: F401

    for path in protected:
        assert hashlib.md5(path.read_bytes()).hexdigest() == digests[path.name]


@pytest.mark.skipif(not FEATHER.exists(), reason="APT feather not available")
def test_winner_event_counts_exact() -> None:
    from research.liquidation_level.liquidation_audit import load_feather

    raw = load_feather(FEATHER)
    events, replay, meta = reproduce_winner_events(raw, expect_counts=True)
    assert meta["reproduced"]["full"] == EXPECTED_FULL
    assert meta["reproduced"]["in_sample"] == EXPECTED_IS
    assert meta["reproduced"]["out_of_sample"] == EXPECTED_OOS
    triggers = validation_events_to_triggers(events, replay, normalize_ohlcv_dataframe(raw))
    assert len(triggers) == EXPECTED_FULL
    assert all(t.source_config_id == WINNER_CONFIG_ID for t in triggers)
    assert all(t.reclaim_status == "immediate_reclaim" for t in triggers)
    assert all(t.primary_leverage == 50 for t in triggers)
