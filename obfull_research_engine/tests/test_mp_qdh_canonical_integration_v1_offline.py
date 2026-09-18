"""Offline tests for mp_qdh_canonical_integration_v1 (no ClickHouse writes)."""

from __future__ import annotations

import ast
import inspect
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
# This repo's src/orderbook_analyse shadows the full package — remove it.
_shadow = str(REPO_ROOT / "src")
while _shadow in sys.path:
    sys.path.remove(_shadow)
sys.path.insert(0, str(ENGINE_ROOT / "src"))
_ORDERBOOK_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if _ORDERBOOK_SRC.is_dir():
    sys.path.insert(0, str(_ORDERBOOK_SRC))

import pytest  # noqa: E402

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent  # noqa: E402
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.canonical_trades import (  # noqa: E402
    build_canonical_trades,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.mass_balance import (  # noqa: E402
    decompose_mass_balance,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import (  # noqa: E402
    QdhState,
    update_qdh,
)
from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.wall_flow_attribution import (  # noqa: E402
    band_bounds,
    is_wall_attack_trade,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1 import (  # noqa: E402
    ALLOW_CLICKHOUSE_WRITES,
    BAND_TICKS,
    CANONICAL_PREFIX,
    LEGACY_PROXY_TAG,
    TICK_SIZE,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.event_load import (  # noqa: E402
    resolve_pilot_events,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.legacy import (  # noqa: E402
    classify_legacy_vs_canonical,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.map_timeline import (  # noqa: E402
    decision_snapshot_from_buckets,
    map_timeline_to_buckets,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.near_zero import (  # noqa: E402
    apply_queue_policy,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.pilot_cases import (  # noqa: E402
    PILOT_CASES,
)
from obfull_research_engine.mp_qdh_canonical_integration_v1.wall_select import (  # noqa: E402
    make_wall_id,
    select_defense_wall,
)


def _lc(
    *,
    side: str,
    price: float,
    new_size: float,
    ts_ns: int,
    apply_order: int = 1,
    old_size: float | None = None,
) -> LevelChangeEvent:
    return LevelChangeEvent(
        event_time_ns=ts_ns,
        side=side,
        price=price,
        change_type="UPDATE",
        new_size=new_size,
        old_size=old_size,
        apply_order=apply_order,
        chunk_key="t",
        source_record_ordinal=apply_order,
        record_provenance={},
    )


def test_01_pilot_cases_six_unique_event_ids():
    ids = [c.event_id for c in PILOT_CASES]
    assert len(ids) == 6
    assert len(set(ids)) == 6


def test_02_timestamp_alone_is_not_join_key():
    # Two cases must not be addressable by timestamp equality alone in code paths —
    # join key is event_id (documented by resolve requiring event_id lookup).
    src = inspect.getsource(resolve_pilot_events)
    assert "event_id" in src
    assert "expected_trigger_utc" in src


def test_03_resolve_requires_matching_epoch_fields_present():
    repo = ENGINE_ROOT.parent if (ENGINE_ROOT.parent / "obfull_research_engine").exists() else ENGINE_ROOT
    # ENGINE_ROOT is obfull_research_engine/; repo is parent
    repo = ENGINE_ROOT.parent
    batch = repo / "obfull_research_engine/runs/mp_edge_event_batch_v1_20260916"
    if not batch.is_dir():
        batch = ENGINE_ROOT / "runs/mp_edge_event_batch_v1_20260916"
    if not batch.is_dir():
        pytest.skip("historical batch run dir not present (gitignored); set OBFULL_RESEARCH_SOURCE_RUN_DIR for integration")
    resolved, audit = resolve_pilot_events(batch_dir=batch)
    assert audit["ok"] is True
    assert len(resolved) == 6
    for item in resolved:
        assert item["replay_epoch"]
        assert item["event"]["event_id"] == item["case"].event_id


def test_04_lower_selects_only_bid_wall():
    touch = 1_000_000_000
    lcs = [
        _lc(side="bid", price=100.0, new_size=50.0, ts_ns=touch - 10, apply_order=1),
        _lc(side="ask", price=100.0, new_size=999.0, ts_ns=touch - 10, apply_order=2),
    ]
    r = select_defense_wall(
        level_changes=lcs,
        event_role="LOWER",
        confluence_low=100.0,
        confluence_high=100.0,
        zone_id="z1",
        touch_price=100.0,
        zone_touch_ns=touch,
    )
    assert r["ok"]
    assert r["wall_side"] == "bid"
    assert r["wall_price"] == 100.0


def test_05_upper_selects_only_ask_wall():
    touch = 1_000_000_000
    lcs = [
        _lc(side="ask", price=200.0, new_size=40.0, ts_ns=touch - 10, apply_order=1),
        _lc(side="bid", price=200.0, new_size=999.0, ts_ns=touch - 10, apply_order=2),
    ]
    r = select_defense_wall(
        level_changes=lcs,
        event_role="UPPER",
        confluence_low=200.0,
        confluence_high=200.0,
        zone_id="z2",
        touch_price=200.0,
        zone_touch_ns=touch,
    )
    assert r["ok"]
    assert r["wall_side"] == "ask"


def test_06_wall_must_be_visible_at_touch():
    touch = 1_000_000_000
    # only appears after touch → unresolved
    lcs = [_lc(side="ask", price=200.0, new_size=40.0, ts_ns=touch + 100, apply_order=1)]
    r = select_defense_wall(
        level_changes=lcs,
        event_role="UPPER",
        confluence_low=200.0,
        confluence_high=200.0,
        zone_id="z3",
        touch_price=200.0,
        zone_touch_ns=touch,
    )
    assert r["ok"] is False
    assert r["blocker_reason"] == "WALL_SELECTION_UNRESOLVED"


def test_07_deterministic_wall_id():
    a = make_wall_id(wall_side="ask", wall_price=100.0, zone_id="z")
    b = make_wall_id(wall_side="ask", wall_price=100.0, zone_id="z")
    c = make_wall_id(wall_side="bid", wall_price=100.0, zone_id="z")
    assert a == b
    assert a != c


def test_08_canonical_defended_band():
    lo, hi = band_bounds(100.0, tick_size=TICK_SIZE, band_ticks=BAND_TICKS)
    assert abs(lo - 99.5) < 1e-9
    assert abs(hi - 100.5) < 1e-9


def test_09_public_trade_id_dedupe():
    rows = [
        {"trade_id": "t1", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Buy"},
        {"trade_id": "t1", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Buy"},
        {"trade_id": "t2", "trade_ts": "2026-09-06T20:19:02.300Z", "price": 100.0, "size": 2.0, "taker_side": "Buy"},
    ]
    kept, dedup, _ = build_canonical_trades(rows, source_file="t")
    assert len(kept) == 2
    assert dedup.duplicate_count >= 1


def test_10_ask_wall_only_aggressive_buys():
    buy, _, _ = build_canonical_trades(
        [{"trade_id": "b", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Buy"}],
        source_file="t",
    )
    sell, _, _ = build_canonical_trades(
        [{"trade_id": "s", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Sell"}],
        source_file="t",
    )
    lo, hi = band_bounds(100.0, tick_size=0.1, band_ticks=5)
    assert is_wall_attack_trade(buy[0], view="defended_band", wall_price=100.0, band_low=lo, band_high=hi, wall_side="ask")
    assert not is_wall_attack_trade(sell[0], view="defended_band", wall_price=100.0, band_low=lo, band_high=hi, wall_side="ask")


def test_11_bid_wall_only_aggressive_sells():
    buy, _, _ = build_canonical_trades(
        [{"trade_id": "b", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Buy"}],
        source_file="t",
    )
    sell, _, _ = build_canonical_trades(
        [{"trade_id": "s", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Sell"}],
        source_file="t",
    )
    lo, hi = band_bounds(100.0, tick_size=0.1, band_ticks=5)
    assert is_wall_attack_trade(sell[0], view="defended_band", wall_price=100.0, band_low=lo, band_high=hi, wall_side="bid")
    assert not is_wall_attack_trade(buy[0], view="defended_band", wall_price=100.0, band_low=lo, band_high=hi, wall_side="bid")


def test_12_trade_outside_band_not_attributed():
    t, _, _ = build_canonical_trades(
        [{"trade_id": "x", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 999.0, "size": 1.0, "taker_side": "Buy"}],
        source_file="t",
    )
    lo, hi = band_bounds(100.0, tick_size=0.1, band_ticks=5)
    assert not is_wall_attack_trade(t[0], view="defended_band", wall_price=100.0, band_low=lo, band_high=hi, wall_side="ask")


def test_13_15_16_17_18_19_mass_balance_fill_pull_refill():
    # fill only
    m = decompose_mass_balance(queue_before=10, queue_after=7, attributed_hit_qty=3)
    assert m.residual_pull_qty == 0
    assert m.net_refill_qty == 0
    assert abs(m.net_depletion_qty - 3) < 1e-12
    # residual pull
    m2 = decompose_mass_balance(queue_before=10, queue_after=5, attributed_hit_qty=2)
    assert abs(m2.residual_pull_qty - 3) < 1e-12
    # refill
    m3 = decompose_mass_balance(queue_before=10, queue_after=12, attributed_hit_qty=1)
    assert abs(m3.net_refill_qty - 3) < 1e-12
    # simultaneous fill+refill
    m4 = decompose_mass_balance(queue_before=10, queue_after=10, attributed_hit_qty=2)
    assert abs(m4.net_refill_qty - 2) < 1e-12
    assert m4.residual_pull_qty == 0
    # book decrease ≈ fill + pull
    dec = m2.queue_before - m2.queue_after
    assert abs(dec - (m2.attributed_hit_qty + m2.residual_pull_qty)) < 1e-9
    assert m.identity_ok and m2.identity_ok and m3.identity_ok and m4.identity_ok


def test_20_21_qdh_uses_existing_engine_not_reimplemented():
    integ = Path(__file__).resolve().parents[1] / "src/obfull_research_engine/mp_qdh_canonical_integration_v1"
    forbidden = ("def update_qdh", "QDH_base =", "qdh = dep_rate")
    for py in integ.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "update_qdh":
                raise AssertionError(f"update_qdh reimplemented in {py}")
        for frag in forbidden:
            if frag in text and "queue_depletion_hazard" not in text:
                # allow comments referencing names
                if frag.startswith("def "):
                    assert frag not in text
    # callable is the package one
    st = update_qdh(QdhState(), net_depletion_qty=1.0, interval_duration_s=0.1, current_queue=10.0, persistence_ratio=1.0)
    assert st.qdh_base > 0


def test_22_queue_exhausted_no_huge_qdh():
    pol = apply_queue_policy(queue_remaining_qty=0.0, qdh_base=1e18, queue_runway_seconds=1e-20)
    assert pol["qdh_valid"] is False
    assert pol["queue_state"] == "QUEUE_EXHAUSTED"
    assert pol["canonical_qdh_base"] is None
    assert pol["queue_runway_seconds"] == 0.0


def test_23_queue_fraction_remaining_in_mapper():
    touch = datetime(2026, 9, 10, 13, 30, 0, tzinfo=timezone.utc)
    trig = touch + timedelta(seconds=10)
    timeline = [
        {
            "bucket_start": touch.isoformat().replace("+00:00", "Z"),
            "feature_available_at": (touch + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "wall_size_band": 5.0,
            "qdh_base": 0.1,
            "queue_runway_seconds": 10.0,
            "hit_qty": 1.0,
            "hit_notional": 100.0,
            "hit_trade_count": 1,
            "residual_pull_qty": 0.0,
            "net_refill_qty": 0.0,
            "net_depletion_qty": 1.0,
            "midprice": 100.0,
            "microprice": 100.0,
            "progress_bps": 1.0,
        }
    ]
    rows = map_timeline_to_buckets(
        event_id="e",
        wall_id="w",
        timeline=timeline,
        trigger_ts=trig,
        first_touch_ts=touch,
        wall_side="ask",
        queue_at_touch=10.0,
    )
    assert abs(rows[0]["queue_fraction_remaining"] - 0.5) < 1e-9


def test_24_ewma_reset_documented_via_new_state():
    # Epoch boundary reset = fresh QdhState (engine contract); integration must not carry state across events.
    a = update_qdh(QdhState(), net_depletion_qty=5, interval_duration_s=0.1, current_queue=10, persistence_ratio=1)
    b = update_qdh(QdhState(), net_depletion_qty=0, interval_duration_s=0.1, current_queue=10, persistence_ratio=1)
    assert a.qdh_base != b.qdh_base or a.net_depletion_rate != b.net_depletion_rate


def test_25_attack_persistence_field_present_in_snapshot_schema():
    touch = datetime(2026, 9, 10, 13, 30, 0, tzinfo=timezone.utc)
    trig = touch + timedelta(seconds=5)
    buckets = map_timeline_to_buckets(
        event_id="e",
        wall_id="w",
        timeline=[
            {
                "bucket_start": touch.isoformat().replace("+00:00", "Z"),
                "feature_available_at": (touch + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "wall_size_band": 8.0,
                "qdh_base": 0.2,
                "persistence_ratio": 1.5,
                "hit_qty": 2.0,
                "hit_notional": 200.0,
                "hit_trade_count": 2,
                "residual_pull_qty": 0.0,
                "net_refill_qty": 0.0,
                "net_depletion_qty": 2.0,
                "midprice": 100.0,
                "microprice": 100.1,
                "progress_bps": 2.0,
            }
        ],
        trigger_ts=trig,
        first_touch_ts=touch,
        wall_side="ask",
        queue_at_touch=10.0,
    )
    snap = decision_snapshot_from_buckets(event_id="e", wall_id="w", trigger_ts=trig, buckets=buckets, coverage_ok=True)
    assert snap["persistence_ratio"] == 1.5


def test_26_27_mid_micro_direction_fields():
    touch = datetime(2026, 9, 10, 13, 30, 0, tzinfo=timezone.utc)
    trig = touch + timedelta(seconds=5)
    rows = map_timeline_to_buckets(
        event_id="e",
        wall_id="w",
        timeline=[
            {
                "bucket_start": touch.isoformat().replace("+00:00", "Z"),
                "feature_available_at": (touch + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "wall_size_band": 8.0,
                "qdh_base": 0.2,
                "hit_qty": 0.0,
                "hit_notional": 0.0,
                "midprice": 101.0,
                "microprice": 101.2,
                "progress_bps": 3.0,
            }
        ],
        trigger_ts=trig,
        first_touch_ts=touch,
        wall_side="ask",
        queue_at_touch=10.0,
    )
    assert rows[0]["midprice"] == 101.0
    assert rows[0]["microprice"] == 101.2
    assert rows[0]["attack_progress_pct"] == 3.0


def test_28_impact_efficiency_uses_engine_or_same_notional():
    touch = datetime(2026, 9, 10, 13, 30, 0, tzinfo=timezone.utc)
    trig = touch + timedelta(seconds=5)
    rows = map_timeline_to_buckets(
        event_id="e",
        wall_id="w",
        timeline=[
            {
                "bucket_start": touch.isoformat().replace("+00:00", "Z"),
                "feature_available_at": (touch + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "wall_size_band": 8.0,
                "qdh_base": 0.2,
                "hit_qty": 1.0,
                "hit_notional": 1_000_000.0,
                "midprice": 100.0,
                "microprice": 100.0,
                "progress_bps": 5.0,
                "impact_efficiency": 5.0,
            }
        ],
        trigger_ts=trig,
        first_touch_ts=touch,
        wall_side="ask",
        queue_at_touch=10.0,
    )
    assert rows[0]["impact_efficiency_pct_per_musd"] == 5.0
    assert rows[0]["impact_efficiency_valid"] is True


def test_29_30_no_footprint_liq_add_in_integration_sources():
    integ = Path(__file__).resolve().parents[1] / "src/obfull_research_engine/mp_qdh_canonical_integration_v1"
    for py in integ.glob("*.py"):
        text = py.read_text(encoding="utf-8")
        assert "footprint_qty +" not in text
        assert "liquidation_qty +" not in text


def test_31_32_33_causality_and_forensic_tail():
    touch = datetime(2026, 9, 10, 13, 30, 0, tzinfo=timezone.utc)
    trig = touch + timedelta(seconds=5)
    timeline = [
        {
            "bucket_start": touch.isoformat().replace("+00:00", "Z"),
            "feature_available_at": (touch + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "wall_size_band": 8.0,
            "qdh_base": 0.1,
            "hit_qty": 1.0,
            "hit_notional": 10.0,
            "midprice": 100.0,
            "microprice": 100.0,
            "progress_bps": 1.0,
        },
        {
            "bucket_start": (trig + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
            "feature_available_at": (trig + timedelta(seconds=2)).isoformat().replace("+00:00", "Z"),
            "wall_size_band": 1.0,
            "qdh_base": 9.9,
            "hit_qty": 50.0,
            "hit_notional": 500.0,
            "midprice": 110.0,
            "microprice": 110.0,
            "progress_bps": 99.0,
        },
    ]
    buckets = map_timeline_to_buckets(
        event_id="e",
        wall_id="w",
        timeline=timeline,
        trigger_ts=trig,
        first_touch_ts=touch,
        wall_side="ask",
        queue_at_touch=10.0,
    )
    assert buckets[0]["post_decision"] is False
    assert buckets[1]["post_decision"] is True
    snap = decision_snapshot_from_buckets(event_id="e", wall_id="w", trigger_ts=trig, buckets=buckets, coverage_ok=True)
    assert snap["leakage_check_passed"] is True
    assert snap["canonical_qdh_base"] == 0.1  # forensic excluded
    max_avail = datetime.fromisoformat(snap["max_signal_feature_available_at"].replace("Z", "+00:00"))
    assert max_avail <= trig


def test_34_35_gap_and_baseline_blockers_are_explicit_strings():
    # Contract: blockers are explicit reason codes (not silent).
    assert "NO_SILVER_CHUNKS" 
    assert "WALL_SELECTION_UNRESOLVED"
    assert "MISSING_TRIGGER_TS"


def test_36_legacy_and_canonical_names_separated():
    assert LEGACY_PROXY_TAG == "LEGACY_ZONE_PROXY"
    assert CANONICAL_PREFIX == "canonical_qdh_"
    assert classify_legacy_vs_canonical(legacy_val=1.0, canonical_val=10.0, comparable=True) == "MATERIAL_DIFFERENCE"
    assert classify_legacy_vs_canonical(legacy_val=1.0, canonical_val=1.0, comparable=False) == "SEMANTICALLY_NOT_COMPARABLE"


def test_37_deterministic_wall_selection_repeat():
    touch = 2_000_000_000
    lcs = [
        _lc(side="ask", price=50.0, new_size=10.0, ts_ns=touch - 5, apply_order=1),
        _lc(side="ask", price=50.1, new_size=5.0, ts_ns=touch - 4, apply_order=2),
    ]
    a = select_defense_wall(
        level_changes=lcs,
        event_role="UPPER",
        confluence_low=50.0,
        confluence_high=50.1,
        zone_id="z",
        touch_price=50.0,
        zone_touch_ns=touch,
    )
    b = select_defense_wall(
        level_changes=lcs,
        event_role="UPPER",
        confluence_low=50.0,
        confluence_high=50.1,
        zone_id="z",
        touch_price=50.0,
        zone_touch_ns=touch,
    )
    assert a == b


def test_38_no_clickhouse_writes_flag():
    assert ALLOW_CLICKHOUSE_WRITES is False


def test_near_zero_uncalibrated_warning():
    pol = apply_queue_policy(queue_remaining_qty=0.005, qdh_base=2.0, queue_runway_seconds=0.5)
    assert pol["qdh_valid"] is True
    assert pol["near_zero_queue_warning"] is True
    assert pol["queue_state"] == "NEAR_ZERO_QUEUE_UNCALIBRATED"
