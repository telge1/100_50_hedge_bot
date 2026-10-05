"""Registry core tests."""

from __future__ import annotations

from datetime import datetime, timezone

from bot.shadow_signal_registry.models import make_signal_id
from bot.shadow_signal_registry.outcome_tracker import TrackedShadow, process_long_bar, process_short_bar
from bot.shadow_signal_registry.registry import ShadowRegistry
from bot.shadow_signal_registry.short_product import consolidate_short_product
from bot.shadow_signal_registry.store import RegistryStore


def test_deterministic_signal_id() -> None:
    a = make_signal_id("long", "s", "v1", "X", "2026-01-01T00:00:00+00:00", "pool1")
    b = make_signal_id("long", "s", "v1", "X", "2026-01-01T00:00:00+00:00", "pool1")
    assert a == b


def test_duplicate_prevention_long(tmp_path, monkeypatch) -> None:
    import bot.shadow_signal_registry.store as store_mod

    monkeypatch.setattr(store_mod, "RUNTIME", tmp_path)
    reg = ShadowRegistry("long")
    sig = {
        "symbol": "TUTUSDT",
        "decision_time": "2026-10-04T18:30:00+00:00",
        "pool_id": "p1",
        "entry_price": 1.0,
        "stop": 0.9,
        "tp": 1.1,
        "ladder_pass": True,
        "m15_lower_2_age_h": 1.0,
    }
    assert reg.register_long_geometry(sig) is not None
    assert reg.register_long_geometry(sig) is None


def test_short_floor_guard_product() -> None:
    rows = [
        {"variant": "no_guard", "pool_id": "p", "symbol": "X", "signal_status": "BASELINE_NO_GUARD"},
        {
            "variant": "floor_blocked",
            "pool_id": "p",
            "symbol": "X",
            "decision_time": "t",
            "entry_price": 1,
            "stop": 2,
            "tp": 0.5,
            "block_reason": "floor",
        },
    ]
    p = consolidate_short_product(rows)
    assert p and p["block_reason"] == "FLOOR_GUARD" and p["hypothetical"]


def test_short_e1r_blocked_product() -> None:
    from bot.shadow_signal_registry.models import BLOCK_REASON_NONE
    rows = [
        {"variant": "no_guard", "pool_id": "p", "symbol": "X"},
        {
            "variant": "with_guard",
            "pool_id": "p",
            "symbol": "X",
            "decision_time": "t",
            "entry_price": 1,
            "stop": 2,
            "tp": 0.5,
            "signal_status": "BLOCKED",
            "e1r_state": "ACTIVE",
            "block_reason": "ACTIVE",
        },
    ]
    p = consolidate_short_product(rows)
    assert p and p["block_reason"] == "E1R_ACTIVE"


def test_no_guard_only_returns_none() -> None:
    rows = [{"variant": "no_guard", "pool_id": "p", "symbol": "X", "signal_status": "BASELINE_NO_GUARD"}]
    assert consolidate_short_product(rows) is None


def test_long_sl_outcome() -> None:
    t = TrackedShadow(
        signal_id="x",
        symbol="X",
        side="long",
        decision_time="2026-01-01T00:00:00+00:00",
        entry_price=100.0,
        initial_sl=99.0,
        active_sl=99.0,
        tp=110.0,
        hypothetical=False,
    )
    bar = {"open": 100, "high": 101, "low": 98, "close": 99, "close_time": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)}
    closed, be = process_long_bar(t, bar)
    assert closed and t.outcome == "SL"


def test_short_tp_outcome() -> None:
    t = TrackedShadow(
        signal_id="x",
        symbol="X",
        side="short",
        decision_time="2026-01-01T00:00:00+00:00",
        entry_price=100.0,
        initial_sl=102.0,
        active_sl=102.0,
        tp=95.0,
        hypothetical=False,
    )
    bar = {"open": 100, "high": 101, "low": 94, "close": 96, "close_time": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)}
    assert process_short_bar(t, bar)
    assert t.outcome == "TP"


def test_mae_mfe_short() -> None:
    t = TrackedShadow(
        signal_id="x",
        symbol="X",
        side="short",
        decision_time="2026-01-01T00:00:00+00:00",
        entry_price=100.0,
        initial_sl=105.0,
        active_sl=105.0,
        tp=90.0,
        hypothetical=True,
    )
    bar = {"open": 100, "high": 103, "low": 97, "close": 101, "close_time": datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)}
    process_short_bar(t, bar)
    assert t.mae_pct > 0 and t.mfe_pct > 0


def test_no_orders_in_registry_package() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    for p in root.rglob("*.py"):
        if "tests" in p.parts:
            continue
        assert "create_order" not in p.read_text().lower()
