"""Offline tests for mp_qdh_wall_linkage_audit_v1."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
_shadow = str(REPO_ROOT / "src")
while _shadow in sys.path:
    sys.path.remove(_shadow)
sys.path.insert(0, str(ENGINE_ROOT / "src"))
_ORDERBOOK_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if _ORDERBOOK_SRC.is_dir():
    sys.path.insert(0, str(_ORDERBOOK_SRC))

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
    BookNode,
    attribute_intervals,
    is_wall_attack_trade,
    band_bounds,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import build_zone_bands  # noqa: E402
from obfull_research_engine.mp_qdh_wall_linkage_audit_v1 import ALLOW_CLICKHOUSE_WRITES  # noqa: E402
from obfull_research_engine.mp_qdh_wall_linkage_audit_v1.flow_series import (  # noqa: E402
    aggregate_1s,
    assert_1s_matches_100ms,
)
from obfull_research_engine.mp_qdh_wall_linkage_audit_v1.wall_track import (  # noqa: E402
    track_pre_touch_walls,
)


def _lc(side: str, price: float, new: float, ts_ns: int, ao: int = 1, old: float | None = None, ct: str = "UPDATE") -> LevelChangeEvent:
    return LevelChangeEvent(
        event_time_ns=ts_ns,
        side=side,
        price=price,
        change_type=ct,
        new_size=new,
        old_size=old,
        apply_order=ao,
        chunk_key="t",
        source_record_ordinal=ao,
    )


def test_01_02_upper_ask_lower_bid_true_break_no_flip():
    u = build_zone_bands(role="UPPER", low=100.0, high=100.0)
    l = build_zone_bands(role="LOWER", low=100.0, high=100.0)
    assert u.defense_side == "ask"
    assert l.defense_side == "bid"
    assert build_zone_bands(role="LOWER", low=1.0, high=2.0).defense_side == "bid"


def test_03_04_05_06_07_08_09_10_wall_linkage_classes():
    touch = 2_000_000_000_000
    pre = touch - 120_000_000_000
    lcs = [_lc("ask", 100.0, 5.0, touch - 10, old=0.0, ct="ADD")]
    r = track_pre_touch_walls(
        level_changes=lcs,
        event_role="UPPER",
        confluence_low=100.0,
        confluence_high=100.0,
        zone_id="z",
        touch_price=100.0,
        zone_touch_ns=touch,
        pre_touch_start_ns=pre,
    )
    assert r.status == "PRESENT_AT_TOUCH"
    assert r.selected is not None

    lcs2 = [
        _lc("ask", 100.0, 10.0, touch - 50_000_000_000, old=0.0, ct="ADD"),
        _lc("ask", 100.0, 0.0, touch - 1_000_000_000, old=10.0, ct="DELETE"),
    ]
    r2 = track_pre_touch_walls(
        level_changes=lcs2,
        event_role="UPPER",
        confluence_low=100.0,
        confluence_high=100.0,
        zone_id="z",
        touch_price=100.0,
        zone_touch_ns=touch,
        pre_touch_start_ns=pre,
        attributed_fill_by_price={100.0: 9.0},
    )
    assert r2.status == "DEPLETED_BEFORE_TOUCH"

    r3 = track_pre_touch_walls(
        level_changes=lcs2,
        event_role="UPPER",
        confluence_low=100.0,
        confluence_high=100.0,
        zone_id="z",
        touch_price=100.0,
        zone_touch_ns=touch,
        pre_touch_start_ns=pre,
        attributed_fill_by_price={100.0: 0.0},
    )
    assert r3.status == "PULLED_BEFORE_TOUCH"

    lcs4 = [
        _lc("ask", 100.0, 8.0, touch - 20_000_000_000, old=0.0, ct="ADD"),
        _lc("ask", 100.0, 0.0, touch - 2_000_000_000, old=8.0, ct="DELETE"),
        _lc("ask", 100.3, 8.0, touch - 1_500_000_000, old=0.0, ct="ADD"),
        _lc("ask", 100.3, 0.0, touch - 100, old=8.0, ct="DELETE"),
    ]
    r4 = track_pre_touch_walls(
        level_changes=lcs4,
        event_role="UPPER",
        confluence_low=100.0,
        confluence_high=100.0,
        zone_id="z",
        touch_price=100.0,
        zone_touch_ns=touch,
        pre_touch_start_ns=pre,
    )
    assert r4.status in ("MOVED_BEFORE_TOUCH", "PULLED_BEFORE_TOUCH", "DEPLETED_BEFORE_TOUCH")

    r5 = track_pre_touch_walls(
        level_changes=[],
        event_role="UPPER",
        confluence_low=100.0,
        confluence_high=100.0,
        zone_id="z",
        touch_price=100.0,
        zone_touch_ns=touch,
        pre_touch_start_ns=pre,
    )
    assert r5.status == "NO_CANONICAL_WALL"

    lcs6 = [_lc("ask", 100.0, 9.0, touch + 1_000_000, old=0.0, ct="ADD")]
    r6 = track_pre_touch_walls(
        level_changes=lcs6,
        event_role="UPPER",
        confluence_low=100.0,
        confluence_high=100.0,
        zone_id="z",
        touch_price=100.0,
        zone_touch_ns=touch,
        pre_touch_start_ns=pre,
    )
    assert r6.status == "NO_CANONICAL_WALL"

    a = track_pre_touch_walls(
        level_changes=lcs, event_role="UPPER", confluence_low=100.0, confluence_high=100.0,
        zone_id="z", touch_price=100.0, zone_touch_ns=touch, pre_touch_start_ns=pre,
    )
    b = track_pre_touch_walls(
        level_changes=lcs, event_role="UPPER", confluence_low=100.0, confluence_high=100.0,
        zone_id="z", touch_price=100.0, zone_touch_ns=touch, pre_touch_start_ns=pre,
    )
    assert a.status == b.status and a.selected["candidate_wall_id"] == b.selected["candidate_wall_id"]


def test_11_13_14_15_16_17_19_trade_and_mass_balance():
    rows = [
        {"trade_id": "a", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Buy"},
        {"trade_id": "a", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 100.0, "size": 1.0, "taker_side": "Buy"},
    ]
    kept, dedup, dropped = build_canonical_trades(rows, source_file="t")
    assert dedup.duplicate_count >= 1
    assert len(kept) == 1
    lo, hi = band_bounds(100.0, tick_size=0.1, band_ticks=5)
    assert is_wall_attack_trade(kept[0], view="defended_band", wall_price=100.0, band_low=lo, band_high=hi, wall_side="ask")
    outside, _, _ = build_canonical_trades(
        [{"trade_id": "x", "trade_ts": "2026-09-06T20:19:02.232Z", "price": 200.0, "size": 1.0, "taker_side": "Buy"}],
        source_file="t",
    )
    assert not is_wall_attack_trade(outside[0], view="defended_band", wall_price=100.0, band_low=lo, band_high=hi, wall_side="ask")
    m = decompose_mass_balance(queue_before=10, queue_after=5, attributed_hit_qty=2)
    assert m.residual_pull_qty == 3
    m2 = decompose_mass_balance(queue_before=10, queue_after=12, attributed_hit_qty=1)
    assert m2.net_refill_qty == 3
    assert m2.attributed_hit_qty == 1
    t0 = datetime(2026, 9, 6, 20, 19, 0, tzinfo=timezone.utc)
    nodes = [
        BookNode(t0, t0, None, 10.0, None, None, None, 0, "a", "a"),
        BookNode(t0 + timedelta(seconds=1), t0 + timedelta(seconds=1), None, 10.0, None, None, None, 1, "b", "b"),
    ]
    consumed: set[str] = set()
    attribute_intervals(
        nodes=nodes, trades=kept, view="defended_band", wall_price=100.0, band_low=lo, band_high=hi,
        wall_visible_at=t0 - timedelta(seconds=10), analysis_end_exclusive=t0 + timedelta(hours=1),
        wall_side="ask", consumed_trade_ids=consumed,
    )
    n1 = len(consumed)
    attribute_intervals(
        nodes=nodes, trades=kept, view="defended_band", wall_price=100.0, band_low=lo, band_high=hi,
        wall_visible_at=t0 - timedelta(seconds=10), analysis_end_exclusive=t0 + timedelta(hours=1),
        wall_side="ask", consumed_trade_ids=consumed,
    )
    assert len(consumed) == n1


def test_20_23_24_qdh_and_near_zero():
    st = update_qdh(QdhState(), net_depletion_qty=1, interval_duration_s=0.1, current_queue=10, persistence_ratio=1)
    assert st.qdh_base > 0
    from obfull_research_engine.mp_qdh_canonical_integration_v1.near_zero import apply_queue_policy
    pol = apply_queue_policy(queue_remaining_qty=0.0, qdh_base=1e9, queue_runway_seconds=1e-9)
    assert pol["queue_state"] == "QUEUE_EXHAUSTED"
    assert pol["canonical_qdh_base"] is None


def test_21_22_25_phases_and_1s_agg():
    from obfull_research_engine.mp_qdh_wall_linkage_audit_v1.flow_series import _phase
    touch = datetime(2026, 9, 10, 13, 30, 0, tzinfo=timezone.utc)
    trig = touch + timedelta(seconds=10)
    assert _phase(touch - timedelta(seconds=1), touch, trig) == "PRE_TOUCH"
    assert _phase(touch + timedelta(seconds=1), touch, trig) == "TOUCH_TO_TRIGGER"
    assert _phase(trig + timedelta(seconds=1), touch, trig) == "POST_TRIGGER_FORENSIC"
    rows = [
        {
            "event_id": "e",
            "bucket_start": "2026-09-10T13:30:00.000Z",
            "attributed_fill": 1.0,
            "residual_pull": 0.0,
            "refill": 0.0,
            "net_depletion": 1.0,
            "book_decrease": 1.0,
            "wall_id": "w",
            "wall_price": 1.0,
            "band_low": 0.5,
            "band_high": 1.5,
            "wall_state": "QUEUE_POSITIVE",
            "phase": "PRE_TOUCH",
            "post_decision": False,
            "mid": 1.0,
            "microprice": 1.0,
            "spread_bps": 1.0,
            "cumulative_fill": 1.0,
            "cumulative_pull": 0.0,
            "cumulative_refill": 0.0,
            "qdh_ewma": 0.1,
            "queue_end": 9.0,
            "relative_seconds_to_touch": 0.0,
            "relative_seconds_to_trigger": -10.0,
        },
        {
            "event_id": "e",
            "bucket_start": "2026-09-10T13:30:00.100Z",
            "attributed_fill": 2.0,
            "residual_pull": 0.0,
            "refill": 0.0,
            "net_depletion": 2.0,
            "book_decrease": 2.0,
            "wall_id": "w",
            "wall_price": 1.0,
            "band_low": 0.5,
            "band_high": 1.5,
            "wall_state": "QUEUE_POSITIVE",
            "phase": "PRE_TOUCH",
            "post_decision": False,
            "mid": 1.0,
            "microprice": 1.0,
            "spread_bps": 1.0,
            "cumulative_fill": 3.0,
            "cumulative_pull": 0.0,
            "cumulative_refill": 0.0,
            "qdh_ewma": 0.2,
            "queue_end": 7.0,
            "relative_seconds_to_touch": 0.1,
            "relative_seconds_to_trigger": -9.9,
        },
    ]
    one = aggregate_1s(rows)
    assert assert_1s_matches_100ms(rows, one)
    assert abs(one[0]["attributed_fill"] - 3.0) < 1e-9


def test_27_28_no_writes_flag():
    assert ALLOW_CLICKHOUSE_WRITES is False


def test_18_bucket_mass_balance_identity():
    m = decompose_mass_balance(queue_before=10, queue_after=7, attributed_hit_qty=3)
    assert m.identity_ok
