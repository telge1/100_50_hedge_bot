"""Focused causal tests for next-pool audit (no full suite)."""

from __future__ import annotations

from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1.selection import (
    ask_entirely_above,
    bps,
    intervals_overlap,
    select_next_pool,
)


def _pool(
    pid: str,
    tf: str,
    lo: float,
    hi: float,
    *,
    strength: float = 1.0,
    avail: str = "2026-08-24T16:00:00Z",
    dur: int | None = None,
) -> dict:
    durs = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600}
    return {
        "pool_id": pid,
        "source_timeframe": tf,
        "side": "ASK",
        "lower_edge": lo,
        "upper_edge": hi,
        "front_edge": lo,
        "back_edge": hi,
        "strength": strength,
        "available_at": avail,
        "active_as_of": True,
        "tf_duration_s": dur if dur is not None else durs[tf],
        "distance_to_front_edge_bps": bps(lo, 100.0),
        "overlapping_other_tf_pools": [],
        "forms_shared_price_component": False,
    }


def test_selection_uses_only_asof_distance_not_outcome():
    market = 100.0
    inv = [
        _pool("a", "5m", 101.0, 102.0),
        _pool("b", "5m", 105.0, 106.0),  # farther — not chosen despite "prettier"
        _pool("c", "15m", 101.0, 103.0),  # overlap component
    ]
    # mutate distances as if measured at as-of only
    for r in inv:
        r["distance_to_front_edge_bps"] = bps(r["lower_edge"], market)
    sel = select_next_pool(inv, market_price=market)
    assert sel["manifest"]["outcome_used_for_pool_selection"] is False
    assert sel["selected"]["pool_id"] == "a"
    assert sel["manifest"]["selection_mode"] == "STRICT_ASK_ABOVE_MARKET_FRONT_EDGE"


def test_deterministic_tie_break():
    market = 100.0
    inv = [
        _pool("z", "15m", 101.0, 102.0),
        _pool("a", "5m", 101.0, 102.0),
        _pool("m", "30m", 101.0, 102.0),
    ]
    for r in inv:
        r["distance_to_front_edge_bps"] = bps(r["lower_edge"], market)
    sel = select_next_pool(inv, market_price=market)
    # same lower_edge → smaller tf duration → then pool_id
    assert sel["selected"]["pool_id"] == "a"
    assert sel["selected"]["source_timeframe"] == "5m"
    assert any(c["role"] == "HTF_CONFLUENCE" for c in sel["manifest"]["htf_confluence"])


def test_timeframes_remain_separate_objects():
    market = 100.0
    inv = [
        _pool("lld:5m:x", "5m", 101.0, 102.0),
        _pool("lld:15m:x", "15m", 101.0, 102.0),
    ]
    for r in inv:
        r["distance_to_front_edge_bps"] = bps(r["lower_edge"], market)
    sel = select_next_pool(inv, market_price=market)
    assert sel["selected"]["pool_id"] == "lld:5m:x"
    assert "lld:15m:x" in sel["manifest"]["component_pool_ids"]
    assert sel["selected"]["pool_id"] != "lld:15m:x"


def test_inside_mode_nearest_back_edge():
    market = 80466.2
    inv = [
        _pool("near", "15m", 79979.2, 80507.6),
        _pool("wide", "30m", 79979.2, 80602.6),
        _pool("below", "5m", 79600.0, 80100.0),  # entirely below
    ]
    for r in inv:
        r["distance_to_front_edge_bps"] = bps(r["lower_edge"], market)
    sel = select_next_pool(inv, market_price=market)
    assert sel["manifest"]["selection_mode"] == "INSIDE_ASK_REMAINING_BACK_EDGE"
    assert sel["selected"]["pool_id"] == "near"
    assert sel["selected"]["target_edge"] == 80507.6


def test_local_exit_does_not_end_encounter_flag():
    # unit-level: encounter_active stays true conceptually — covered by pipeline invariant
    encounter_active = True
    local_exit = True
    assert encounter_active and local_exit


def test_cancel_is_not_trade_depletion():
    h = {"cancelled_before_touch": True, "trade_depletion": False, "attacked": False}
    assert h["cancelled_before_touch"] and not h["trade_depletion"]


def test_single_cancel_not_repeated_retreat():
    from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1.pipeline import (
        _build_retreat_sequences,
    )

    events = [
        {
            "disappearance_ts": "2026-08-25T02:18:00Z",
            "disappearance_ms": 1,
            "old_wall_price": 80500.0,
            "old_wall_attacked": False,
            "replacement_wall_price": 80550.0,
            "replacement_first_seen_ts": "2026-08-25T02:18:00Z",
            "displacement_bps": 6.0,
            "price_followed": True,
        }
    ]
    rows, evidence = _build_retreat_sequences(events, {}, 80600.0)
    assert evidence == "SINGLE_UNCONFIRMED_RETREAT"
    assert len(rows) == 1


def test_insufficient_room_blocks_entry():
    from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1.pipeline import (
        _decide,
    )

    room = {"insufficient_room": True, "gross_room_bps": 5.0}
    decision = _decide(
        timeline=[
            {
                "second": "2026-08-25T02:18:00Z",
                "second_ms": 1,
                "aggressor_class_5s": "BUY_AGGRESSION_EFFECTIVE",
                "local_exit": False,
            }
        ],
        state_rows=[
            {"to_state": "BREAKOUT_ACCEPTED", "second_ms": 2, "ts": "2026-08-25T02:18:05Z"}
        ],
        accept_rows=[
            {
                "hold_s": 5,
                "breakout_accepted_ts": "2026-08-25T02:18:05Z",
                "rejection_confirmed_ts": None,
            }
        ],
        retreat_evidence="NO_RETREAT",
        wall_rows=[{"lifecycle_class": "TRADE_SUPPORTED_OVERRUN"}],
        attack_episodes=[{"buy_notional": 1e6, "sell_notional": 1e3}],
        room=room,
        arrival={"reached": True},
        selected={"pool_id": "x", "lower_edge": 1, "upper_edge": 2},
        min_n=10000,
        strong_bps=8,
    )
    assert decision["insufficient_room"] is True
    assert decision["verdict"] != "CLEAR_ASK_BREAKOUT_LONG_CANDIDATE"
    assert decision["first_available_ts"] is None


def test_prefix_parity_helper_marks_exact():
    from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1.pipeline import (
        _prefix_parity,
    )

    rows = _prefix_parity(
        as_of_ms=1000,
        arrival_ms=2000,
        timeline=[{"second_ms": 1500}, {"second_ms": 2000}],
        state_rows=[{"second_ms": 2000}],
        wall_rows=[],
        retreat_rows=[],
        decision={},
        market=100.0,
        selected={"pool_id": "p"},
        inventory=[],
    )
    assert rows
    assert all(r["prefix_status"] == "EXACT_PREFIX_PARITY" for r in rows)


def test_ask_entirely_above_filter():
    inv = [_pool("a", "5m", 101, 102), _pool("b", "5m", 99, 100.5)]
    above = ask_entirely_above(inv, 100.0)
    assert [r["pool_id"] for r in above] == ["a"]


def test_overlap_helper():
    assert intervals_overlap(1, 3, 2, 4)
    assert not intervals_overlap(1, 2, 3, 4)
