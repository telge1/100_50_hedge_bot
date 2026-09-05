"""Focused tests for L2 wall-to-wall strategy discovery V1."""

from __future__ import annotations

from orderbook_analyse.l2_wall_to_wall_discovery import COST_BPS
from orderbook_analyse.l2_wall_to_wall_discovery.exits import compute_path_and_exits
from orderbook_analyse.l2_wall_to_wall_discovery.models import (
    sample_index,
    side_adjusted_return_bps,
    trade_side_for_module,
)
from orderbook_analyse.l2_wall_to_wall_discovery.outcomes import compute_horizon_outcomes, cost_summary, match_controls
from orderbook_analyse.l2_wall_to_wall_discovery.signals import detect_breakout_signals, detect_reclaim_signals
from orderbook_analyse.l2_wall_to_wall_discovery.targets import select_target_wall, track_target
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow


def _sample(
    ts_ms: int,
    mid: float,
    *,
    bid_wall: float | None = None,
    bid_qty: float | None = None,
    ask_wall: float | None = None,
    ask_qty: float | None = None,
    bid_far: float | None = None,
    bid_far_qty: float | None = None,
    ask_far: float | None = None,
    ask_far_qty: float | None = None,
    symbol: str = "BTCUSDT",
) -> SampleRow:
    bb = mid - 0.05
    ba = mid + 0.05
    return SampleRow(
        symbol=symbol,
        ts_ms=ts_ms,
        best_bid=bb,
        best_ask=ba,
        mid=mid,
        spread=0.1,
        spread_bps=1.0,
        microprice=mid,
        bid_levels=200,
        ask_levels=200,
        bid_qty_l10=1.0,
        ask_qty_l10=1.0,
        imbalance_l10=0.0,
        bid_qty_bps10=1.0,
        ask_qty_bps10=1.0,
        imbalance_bps10=0.0,
        bid_wall_price=bid_wall,
        bid_wall_qty=bid_qty,
        ask_wall_price=ask_wall,
        ask_wall_qty=ask_qty,
        source_file="test",
        warmup=False,
        bid_far_wall_price=bid_far,
        bid_far_wall_qty=bid_far_qty,
        ask_far_wall_price=ask_far,
        ask_far_wall_qty=ask_far_qty,
    )


def _path(mids: list[tuple[int, float]], **kw) -> list[SampleRow]:
    return [_sample(t, m, **kw) for t, m in mids]


def test_trade_side_mapping() -> None:
    assert trade_side_for_module("WALL_HOLD_RECLAIM", "BID") == "LONG"
    assert trade_side_for_module("WALL_HOLD_RECLAIM", "ASK") == "SHORT"
    assert trade_side_for_module("WALL_REMOVED_BREAK", "BID") == "SHORT"
    assert trade_side_for_module("WALL_REMOVED_BREAK", "ASK") == "LONG"


def test_bid_reclaim_long_entry_after_confirm() -> None:
    wall = 100.0
    # contact at 0, break below, reclaim cross at 2000, hold 3s
    samples = _path(
        [
            (0, 100.0),
            (250, 99.95),
            (500, 99.9),
            (1000, 99.85),
            (2000, 100.05),  # reclaim cross
            *[(2000 + i * 250, 100.1) for i in range(1, 20)],
        ],
        ask_wall=100.5,
        ask_qty=20.0,
        bid_wall=100.0,
        bid_qty=15.0,
    )
    ts = sample_index(samples)
    ep = {
        "attack_id": "a1",
        "symbol": "BTCUSDT",
        "side": "BID",
        "first_contact_at": 0,
        "wall_price_at_contact": wall,
        "lifecycle_id": "lc1",
    }
    sigs = detect_reclaim_signals(ep, samples, ts, None)
    variants = {s["variant"] for s in sigs}
    assert "R1_CROSS" in variants
    assert "R2_HOLD_1S" in variants
    assert "R3_HOLD_3S" in variants
    r3 = next(s for s in sigs if s["variant"] == "R3_HOLD_3S")
    assert r3["position_side"] == "LONG"
    assert r3["entry_at"] > r3["confirmed_at"]
    assert r3["confirmed_at"] >= 5000  # cross 2000 + 3000


def test_ask_reclaim_short() -> None:
    samples = _path(
        [
            (0, 100.0),
            (500, 100.1),
            (1000, 100.2),
            (2000, 99.95),  # reclaim below ask wall 100
            *[(2000 + i * 250, 99.9) for i in range(1, 16)],
        ],
        ask_wall=100.0,
        ask_qty=10.0,
    )
    ts = sample_index(samples)
    ep = {
        "attack_id": "a2",
        "symbol": "BTCUSDT",
        "side": "ASK",
        "first_contact_at": 0,
        "wall_price_at_contact": 100.0,
        "lifecycle_id": "lc2",
    }
    sigs = detect_reclaim_signals(ep, samples, ts, None)
    assert any(s["position_side"] == "SHORT" for s in sigs)
    assert all(s["entry_at"] > s["confirmed_at"] for s in sigs)


def test_bid_break_short_hold() -> None:
    samples = _path(
        [
            (0, 100.0),
            (250, 99.95),
            *[(500 + i * 250, 99.8) for i in range(0, 20)],
        ],
        bid_wall=100.0,
        bid_qty=1.0,
    )
    ts = sample_index(samples)
    ep = {
        "attack_id": "b1",
        "symbol": "BTCUSDT",
        "side": "BID",
        "first_contact_at": 0,
        "wall_price_at_contact": 100.0,
        "lifecycle_id": "lc3",
    }
    sigs = detect_breakout_signals(ep, samples, ts, {"pull_proxy": True, "depletion_ratio": 0.8})
    variants = {s["variant"] for s in sigs}
    assert "B1_HOLD_1S" in variants
    assert "B2_HOLD_3S" in variants
    assert "B5_WALL_REMOVED_CONFIRM" in variants
    b2 = next(s for s in sigs if s["variant"] == "B2_HOLD_3S")
    assert b2["position_side"] == "SHORT"
    assert b2["entry_at"] > b2["confirmed_at"]


def test_ask_break_long() -> None:
    samples = _path([(0, 100.0), *[(250 + i * 250, 100.2) for i in range(0, 20)]], ask_wall=100.0, ask_qty=1.0)
    ts = sample_index(samples)
    ep = {
        "attack_id": "b2",
        "symbol": "BTCUSDT",
        "side": "ASK",
        "first_contact_at": 0,
        "wall_price_at_contact": 100.0,
        "lifecycle_id": "lc4",
    }
    sigs = detect_breakout_signals(ep, samples, ts, None)
    assert any(s["position_side"] == "LONG" and s["variant"].startswith("B") for s in sigs)


def test_reclaim_retest_and_break_retest() -> None:
    # reclaim retest: cross, approach wall, hold reclaim side
    samples = _path(
        [
            (0, 99.9),
            (1000, 100.05),  # cross
            (2000, 100.01),  # retest near
            (3000, 100.2),  # hold reclaim
            *[(3000 + i * 250, 100.25) for i in range(1, 10)],
        ]
    )
    ts = sample_index(samples)
    ep = {
        "attack_id": "rt1",
        "symbol": "BTCUSDT",
        "side": "BID",
        "first_contact_at": 0,
        "wall_price_at_contact": 100.0,
        "lifecycle_id": "lc5",
    }
    sigs = detect_reclaim_signals(ep, samples, ts, None)
    r4 = [s for s in sigs if s["variant"] == "R4_RETEST_HOLD"]
    assert r4
    assert r4[0]["confirmed_at"] >= 3000

    # break retest fail
    samples2 = _path(
        [
            (0, 100.0),
            (250, 99.9),
            (1000, 99.99),  # near wall from below
            (2000, 99.8),  # fail reclaim
            *[(2000 + i * 250, 99.7) for i in range(1, 10)],
        ]
    )
    ts2 = sample_index(samples2)
    ep2 = {
        "attack_id": "rt2",
        "symbol": "BTCUSDT",
        "side": "BID",
        "first_contact_at": 0,
        "wall_price_at_contact": 100.0,
        "lifecycle_id": "lc6",
    }
    bsigs = detect_breakout_signals(ep2, samples2, ts2, None)
    assert any(s["variant"] == "B4_RETEST_FAIL" for s in bsigs)


def test_target_visible_and_direction() -> None:
    samples = _path(
        [(i * 250, 100.0 + i * 0.01) for i in range(0, 40)],
        ask_wall=100.8,
        ask_qty=12.0,
        bid_wall=99.2,
        bid_qty=8.0,
        ask_far=100.8,
        ask_far_qty=12.0,
        bid_far=99.2,
        bid_far_qty=8.0,
    )
    ts = sample_index(samples)
    entry = {
        "signal_id": "s1",
        "attack_id": "a",
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "entry_at": 1000,
        "entry_mid": 100.05,
        "wall_price": 100.0,
        "module": "WALL_HOLD_RECLAIM",
        "variant": "R2_HOLD_1S",
    }
    lifecycles = [
        {
            "lifecycle_id": "ask_far",
            "symbol": "BTCUSDT",
            "side": "ASK",
            "wall_price": 100.8,
            "peak_qty": 12,
            "appear_ts": 0,
            "end_ts": 100_000,
        },
        {
            "lifecycle_id": "ask_future",
            "symbol": "BTCUSDT",
            "side": "ASK",
            "wall_price": 100.2,
            "peak_qty": 9,
            "appear_ts": 5000,  # after entry — must not pick
            "end_ts": 100_000,
        },
    ]
    tgt = select_target_wall(entry, samples=samples, ts_index=ts, lifecycles=lifecycles)
    assert tgt["target_visible_at_entry"] is True
    assert tgt["target_wall_id"] == "ask_far"
    assert tgt["target_price_at_entry"] == 100.8
    assert tgt["no_target_wall"] is False

    # short looks for bid below
    entry_s = {**entry, "position_side": "SHORT", "signal_id": "s2"}
    lifecycles_b = [
        {
            "lifecycle_id": "bid_below",
            "symbol": "BTCUSDT",
            "side": "BID",
            "wall_price": 99.2,
            "peak_qty": 8,
            "appear_ts": 0,
            "end_ts": 100_000,
        }
    ]
    tgt2 = select_target_wall(entry_s, samples=samples, ts_index=ts, lifecycles=lifecycles_b)
    assert tgt2["target_side"] == "BID"
    assert tgt2["target_price_at_entry"] == 99.2


def test_target_pull_defense_break_reclaim() -> None:
    # pull before reach
    samples = [
        _sample(0, 100.0, ask_wall=100.5, ask_qty=10),
        _sample(250, 100.1, ask_wall=None, ask_qty=None),
        _sample(500, 100.15, ask_wall=None, ask_qty=None),
    ]
    entry = {
        "signal_id": "p1",
        "attack_id": "a",
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "entry_at": 0,
        "entry_mid": 100.0,
        "wall_price": 99.9,
        "module": "WALL_HOLD_RECLAIM",
        "variant": "R1_CROSS",
    }
    tgt = {
        "no_target_wall": False,
        "target_price_at_entry": 100.5,
        "target_side": "ASK",
        "target_size_at_entry": 10.0,
        "signal_id": "p1",
    }
    tl, res = track_target(entry, tgt, samples, sample_index(samples))
    assert res["target_end_state"] == "TARGET_PULLED_BEFORE_REACH"

    # reach then defend
    samples2 = _path(
        [
            (0, 100.0),
            (1000, 100.5),
            (2000, 100.49),
            (3000, 100.48),
        ],
        ask_wall=100.5,
        ask_qty=10.0,
    )
    tgt2 = {**tgt, "signal_id": "p2"}
    entry2 = {**entry, "signal_id": "p2"}
    _, res2 = track_target(entry2, tgt2, samples2, sample_index(samples2), max_horizon_ms=10_000)
    assert res2["target_reached"] is True
    assert res2["target_end_state"] in {"TARGET_DEFENDED", "TARGET_REACHED"}

    # break then reclaim against long (mid goes through then back)
    samples3 = _path(
        [
            (0, 100.0),
            (1000, 100.5),
            (2000, 100.6),  # break through ask
            (3000, 100.55),
            (4000, 100.5),  # reclaim
        ],
        ask_wall=100.5,
        ask_qty=5.0,
    )
    _, res3 = track_target({**entry, "signal_id": "p3"}, {**tgt, "signal_id": "p3"}, samples3, sample_index(samples3))
    assert res3["target_broken"] is True or res3["target_reached"] is True


def test_exits_mfe_mae_costs_horizons() -> None:
    # ~6 minutes of samples so 5m horizon is complete
    samples = _path([(i * 250, 100.0 + min(i, 40) * 0.02) for i in range(0, 1500)], ask_wall=100.4, ask_qty=10)
    ts = sample_index(samples)
    entry = {
        "signal_id": "e1",
        "attack_id": "a",
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "entry_at": 0,
        "entry_mid": 100.0,
        "wall_price": 99.9,
        "module": "WALL_HOLD_RECLAIM",
        "variant": "R2_HOLD_1S",
    }
    tgt = {
        "no_target_wall": False,
        "target_price_at_entry": 100.4,
        "target_side": "ASK",
        "target_size_at_entry": 10.0,
        "signal_id": "e1",
        "target_visible_at_entry": True,
    }
    tl, tres = track_target(entry, tgt, samples, ts)
    path_row, exits = compute_path_and_exits(entry, tgt, tres, tl, samples, ts)
    assert path_row["mfe_bps_before_target"] is not None
    assert path_row["mae_bps_before_target"] is not None
    variants = {e["exit_variant"] for e in exits}
    assert "E1_TARGET_FIRST_TOUCH" in variants
    assert "E2_FRONT_RUN_TARGET" in variants
    assert "E6_INVALIDATION_EXIT" in variants
    assert side_adjusted_return_bps(100.0, 101.0, "LONG") == 100.0
    assert side_adjusted_return_bps(100.0, 101.0, "SHORT") == -100.0

    outs = compute_horizon_outcomes(
        [entry],
        {"BTCUSDT": samples},
        {"BTCUSDT": ts},
        data_end_ms=samples[-1].ts_ms,
    )
    complete = [o for o in outs if o["outcome_complete"]]
    assert any(o["horizon_s"] == 300 for o in complete)
    # 4h incomplete if path shorter
    incomplete = [o for o in outs if not o["outcome_complete"]]
    assert any(o["horizon_s"] == 14400 for o in incomplete)

    for ex in exits:
        ex["module"] = "WALL_HOLD_RECLAIM"
        ex["position_side"] = "LONG"
    costs = cost_summary(outs, exits)
    assert {c["cost_bps"] for c in costs if c["source"] == "horizon"} >= set(COST_BPS)


def test_controls_no_overlap_and_deterministic() -> None:
    samples = _path([(i * 1000, 100.0) for i in range(0, 500)])
    entries = [
        {
            "signal_id": "s1",
            "symbol": "BTCUSDT",
            "position_side": "LONG",
            "module": "WALL_HOLD_RECLAIM",
            "entry_at": 100_000,
            "entry_mid": 100.0,
            "variant": "R1_CROSS",
        }
    ]
    c1, _ = match_controls(entries, {"BTCUSDT": samples}, seed=42, per_event=2)
    c2, _ = match_controls(entries, {"BTCUSDT": samples}, seed=42, per_event=2)
    assert [x["entry_at"] for x in c1] == [x["entry_at"] for x in c2]
    for c in c1:
        assert not (70_000 <= c["entry_at"] <= 220_000)


def test_no_ex_post_label_in_signal() -> None:
    samples = _path([(0, 99.9), (1000, 100.1), *[(1000 + i * 250, 100.15) for i in range(1, 20)]])
    ts = sample_index(samples)
    ep = {
        "attack_id": "x",
        "symbol": "BTCUSDT",
        "side": "BID",
        "first_contact_at": 0,
        "wall_price_at_contact": 100.0,
        "lifecycle_id": "lc",
        "resolution_class": "DEFENDED",  # must not drive detection
    }
    sigs = detect_reclaim_signals(ep, samples, ts, None)
    assert all("resolution_class" not in s for s in sigs)
    assert all(s.get("semantic_role") == "causal_feature" for s in sigs)


def test_migration_no_lookahead_target() -> None:
    samples = [
        _sample(0, 100.0, ask_wall=100.3, ask_qty=10),
        _sample(1000, 100.1, ask_wall=100.31, ask_qty=10),
        _sample(2000, 100.2, ask_wall=100.32, ask_qty=11),
        _sample(5000, 100.3, ask_wall=100.33, ask_qty=11),
    ]
    entry = {
        "signal_id": "m1",
        "attack_id": "a",
        "symbol": "BTCUSDT",
        "position_side": "LONG",
        "entry_at": 0,
        "entry_mid": 100.0,
        "wall_price": 99.8,
        "module": "WALL_HOLD_RECLAIM",
        "variant": "R1_CROSS",
    }
    tgt = {
        "no_target_wall": False,
        "target_price_at_entry": 100.3,
        "target_side": "ASK",
        "target_size_at_entry": 10.0,
        "signal_id": "m1",
    }
    tl, res = track_target(entry, tgt, samples, sample_index(samples))
    assert any(t["state"] == "TARGET_MIGRATING" for t in tl) or res["target_reached"]
