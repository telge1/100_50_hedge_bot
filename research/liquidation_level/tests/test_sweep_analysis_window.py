"""Tests for Phase B sweep analysis windows."""

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
)
from research.liquidation_level.sweep_analysis_window import (
    STATE_ANALYSIS_ACTIVE,
    STATE_INCOMPLETE_END_OF_DATA,
    STATE_SWEEP_DETECTED,
    STATE_WINDOW_COMPLETED,
    assert_no_entry_fields,
    build_analysis_windows_for_events,
    build_single_window,
    bundle_deterministic_hash,
    compute_overlap_diagnostics,
    level_behavior_for_bar,
)
from research.liquidation_level.sweep_scanner_join import (
    SOURCE_CONFIG_ID,
    SweepTriggerEvent,
    decision_time_from_signal_open,
    ensure_utc,
    join_sweep_event,
    precompute_scanner_feature_store,
    reproduce_winner_events,
    validation_events_to_triggers,
)

FEATHER = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data/bybit/futures/APT_USDT_USDT-5m-futures.feather"
)
SCANNER_ROOT = Path(__file__).resolve().parents[2] / "regime_scanner"


def _ts(i: int, start: datetime | None = None) -> datetime:
    base = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return base + timedelta(minutes=5 * i)


def _grid(n: int = 500, start: datetime | None = None) -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(n):
        c = px + (0.05 if i % 5 else -0.08)
        hi = max(px, c) + 0.5
        lo = min(px, c) - 0.5
        rows.append(
            {
                "timestamp": _ts(i, start),
                "open": px,
                "high": hi,
                "low": lo,
                "close": c,
                "volume": 150.0 + i % 11,
            }
        )
        px = c
    return pd.DataFrame(rows)


def _event(data: pd.DataFrame, i: int, level: float = 100.5) -> SweepTriggerEvent:
    row = data.iloc[i]
    return SweepTriggerEvent(
        event_id=f"E{i}",
        source_config_id=SOURCE_CONFIG_ID,
        signal_index=i,
        signal_timestamp=ensure_utc(row["timestamp"]),
        side="upper",
        direction_context="short_context",
        primary_leverage=50,
        swept_leverages=(50,),
        swept_level_ids=(1,),
        swept_level_count=1,
        swept_total_strength=4,
        cluster_center_price=level,
        cluster_min_price=level,
        cluster_max_price=level,
        reclaim_status="immediate_reclaim",
        close_relative_to_level_pct=0.1,
        sweep_candle_open=float(row["open"]),
        sweep_candle_high=float(row["high"]),
        sweep_candle_low=float(row["low"]),
        sweep_candle_close=float(row["close"]),
        sweep_candle_volume=float(row["volume"]),
        sample="in_sample",
    )


def test_level_behavior_reclaim_accept_reject() -> None:
    lvl = 100.0
    b = level_behavior_for_bar(open_=99.5, high=100.5, low=99.0, close=99.2, level=lvl)
    assert b["reclaimed_below_level"] is True
    assert b["rejected_from_level_candidate"] is True
    assert b["accepted_above_level_candidate"] is False
    assert b["close_below_level"] is True
    assert b["touched_level"] is True
    a = level_behavior_for_bar(open_=100.2, high=101.0, low=100.1, close=100.8, level=lvl)
    assert a["accepted_above_level_candidate"] is True
    assert a["close_above_level"] is True
    assert a["reclaimed_below_level"] is False


def test_window_offsets_and_no_sweep_as_follow() -> None:
    data = _grid(480)
    store = precompute_scanner_feature_store(data)
    ev = _event(data, 300, level=float(data.iloc[300]["high"]))
    snap = join_sweep_event(ev, store)
    win, bars, metrics, _ = build_single_window(ev, store=store, frozen_snap=snap, window_size=6)
    assert win.start_index == 301
    assert [b.window_offset for b in bars] == [1, 2, 3, 4, 5, 6]
    assert all(b.candle_index != 300 for b in bars)
    assert bars[0].candle_index == 301
    assert bars[-1].candle_index == 306
    assert win.end_index == 306
    assert win.complete is True
    assert win.status == STATE_WINDOW_COMPLETED
    assert bars[0].state_before == STATE_SWEEP_DETECTED
    assert bars[0].state_after == STATE_ANALYSIS_ACTIVE
    assert bars[-1].state_after == STATE_WINDOW_COMPLETED
    assert_no_entry_fields(win.to_dict())
    assert_no_entry_fields(bars[0].to_dict())
    assert metrics.complete is True


def test_end_of_data_incomplete() -> None:
    data = _grid(310)
    store = precompute_scanner_feature_store(data)
    ev = _event(data, 305, level=100.0)
    snap = join_sweep_event(ev, store)
    win, bars, _, _ = build_single_window(ev, store=store, frozen_snap=snap, window_size=12)
    assert win.complete is False
    assert win.status == STATE_INCOMPLETE_END_OF_DATA
    assert win.available_candle_count == 4
    assert win.expected_candle_count == 12
    assert len(bars) == 4


def test_frozen_context_immutable() -> None:
    data = _grid(450)
    store = precompute_scanner_feature_store(data)
    ev = _event(data, 320, level=100.0)
    snap = join_sweep_event(ev, store)
    win, bars, _, _ = build_single_window(ev, store=store, frozen_snap=snap, window_size=3)
    frozen_ema = win.frozen_context["5m"]["ema_9"]
    store.structure_5m[320]["structure_bias"] = "MUTATED"
    store.ind_5m.loc[320, "ema_9"] = -12345.0
    for b in bars:
        assert b.frozen_5m["ema_9"] == frozen_ema
        assert b.frozen_5m.get("structure_bias") != "MUTATED"


def test_dynamic_context_updates() -> None:
    data = _grid(450)
    store = precompute_scanner_feature_store(data)
    ev = _event(data, 330, level=100.0)
    snap = join_sweep_event(ev, store)
    _, bars, _, _ = build_single_window(ev, store=store, frozen_snap=snap, window_size=3)
    assert bars[0].current_5m["ema_9"] is not None
    assert "adx" in bars[0].deltas


def test_htf_no_lookahead_inside_window() -> None:
    start = datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)
    data = _grid(80, start=start)
    store = precompute_scanner_feature_store(data)
    target = ensure_utc("2026-01-01T10:05:00Z")
    idx = int(np.where(pd.to_datetime(data["timestamp"], utc=True) == target)[0][0])
    ev = _event(data, idx, level=100.0)
    snap = join_sweep_event(ev, store)
    assert snap.tf15_bucket_end is None or snap.tf15_bucket_end <= snap.decision_time
    _, bars, _, _ = build_single_window(ev, store=store, frozen_snap=snap, window_size=6)
    for b in bars:
        if b.htf_15m.get("bucket_end") is not None:
            assert ensure_utc(b.htf_15m["bucket_end"]) <= b.available_at
        if b.htf_30m.get("bucket_end") is not None:
            assert ensure_utc(b.htf_30m["bucket_end"]) <= b.available_at
    assert bars[0].available_at == decision_time_from_signal_open(bars[0].timestamp)


def test_path_metrics_and_crosses() -> None:
    data = _grid(450)
    store = precompute_scanner_feature_store(data)
    lvl = float(data.iloc[340]["close"])
    ev = _event(data, 340, level=lvl)
    snap = join_sweep_event(ev, store)
    _, _, metrics, _ = build_single_window(ev, store=store, frozen_snap=snap, window_size=6)
    m = metrics.metrics
    assert m["candles_closed_above_level"] + m["candles_closed_below_level"] <= 6
    assert m["window_range_pct"] is not None
    assert m["close_return_end_pct"] is not None
    assert metrics.complete is True


def test_overlaps_separate_and_counts() -> None:
    data = _grid(450)
    store = precompute_scanner_feature_store(data)
    events = [_event(data, 350, level=100.0), _event(data, 352, level=100.0)]
    snaps = [join_sweep_event(e, store) for e in events]
    bundle = build_analysis_windows_for_events(
        events, store=store, frozen_snapshots=snaps, window_sizes=(3,)
    )
    assert len(bundle.windows) == 2
    _, summary = compute_overlap_diagnostics(bundle.windows, n_candles=len(data))
    assert summary["maximum_concurrent_windows"] >= 1
    assert summary["events_with_overlaps"] >= 2


def test_deterministic_hash_repeat() -> None:
    data = _grid(450)
    store = precompute_scanner_feature_store(data)
    events = [_event(data, 360, level=100.0)]
    snaps = [join_sweep_event(e, store) for e in events]
    b1 = build_analysis_windows_for_events(
        events, store=store, frozen_snapshots=snaps, window_sizes=(3, 6)
    )
    b2 = build_analysis_windows_for_events(
        events, store=store, frozen_snapshots=snaps, window_sizes=(3, 6)
    )
    assert bundle_deterministic_hash(b1) == bundle_deterministic_hash(b2)


def test_feature_deltas_regime_structure_flags() -> None:
    data = _grid(450)
    store = precompute_scanner_feature_store(data)
    ev = _event(data, 370, level=100.0)
    snap = join_sweep_event(ev, store)
    _, bars, _, _ = build_single_window(ev, store=store, frozen_snap=snap, window_size=3)
    assert "regime_changed" in bars[0].deltas
    assert "structure_bias_changed" in bars[0].deltas
    assert bars[0].current_5m.get("price_action_state") is None
    assert bars[0].current_5m.get("momentum_state") is None


def test_sizes_3_6_12() -> None:
    data = _grid(450)
    store = precompute_scanner_feature_store(data)
    events = [_event(data, 380, level=100.0)]
    snaps = [join_sweep_event(e, store) for e in events]
    bundle = build_analysis_windows_for_events(
        events, store=store, frozen_snapshots=snaps, window_sizes=(3, 6, 12)
    )
    assert {w.window_size for w in bundle.windows} == {3, 6, 12}
    for ws in (3, 6, 12):
        bars = [b for b in bundle.bars if b.window_size == ws]
        assert [b.window_offset for b in bars] == list(range(1, ws + 1))


def test_no_entry_pnl_fields_in_exports() -> None:
    data = _grid(420)
    store = precompute_scanner_feature_store(data)
    events = [_event(data, 300, level=100.0)]
    snaps = [join_sweep_event(e, store) for e in events]
    bundle = build_analysis_windows_for_events(
        events, store=store, frozen_snapshots=snaps, window_sizes=(3,)
    )
    for w in bundle.windows:
        assert_no_entry_fields(w.to_dict())
    for b in bundle.bars:
        assert_no_entry_fields(b.to_dict())
    for m in bundle.path_metrics:
        assert_no_entry_fields(m.to_dict())


def test_no_scanner_files_modified() -> None:
    protected = [
        SCANNER_ROOT / "timeframes.py",
        SCANNER_ROOT / "indicators.py",
        SCANNER_ROOT / "point_audit.py",
        SCANNER_ROOT / "momentum.py",
        SCANNER_ROOT / "price_action.py",
    ]
    digests = {p.name: hashlib.md5(p.read_bytes()).hexdigest() for p in protected}
    import research.liquidation_level.sweep_analysis_window as _m  # noqa: F401
    import research.liquidation_level.sweep_analysis_window_audit as _m2  # noqa: F401

    for p in protected:
        assert hashlib.md5(p.read_bytes()).hexdigest() == digests[p.name]


@pytest.mark.skipif(not FEATHER.exists(), reason="APT feather not available")
def test_winner_event_counts_phase_b_input() -> None:
    from research.liquidation_level.liquidation_audit import load_feather

    raw = load_feather(FEATHER)
    events, replay, meta = reproduce_winner_events(raw, expect_counts=True)
    assert meta["reproduced"]["full"] == EXPECTED_FULL
    assert meta["reproduced"]["in_sample"] == EXPECTED_IS
    assert meta["reproduced"]["out_of_sample"] == EXPECTED_OOS
    triggers = validation_events_to_triggers(events, replay, raw)
    assert len(triggers) == EXPECTED_FULL
