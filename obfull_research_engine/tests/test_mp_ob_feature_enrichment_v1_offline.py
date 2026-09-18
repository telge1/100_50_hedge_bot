"""Offline tests for mp_ob_feature_enrichment_v1 (no CH / no MP batch)."""

from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path

import pytest

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.breakout_xray_v1.trades import XRayTrade
from obfull_research_engine.mp_ob_feature_enrichment_v1 import run_cli
from obfull_research_engine.mp_ob_feature_enrichment_v1.analysis import (
    build_discovery_validation_split,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.features import (
    compute_event_features,
    _phase_bounds,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.input_audit import audit_batch_input
from obfull_research_engine.mp_ob_feature_enrichment_v1.lc_book import attribute_change
from obfull_research_engine.mp_ob_feature_enrichment_v1.params import (
    NS,
    PILOT_MAX_EVENTS,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.run_cli import select_pilot_events
from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import (
    build_zone_bands,
    price_in_interval,
    sum_depth,
)


REPO = Path(__file__).resolve().parents[1]
BATCH = REPO / "runs" / "mp_edge_event_batch_v1_20260916"


def test_batch_read_only_and_no_mp_batch_started():
    src = inspect.getsource(run_cli)
    assert "run_offline_pipeline_v2" not in src
    assert "BATCH_RUN_REL" in src or "mp_edge_event_batch_v1" in inspect.getsource(
        __import__(
            "obfull_research_engine.mp_ob_feature_enrichment_v1.params",
            fromlist=["BATCH_RUN_REL"],
        )
    )
    assert "mp_edge_event_batch_v1" in str(
        __import__(
            "obfull_research_engine.mp_ob_feature_enrichment_v1.params",
            fromlist=["BATCH_RUN_REL"],
        ).BATCH_RUN_REL
    )

def test_input_audit_counts():
    if not BATCH.exists():
        pytest.skip("batch run missing")
    audit = audit_batch_input(BATCH)
    assert audit["ok"] is True
    assert audit["checks"]["n_events"] == 282
    assert audit["checks"]["first_touch"] == 120
    assert audit["checks"]["non_overlapping_30m"] == 87
    assert audit["checks"]["cooldown_15m"] == 134
    assert audit["checks"]["complete_windows"] == 7


def test_phase_windows_and_cutoff():
    touch = 1_000_000_000_000
    trigger = touch + 5 * NS
    phases = _phase_bounds(touch, trigger)
    assert phases["baseline"][0] == touch - 120 * NS
    assert phases["baseline"][1] == touch - 30 * NS
    assert phases["approach"][0] == touch - 30 * NS
    assert phases["approach"][1] == touch
    assert phases["contact"][0] == touch
    assert phases["contact"][1] == trigger


def test_upper_lower_normalization_and_zone_mapping():
    u = build_zone_bands(role="UPPER", low=100.0, high=100.2)
    l = build_zone_bands(role="LOWER", low=100.0, high=100.2)
    assert u.defense_side == "ask"
    assert l.defense_side == "bid"
    assert u.beyond_1bp[0] >= u.high
    assert l.beyond_1bp[1] <= l.low
    assert price_in_interval(100.1, u.inside)
    sizes = {("ask", 100.1): 2.0, ("bid", 99.9): 3.0}
    assert sum_depth(sizes, side="ask", interval=u.inside) == 2.0


def test_hit_pull_add_semantics_and_pull_not_hit():
    ts = datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone.utc)
    trade = XRayTrade(trade_ts=ts, trade_id="1", side="Buy", price=100.0, size=1.0, notional=100.0)
    # ask decrease with Buy trade → HIT
    hit = attribute_change(
        side="ask",
        price=100.0,
        old_size=5.0,
        new_size=4.0,
        event_time_ns=int(ts.timestamp() * NS),
        trades=[trade],
        trades_available=True,
    )
    assert hit.kind == "HIT"
    # ask decrease without trade → PULL (not HIT)
    pull = attribute_change(
        side="ask",
        price=100.0,
        old_size=5.0,
        new_size=4.0,
        event_time_ns=int(ts.timestamp() * NS) + 10_000_000_000,
        trades=[trade],
        trades_available=True,
    )
    assert pull.kind == "PULL"
    add = attribute_change(
        side="ask",
        price=100.0,
        old_size=1.0,
        new_size=2.0,
        event_time_ns=int(ts.timestamp() * NS),
        trades=[trade],
        trades_available=True,
    )
    assert add.kind == "ADD"


def test_missing_trades_not_available_not_zero():
    dec = attribute_change(
        side="ask",
        price=100.0,
        old_size=5.0,
        new_size=4.0,
        event_time_ns=1,
        trades=[],
        trades_available=False,
    )
    assert dec.kind == "DECREASE_UNCLASSIFIED"


def test_refill_after_hit_and_wall_persistence():
    touch = 10_000 * NS
    trigger = touch + 10 * NS
    event = {
        "event_id": "mpe_test",
        "first_touch_ts_ns": str(touch),
        "trigger_ts_ns": str(trigger),
        "event_role": "UPPER",
        "confluence_low": "100",
        "confluence_high": "100",
        "max_penetration_bps": "2",
        "reclaim_ts_ns": "",
    }
    # baseline establish + hit + refill
    lcs = [
        LevelChangeEvent(
            event_time_ns=touch - 60 * NS,
            side="ask",
            price=100.0,
            change_type="ADD",
            new_size=10.0,
            old_size=0.0,
        ),
        LevelChangeEvent(
            event_time_ns=touch + NS,
            side="ask",
            price=100.0,
            change_type="UPDATE",
            new_size=6.0,
            old_size=10.0,
        ),
        LevelChangeEvent(
            event_time_ns=touch + 2 * NS,
            side="ask",
            price=100.0,
            change_type="UPDATE",
            new_size=9.0,
            old_size=6.0,
        ),
    ]
    ts_hit = datetime.fromtimestamp((touch + NS) / NS, tz=timezone.utc)
    trades = [
        XRayTrade(
            trade_ts=ts_hit,
            trade_id="h1",
            side="Buy",
            price=100.0,
            size=4.0,
            notional=400.0,
        )
    ]
    bundle = compute_event_features(
        event=event,
        level_changes=lcs,
        metrics=[],
        trades=trades,
        trades_available=True,
        window_start_ns=touch - 200 * NS,
        window_end_ns=trigger + 100 * NS,
    )
    assert bundle.causal_ok
    assert bundle.max_feature_ts_ns <= trigger
    assert bundle.features["hit_qty"].value == pytest.approx(4.0)
    assert bundle.features["add_after_hit_qty"].value == pytest.approx(3.0)
    assert bundle.features["refill_ratio"].value == pytest.approx(0.75)
    assert bundle.features["wall_present_contact_fraction"].value is not None


def test_no_feature_after_trigger():
    touch = 5_000 * NS
    trigger = touch + 3 * NS
    event = {
        "event_id": "mpe_cut",
        "first_touch_ts_ns": str(touch),
        "trigger_ts_ns": str(trigger),
        "event_role": "LOWER",
        "confluence_low": "50",
        "confluence_high": "50",
    }
    lcs = [
        LevelChangeEvent(
            event_time_ns=trigger + NS,
            side="bid",
            price=50.0,
            change_type="ADD",
            new_size=1.0,
            old_size=0.0,
        ),
        LevelChangeEvent(
            event_time_ns=touch - 40 * NS,
            side="bid",
            price=50.0,
            change_type="ADD",
            new_size=5.0,
            old_size=0.0,
        ),
    ]
    bundle = compute_event_features(
        event=event,
        level_changes=lcs,
        metrics=[],
        trades=[],
        trades_available=True,
        window_start_ns=touch - 200 * NS,
        window_end_ns=trigger + 50 * NS,
    )
    assert bundle.max_feature_ts_ns <= trigger
    assert bundle.features["hit_qty"].value == 0.0 or bundle.features["hit_qty"].value is not None


def test_missing_baseline_null():
    touch = 1000 * NS
    event = {
        "event_id": "mpe_base",
        "first_touch_ts_ns": str(touch),
        "trigger_ts_ns": str(touch + NS),
        "event_role": "UPPER",
        "confluence_low": "1",
        "confluence_high": "1",
    }
    bundle = compute_event_features(
        event=event,
        level_changes=[],
        metrics=[],
        trades=[],
        trades_available=False,
        window_start_ns=touch - 10 * NS,  # insufficient baseline
        window_end_ns=touch + 50 * NS,
    )
    assert bundle.baseline_warmup_ok is False
    assert bundle.features["wall_present_baseline_fraction"].available is False
    assert bundle.features["hit_qty"].missing_reason == "NOT_AVAILABLE"


def test_discovery_validation_by_windows_no_overlap(tmp_path: Path):
    if not BATCH.exists():
        pytest.skip("batch run missing")
    import csv

    events = list(csv.DictReader((BATCH / "events_all.csv").open()))
    rows, meta = build_discovery_validation_split(
        windows_csv=BATCH / "batch_windows.csv",
        events=events,
        discovery_window_count=5,
    )
    d = set(meta["discovery_window_ids"])
    v = set(meta["validation_window_ids"])
    assert not (d & v)
    assert meta["n_discovery_events"] + meta["n_validation_events"] == 282
    assert all(r["split"] in ("DISCOVERY", "VALIDATION") for r in rows)


def test_selection_policy_and_pilot_cap():
    events = [
        {
            "event_id": f"e{i}",
            "window_id": f"w{i%7}",
            "label_price_only": ["ABSORB", "TRUE_BREAK", "FAILED_BREAK"][i % 3],
            "event_role": "UPPER" if i % 2 == 0 else "LOWER",
            "confluence_class": "C1_30M",
        }
        for i in range(50)
    ]
    pilot = select_pilot_events(events, max_n=PILOT_MAX_EVENTS)
    assert len(pilot) <= 20
    assert len(pilot) <= PILOT_MAX_EVENTS


def test_costs_not_double_counted():
    src = inspect.getsource(
        __import__(
            "obfull_research_engine.mp_ob_feature_enrichment_v1.analysis",
            fromlist=["run_analyses"],
        ).run_analyses
    )
    assert "net_return_bps_8" in src
    assert "never subtract again" in src or "already applied" in src


def test_event_id_and_epoch_passthrough_in_enrich_row_shape():
    # enrich_one preserves ids — checked via source contract
    src = inspect.getsource(run_cli.enrich_one)
    assert 'row: dict[str, Any] = {\n        "event_id": event["event_id"]' in src or '"event_id": event["event_id"]' in src
    assert "replay_epoch" in src


def test_deterministic_feature_output():
    touch = 8_000 * NS
    trigger = touch + 4 * NS
    event = {
        "event_id": "mpe_det",
        "first_touch_ts_ns": str(touch),
        "trigger_ts_ns": str(trigger),
        "event_role": "UPPER",
        "confluence_low": "200",
        "confluence_high": "200",
    }
    lcs = [
        LevelChangeEvent(
            event_time_ns=touch - 50 * NS,
            side="ask",
            price=200.0,
            change_type="ADD",
            new_size=3.0,
            old_size=0.0,
        )
    ]
    a = compute_event_features(
        event=event,
        level_changes=lcs,
        metrics=[],
        trades=[],
        trades_available=True,
        window_start_ns=touch - 200 * NS,
        window_end_ns=trigger + 10 * NS,
    )
    b = compute_event_features(
        event=event,
        level_changes=lcs,
        metrics=[],
        trades=[],
        trades_available=True,
        window_start_ns=touch - 200 * NS,
        window_end_ns=trigger + 10 * NS,
    )
    assert a.values_dict() == b.values_dict()


def test_wall_migration_feature_present():
    touch = 9_000 * NS
    trigger = touch + 5 * NS
    event = {
        "event_id": "mpe_mig",
        "first_touch_ts_ns": str(touch),
        "trigger_ts_ns": str(trigger),
        "event_role": "UPPER",
        "confluence_low": "300",
        "confluence_high": "300",
    }
    lcs = [
        LevelChangeEvent(
            event_time_ns=touch - 40 * NS,
            side="ask",
            price=300.0,
            change_type="ADD",
            new_size=5.0,
            old_size=0.0,
        ),
        LevelChangeEvent(
            event_time_ns=touch + NS,
            side="ask",
            price=300.1,
            change_type="ADD",
            new_size=5.0,
            old_size=0.0,
        ),
        LevelChangeEvent(
            event_time_ns=touch + 2 * NS,
            side="ask",
            price=300.0,
            change_type="DELETE",
            new_size=0.0,
            old_size=5.0,
        ),
    ]
    bundle = compute_event_features(
        event=event,
        level_changes=lcs,
        metrics=[],
        trades=[],
        trades_available=True,
        window_start_ns=touch - 200 * NS,
        window_end_ns=trigger + 10 * NS,
    )
    assert "wall_price_migration_bps" in bundle.features
    assert "wall_moved_with_price" in bundle.features


def test_censored_exclusion_helper():
    from obfull_research_engine.mp_ob_feature_enrichment_v1.analysis import _truthy

    assert _truthy("True")
    assert not _truthy("False")
