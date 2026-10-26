"""Phase 4H OCO pending manager tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parents[1]
TOOLS4D = ROOT / "tools" / "phase4d_pool_landscape_backtester_v2"
sys.path.insert(0, str(TOOLS4D))
sys.path.insert(0, str(TOOLS))

from oco_engine import run_oco_backtest  # noqa: E402
from pool_zone_tracker import LifecycleIndex, PoolAnchor  # noqa: E402
from signal_provider import RawSignal  # noqa: E402

CFG = json.loads((TOOLS / "strategy_config_oco_v1.json").read_text(encoding="utf-8"))


def _zone_active(lifecycle: LifecycleIndex, bars: list[str], side: str, bot: float, top: float, origins: str) -> None:
    for bar in bars:
        lifecycle.by_bar.setdefault(bar, []).append(
            {
                "side": side,
                "event_type": "POOL_CONTINUED",
                "bottom_price": str(bot),
                "top_price": str(top),
                "depth": "2",
                "origins_json": json.dumps([origins]),
                "identity_confidence": "HIGH",
                "causal_as_of_utc": bar,
            }
        )


def _sig(cid: str, direction: str, active: str, lower: float, upper: float, origins: str) -> RawSignal:
    return RawSignal(
        candidate_id=cid,
        direction=direction,
        signal_close_utc=active,
        order_active_from_utc=active,
        signal_source="TEST",
        pool_lower=lower,
        pool_upper=upper,
        pool_identity=origins,
        landscape_intervals=[],
        upper_tp_rows=[],
        market_state_experimental="UNSET",
    )


def _run(sigs: list[RawSignal], prices: dict, lifecycle: LifecycleIndex | None = None) -> dict:
    cfg = {**CFG, "pilot_from_utc": min(prices), "pilot_to_utc": max(prices)}
    return run_oco_backtest(cfg, sigs, prices, lifecycle=lifecycle or LifecycleIndex())


def _events(out: dict, etype: str) -> list[dict]:
    return [e for e in out["oco_order_events"] if e.get("event_type") == etype]


def test_short_fill_cancels_long():
    lifecycle = LifecycleIndex()
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.105, "l": 0.101, "c": 0.1},
        "2026-09-11T12:05:00Z": {"o": 0.1, "h": 0.102, "l": 0.096, "c": 0.1},
    }
    _zone_active(lifecycle, list(prices), "upper", 0.1, 0.11, "o1")
    _zone_active(lifecycle, list(prices), "lower", 0.09, 0.095, "o2")
    short = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    long = _sig("L1", "LONG", "2026-09-11T12:00:00Z", 0.09, 0.095, "o2")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    long.upper_tp_rows = [
        {"bottom_price": 0.12, "top_price": 0.13, "depth": 2, "origins": ["y"]}
    ]
    out = _run([short, long], prices, lifecycle)
    assert _events(out, "ORDER_FILLED")
    assert any(e["direction"] == "SHORT" for e in _events(out, "ORDER_FILLED"))
    assert any(e["direction"] == "LONG" for e in _events(out, "OCO_CANCELLED"))


def test_long_fill_cancels_short():
    lifecycle = LifecycleIndex()
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.105, "l": 0.101, "c": 0.1},
        "2026-09-11T12:05:00Z": {"o": 0.096, "h": 0.096, "l": 0.094, "c": 0.096},
    }
    _zone_active(lifecycle, list(prices), "upper", 0.1, 0.11, "o1")
    _zone_active(lifecycle, list(prices), "lower", 0.09, 0.095, "o2")
    short = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    long = _sig("L1", "LONG", "2026-09-11T12:00:00Z", 0.09, 0.095, "o2")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    long.upper_tp_rows = [
        {"bottom_price": 0.12, "top_price": 0.13, "depth": 2, "origins": ["y"]}
    ]
    out = _run([short, long], prices, lifecycle)
    fills = _events(out, "ORDER_FILLED")
    assert any(e["direction"] == "LONG" for e in fills)
    assert any(e["direction"] == "SHORT" for e in _events(out, "OCO_CANCELLED"))


def test_upper_zone_loss_cancels_short_only():
    lifecycle = LifecycleIndex()
    bar = "2026-09-11T12:05:00Z"
    lifecycle.by_bar[bar] = [
        {
            "side": "upper",
            "event_type": "POOL_DISAPPEARED",
            "bottom_price": "0.1",
            "top_price": "0.11",
            "depth": "2",
            "origins_json": '["o1"]',
            "identity_confidence": "HIGH",
            "causal_as_of_utc": bar,
        }
    ]
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.105, "l": 0.101, "c": 0.1},
        bar: {"o": 0.1, "h": 0.105, "l": 0.101, "c": 0.1},
    }
    _zone_active(lifecycle, ["2026-09-11T12:00:00Z"], "upper", 0.1, 0.11, "o1")
    _zone_active(lifecycle, list(prices), "lower", 0.09, 0.095, "o2")
    short = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    long = _sig("L1", "LONG", "2026-09-11T12:00:00Z", 0.09, 0.095, "o2")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    long.upper_tp_rows = [{"bottom_price": 0.12, "top_price": 0.13, "depth": 2, "origins": ["y"]}]
    out = _run([short, long], prices, lifecycle)
    inv = _events(out, "POOL_ZONE_INVALIDATED")
    assert any(e["direction"] == "SHORT" for e in inv)
    assert not any(e.get("direction") == "LONG" for e in inv)


def test_lower_zone_loss_cancels_long_only():
    lifecycle = LifecycleIndex()
    bar = "2026-09-11T12:05:00Z"
    lifecycle.by_bar[bar] = [
        {
            "side": "lower",
            "event_type": "POOL_DISAPPEARED",
            "bottom_price": "0.09",
            "top_price": "0.095",
            "depth": "2",
            "origins_json": '["o2"]',
            "identity_confidence": "HIGH",
            "causal_as_of_utc": bar,
        }
    ]
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.105, "l": 0.101, "c": 0.1},
        bar: {"o": 0.1, "h": 0.094, "l": 0.092, "c": 0.1},
    }
    _zone_active(lifecycle, ["2026-09-11T12:00:00Z"], "upper", 0.1, 0.11, "o1")
    _zone_active(lifecycle, ["2026-09-11T12:00:00Z"], "lower", 0.09, 0.095, "o2")
    short = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    long = _sig("L1", "LONG", "2026-09-11T12:00:00Z", 0.09, 0.095, "o2")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    long.upper_tp_rows = [{"bottom_price": 0.12, "top_price": 0.13, "depth": 2, "origins": ["y"]}]
    out = _run([short, long], prices, lifecycle)
    inv = _events(out, "POOL_ZONE_INVALIDATED")
    assert any(e["direction"] == "LONG" for e in inv)
    assert not any(e.get("direction") == "SHORT" for e in inv)


def test_both_sides_same_bar_ambiguous():
    lifecycle = LifecycleIndex()
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.11, "l": 0.09, "c": 0.1},
        "2026-09-11T12:05:00Z": {"o": 0.1, "h": 0.11, "l": 0.089, "c": 0.1},
    }
    _zone_active(lifecycle, list(prices), "upper", 0.1, 0.11, "o1")
    _zone_active(lifecycle, list(prices), "lower", 0.09, 0.095, "o2")
    short = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    long = _sig("L1", "LONG", "2026-09-11T12:00:00Z", 0.09, 0.095, "o2")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    long.upper_tp_rows = [{"bottom_price": 0.12, "top_price": 0.13, "depth": 2, "origins": ["y"]}]
    out = _run([short, long], prices, lifecycle)
    assert out["oco_ambiguous_cases"]
    assert not _events(out, "ORDER_FILLED")


def test_single_position_after_fill():
    lifecycle = LifecycleIndex()
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.105, "l": 0.101, "c": 0.1},
        "2026-09-11T12:05:00Z": {"o": 0.1, "h": 0.102, "l": 0.096, "c": 0.1},
    }
    _zone_active(lifecycle, list(prices), "upper", 0.1, 0.11, "o1")
    _zone_active(lifecycle, list(prices), "lower", 0.09, 0.095, "o2")
    short = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    long = _sig("L1", "LONG", "2026-09-11T12:00:00Z", 0.09, 0.095, "o2")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    long.upper_tp_rows = [{"bottom_price": 0.12, "top_price": 0.13, "depth": 2, "origins": ["y"]}]
    out = _run([short, long], prices, lifecycle)
    opens = _events(out, "POSITION_OPEN")
    assert len(opens) == 1
    assert all(c["ok"] for c in out["oco_single_position_checks"])


def test_no_new_entry_while_position_open():
    lifecycle = LifecycleIndex()
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.11, "l": 0.09, "c": 0.1},
        "2026-09-11T12:05:00Z": {"o": 0.1, "h": 0.11, "l": 0.09, "c": 0.1},
        "2026-09-11T12:10:00Z": {"o": 0.1, "h": 0.11, "l": 0.09, "c": 0.1},
    }
    _zone_active(lifecycle, list(prices), "upper", 0.1, 0.11, "o1")
    s1 = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    s1.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    s2 = _sig("S2", "SHORT", "2026-09-11T12:10:00Z", 0.1, 0.11, "o3")
    s2.landscape_intervals = s1.landscape_intervals
    out = _run([s1, s2], prices, lifecycle)
    blocked = [
        e
        for e in out["oco_order_events"]
        if e.get("rule_id") == "EXEC-POSITION-BLOCK-01"
    ]
    assert blocked


def test_no_retroactive_fill():
    lifecycle = LifecycleIndex()
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.12, "h": 0.12, "l": 0.12, "c": 0.12},
        "2026-09-11T12:05:00Z": {"o": 0.1, "h": 0.101, "l": 0.099, "c": 0.1},
    }
    _zone_active(lifecycle, ["2026-09-11T12:05:00Z"], "upper", 0.1, 0.11, "o1")
    short = _sig("S1", "SHORT", "2026-09-11T12:05:00Z", 0.1, 0.11, "o1")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    out = _run([short], prices, lifecycle)
    fills = _events(out, "ORDER_FILLED")
    if fills:
        assert fills[0]["event_utc"] >= "2026-09-11T12:05:00Z"


def test_no_time_timeout():
    lifecycle = LifecycleIndex()
    prices = {f"2026-09-11T12:{i:02d}:00Z": {"o": 0.12, "h": 0.12, "l": 0.12, "c": 0.12} for i in range(0, 30)}
    _zone_active(lifecycle, list(prices), "upper", 0.1, 0.11, "o1")
    short = _sig("S1", "SHORT", "2026-09-11T12:00:00Z", 0.1, 0.11, "o1")
    short.landscape_intervals = [
        {"side": "lower", "bottom_price": 0.05, "top_price": 0.06, "depth": 2, "origins": ["x"]}
    ]
    cfg = {**CFG, "pilot_from_utc": "2026-09-11T12:00:00Z", "pilot_to_utc": "2026-09-11T12:29:00Z"}
    out = run_oco_backtest(cfg, [short], prices, lifecycle=lifecycle)
    assert not any("EXPIRED" in str(e) for e in out["oco_order_events"])


def test_long_short_mirror_sl():
    lifecycle = LifecycleIndex()
    prices = {
        "2026-09-11T12:00:00Z": {"o": 0.1, "h": 0.11, "l": 0.09, "c": 0.1},
        "2026-09-11T12:05:00Z": {"o": 0.096, "h": 0.097, "l": 0.094, "c": 0.096},
    }
    _zone_active(lifecycle, list(prices), "lower", 0.09, 0.1, "o2")
    long = _sig("L1", "LONG", "2026-09-11T12:00:00Z", 0.09, 0.1, "o2")
    long.upper_tp_rows = [{"bottom_price": 0.12, "top_price": 0.13, "depth": 2, "origins": ["y"]}]
    out = _run([long], prices, lifecycle)
    pos = _events(out, "POSITION_OPEN")
    assert pos
    assert "sl=" in pos[0]["note"] and "tp=" in pos[0]["note"]
