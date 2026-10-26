"""Phase 4M touch/rejection market backtester tests."""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
PHASE4D = TOOLS.parents[1] / "tools/phase4d_pool_landscape_backtester_v2"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(PHASE4D))

from market_engine import WatchSpec, run_market_backtest  # noqa: E402
from pool_zone_tracker import PoolAnchor  # noqa: E402
from strategy_rules import long_sl, plan_long, plan_short, short_sl  # noqa: E402
from touch_rejection_logic import (  # noqa: E402
    long_pool_touch,
    long_rejection_confirmed,
    short_pool_touch,
    short_rejection_confirmed,
)
from exit_simulator import simulate_exit  # noqa: E402


class AlwaysActiveLifecycle:
    pass


def zone_always_active(anchor, bar_utc, lifecycle):
    from pool_zone_tracker import ZoneStatus

    return ZoneStatus("ACTIVE", "TEST", "ok")


def zone_inactive_after_touch(anchor, bar_utc, lifecycle):
    from pool_zone_tracker import ZoneStatus

    if bar_utc >= "2026-01-01T00:15:00Z":
        return ZoneStatus("INACTIVE", "TEST", "lost")
    return ZoneStatus("ACTIVE", "TEST", "ok")


def anchor_from_signal(direction, pool_lower, pool_upper, pool_identity):
    origins = frozenset(pool_identity.split("|")) if pool_identity else frozenset()
    side = "upper" if direction == "SHORT" else "lower"
    return PoolAnchor(side=side, bottom=pool_lower, top=pool_upper, origins=origins)


CFG = {
    "pilot_from_utc": "2026-01-01T00:00:00Z",
    "pilot_to_utc": "2026-01-01T01:00:00Z",
    "sl_above_upper_pool_pct": 0.2,
    "sl_below_lower_pool_pct": 0.2,
    "min_tp_distance_pct": 0.5,
    "roundtrip_fees_pct": 0.11,
    "slippage_modeled": False,
}


def _short_watch(pool_l=0.099, pool_u=0.101):
    lowers = [
        {
            "side": "lower",
            "bottom_price": 0.095,
            "top_price": 0.096,
            "depth": 2,
            "origins": ["o1"],
        }
    ]
    return WatchSpec(
        watch_key="S1",
        candidate_id="S1",
        direction="SHORT",
        watch_from_utc="2026-01-01T00:00:00Z",
        pool_lower=pool_l,
        pool_upper=pool_u,
        pool_identity="o1",
        landscape_intervals=lowers,
        upper_tp_rows=[],
    )


def _long_watch(pool_l=0.09, pool_u=0.091):
    uppers = [
        {
            "side": "upper",
            "bottom_price": 0.10,
            "top_price": 0.101,
            "depth": 2,
            "origins": ["u1"],
        }
    ]
    return WatchSpec(
        watch_key="L1",
        candidate_id="L1",
        direction="LONG",
        watch_from_utc="2026-01-01T00:00:00Z",
        pool_lower=pool_l,
        pool_upper=pool_u,
        pool_identity="u1",
        landscape_intervals=[],
        upper_tp_rows=uppers,
    )


def test_upper_touch_no_immediate_short():
    bar = {"o": 0.099, "h": 0.1005, "l": 0.099, "c": 0.100}
    assert short_pool_touch(bar, 0.10)
    assert not short_rejection_confirmed(bar, 0.10)


def test_short_after_later_close_below():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.099, "h": 0.1005, "l": 0.099, "c": 0.100},
        "2026-01-01T00:05:00Z": {"o": 0.100, "h": 0.1002, "l": 0.099, "c": 0.0995},
        "2026-01-01T00:10:00Z": {"o": 0.0995, "h": 0.0998, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:15:00Z": {"o": 0.0985, "h": 0.099, "l": 0.098, "c": 0.098},
    }
    out = run_market_backtest(
        CFG,
        [_short_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    assert len(out["pool_touch_events"]) == 1
    assert len(out["rejection_confirmations"]) == 1
    assert out["rejection_confirmations"][0]["confirm_candle_utc"] == "2026-01-01T00:10:00Z"


def test_lower_touch_no_immediate_long():
    bar = {"o": 0.092, "h": 0.092, "l": 0.0905, "c": 0.091}
    assert long_pool_touch(bar, 0.091)
    assert not long_rejection_confirmed(bar, 0.091)


def test_no_confirm_on_touch_bar_same_index():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.099, "h": 0.1005, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:05:00Z": {"o": 0.0985, "h": 0.099, "l": 0.097, "c": 0.098},
    }
    out = run_market_backtest(
        CFG,
        [_short_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    assert out["rejection_confirmations"][0]["confirm_candle_utc"] == "2026-01-01T00:05:00Z"
    assert out["rejection_confirmations"][0]["touch_candle_utc"] == "2026-01-01T00:00:00Z"


def test_long_touch_and_confirm():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.092, "h": 0.092, "l": 0.0905, "c": 0.091},
        "2026-01-01T00:05:00Z": {"o": 0.091, "h": 0.0915, "l": 0.0905, "c": 0.091},
        "2026-01-01T00:10:00Z": {"o": 0.091, "h": 0.092, "l": 0.0905, "c": 0.0915},
        "2026-01-01T00:15:00Z": {"o": 0.0915, "h": 0.092, "l": 0.091, "c": 0.092},
    }
    out = run_market_backtest(
        CFG,
        [_long_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    assert any(e["direction"] == "LONG" for e in out["pool_touch_events"])
    assert any(r["direction"] == "LONG" for r in out["rejection_confirmations"])


def test_color_irrelevant_short_red_green():
    assert short_rejection_confirmed({"c": 0.09}, 0.10)


def test_close_exactly_on_edge_no_confirm():
    assert not short_rejection_confirmed({"c": 0.10}, 0.10)
    assert not long_rejection_confirmed({"c": 0.091}, 0.091)


def test_wait_inside_pool_multiple_bars():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.099, "h": 0.1005, "l": 0.099, "c": 0.100},
        "2026-01-01T00:05:00Z": {"o": 0.100, "h": 0.1005, "l": 0.0995, "c": 0.100},
        "2026-01-01T00:10:00Z": {"o": 0.100, "h": 0.1005, "l": 0.0995, "c": 0.100},
        "2026-01-01T00:15:00Z": {"o": 0.100, "h": 0.1005, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:20:00Z": {"o": 0.0985, "h": 0.099, "l": 0.098, "c": 0.098},
    }
    out = run_market_backtest(
        CFG,
        [_short_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    assert out["rejection_confirmations"][0]["confirm_candle_utc"] == "2026-01-01T00:15:00Z"


def test_pool_depth2_loss_aborts():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.099, "h": 0.1005, "l": 0.099, "c": 0.100},
        "2026-01-01T00:05:00Z": {"o": 0.100, "h": 0.1002, "l": 0.099, "c": 0.0995},
        "2026-01-01T00:10:00Z": {"o": 0.0995, "h": 0.0998, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:15:00Z": {"o": 0.0985, "h": 0.099, "l": 0.098, "c": 0.098},
    }

    def zone_loss_before_confirm(anchor, bar_utc, lifecycle):
        from pool_zone_tracker import ZoneStatus

        if bar_utc >= "2026-01-01T00:10:00Z":
            return ZoneStatus("INACTIVE", "TEST", "lost")
        return ZoneStatus("ACTIVE", "TEST", "ok")

    out = run_market_backtest(
        CFG,
        [_short_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_loss_before_confirm,
        anchor_from_signal,
    )
    assert out["rejection_confirmations"] == []
    assert any(a["reason"] == "ENTRY_POOL_DEPTH2_LOST" for a in out["pool_watch_aborts"])


def test_market_fill_next_open_not_confirm_bar():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.099, "h": 0.1005, "l": 0.099, "c": 0.100},
        "2026-01-01T00:05:00Z": {"o": 0.100, "h": 0.1002, "l": 0.099, "c": 0.0995},
        "2026-01-01T00:10:00Z": {"o": 0.0995, "h": 0.0998, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:15:00Z": {"o": 0.097, "h": 0.099, "l": 0.097, "c": 0.098},
    }
    out = run_market_backtest(
        CFG,
        [_short_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    trade = out["simulated_market_trades"][0]
    assert trade["fill_utc"] == "2026-01-01T00:15:00Z"
    assert trade["entry_price"] == 0.097


def test_sl_formulas():
    assert abs(short_sl(0.085982, 0.2) - 0.085982 * 1.002) < 1e-12
    assert abs(long_sl(0.08412, 0.2) - 0.08412 * 0.998) < 1e-12


def test_tp_min_distance_from_fill():
    lowers = [
        {"side": "lower", "bottom_price": 0.094, "top_price": 0.095, "depth": 2, "origins": []},
        {"side": "lower", "bottom_price": 0.088, "top_price": 0.089, "depth": 2, "origins": []},
    ]
    plan = plan_short(0.10, 0.101, lowers, 0.10, CFG, quality_gate=False)
    assert plan.tp_price is not None
    assert plan.tp_distance_pct is not None
    assert plan.tp_distance_pct >= 0.5


def test_max_one_position():
    """Two intents scheduled for the same fill bar → only one MARKET_FILL."""
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.099, "h": 0.1005, "l": 0.099, "c": 0.100},
        "2026-01-01T00:05:00Z": {"o": 0.100, "h": 0.1002, "l": 0.099, "c": 0.0995},
        "2026-01-01T00:10:00Z": {"o": 0.0995, "h": 0.0998, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:15:00Z": {"o": 0.097, "h": 0.099, "l": 0.097, "c": 0.098},
    }
    w1 = _short_watch()
    w2 = WatchSpec(
        watch_key="S2",
        candidate_id="S2",
        direction="SHORT",
        watch_from_utc="2026-01-01T00:00:00Z",
        pool_lower=0.099,
        pool_upper=0.101,
        pool_identity="o2",
        landscape_intervals=w1.landscape_intervals,
        upper_tp_rows=[],
    )
    out = run_market_backtest(
        CFG,
        [w1, w2],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    fills = [i for i in out["market_entry_intents"] if i.get("market_entry_intent") == "MARKET_FILL"]
    suppressed = [
        i
        for i in out["market_entry_intents"]
        if i.get("market_entry_intent") in ("SUPPRESSED_POSITION_OPEN", "SUPPRESSED_SLOT_BUSY")
    ]
    assert len(fills) == 1
    assert len(out["rejection_confirmations"]) == 2
    assert any(
        i.get("market_entry_intent") in ("SUPPRESSED_SLOT_BUSY", "SUPPRESSED_POSITION_OPEN")
        for i in out["market_entry_intents"]
    )


def test_simultaneous_opposing_signals():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.092, "h": 0.1005, "l": 0.0905, "c": 0.095},
        "2026-01-01T00:05:00Z": {"o": 0.095, "h": 0.1002, "l": 0.0905, "c": 0.0915},
        "2026-01-01T00:10:00Z": {"o": 0.0915, "h": 0.092, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:15:00Z": {"o": 0.0985, "h": 0.099, "l": 0.098, "c": 0.098},
    }
    out = run_market_backtest(
        CFG,
        [_short_watch(pool_l=0.099, pool_u=0.101), _long_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    assert any(
        i.get("lifecycle") == "SIMULTANEOUS_OPPOSING_MARKET_SIGNALS"
        for i in out["market_entry_intents"]
    )
    assert out["simulated_market_trades"] == []


def test_phase4d_engine_unchanged_import():
    from engine import run_backtest  # noqa: F401

    p4d = PHASE4D / "engine.py"
    assert p4d.is_file()


def test_no_validated_performance_flag():
    prices = {
        "2026-01-01T00:00:00Z": {"o": 0.099, "h": 0.1005, "l": 0.099, "c": 0.100},
        "2026-01-01T00:05:00Z": {"o": 0.100, "h": 0.1002, "l": 0.099, "c": 0.0995},
        "2026-01-01T00:10:00Z": {"o": 0.0995, "h": 0.0998, "l": 0.098, "c": 0.0985},
        "2026-01-01T00:15:00Z": {"o": 0.097, "h": 0.099, "l": 0.097, "c": 0.098},
    }
    out = run_market_backtest(
        CFG,
        [_short_watch()],
        prices,
        AlwaysActiveLifecycle(),
        plan_short,
        plan_long,
        simulate_exit,
        zone_always_active,
        anchor_from_signal,
    )
    assert out["simulated_market_trades"][0]["performance_validated"] is False
