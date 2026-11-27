"""Synthetic trigger safety tests for price_alert_monitor logic."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import price_alert_monitor as mon
from price_alert_store import alert_key, crossed_level


def _reset_monitor_globals() -> None:
    mon._last_price_by_symbol.clear()
    mon._cross_reference.clear()
    mon._initialized_alert_keys.clear()
    mon._firing_keys.clear()
    mon._fanout_available = None
    mon._fanout_last_event_at.clear()


def _sample_state(sym: str, target: float, trigger: str, ref: float) -> dict:
    key = alert_key(sym, target, trigger)
    return {
        key: {
            "symbol": sym,
            "target_price": target,
            "trigger": trigger,
            "engine": "monitor_v1",
            "reference_price": ref,
            "started_at_unix": 1_700_000_000,
        }
    }


def test_crossed_from_below_above():
    assert crossed_level(99.0, 101.0, 100.0, "above")
    assert not crossed_level(100.5, 101.0, 100.0, "above")


def test_crossed_from_above_below():
    assert crossed_level(101.0, 99.0, 100.0, "below")


def test_spike_through_level():
    assert crossed_level(98.0, 102.0, 100.0, "above")


def test_touch_and_revert_needs_fanout_event():
    """Simulated tick: touch at 100 then revert — second event must not re-fire."""
    _reset_monitor_globals()
    sym = "TESTUSDT"
    state = _sample_state(sym, 100.0, "above", 98.0)
    mon._sync_alert_reference_seeds()
    fired = []

    def fake_fire(key, entry, price, source):
        fired.append((key, price, source))

    with patch.object(mon, "load_state", side_effect=lambda: dict(state)):
        with patch.object(mon, "_fire_alert", side_effect=fake_fire):
            mon._check_symbol_prices(sym, [100.0], source="t")
            assert len(fired) == 1
            mon._check_symbol_prices(sym, [99.5], source="t")
            assert len(fired) == 1


def test_no_retrospective_forming_high():
    """Reference seed 98; partial bar high 100.5 but close 99 — must not fire above 100."""
    _reset_monitor_globals()
    sym = "QNTUSDT"
    state = _sample_state(sym, 100.0, "above", 98.0)
    with patch.object(mon, "load_state", return_value=state):
        mon._sync_alert_reference_seeds()
    fired = []
    with patch.object(mon, "load_state", return_value=state):
        with patch.object(mon, "_fire_alert", side_effect=lambda *a: fired.append(a)):
            mon._check_symbol_prices(sym, [99.0], source="collector_forming")
    assert fired == []


def test_multiple_alerts_same_symbol():
    _reset_monitor_globals()
    sym = "AAVEUSDT"
    state = {
        alert_key(sym, 100.0, "above"): {
            "symbol": sym,
            "target_price": 100.0,
            "trigger": "above",
            "engine": "monitor_v1",
            "reference_price": 98.0,
        },
        alert_key(sym, 105.0, "above"): {
            "symbol": sym,
            "target_price": 105.0,
            "trigger": "above",
            "engine": "monitor_v1",
            "reference_price": 98.0,
        },
    }
    with patch.object(mon, "load_state", return_value=state):
        mon._sync_alert_reference_seeds()
    keys_fired = []

    def fake_fire(key, entry, price, source):
        keys_fired.append(key)
        state.pop(key, None)

    with patch.object(mon, "load_state", return_value=state):
        with patch.object(mon, "_fire_alert", side_effect=fake_fire):
            mon._check_symbol_prices(sym, [106.0], source="t")
    assert len(keys_fired) == 2


def test_duplicate_fire_guard():
    _reset_monitor_globals()
    sym = "SOLUSDT"
    key = alert_key(sym, 50.0, "above")
    state = _sample_state(sym, 50.0, "above", 48.0)
    notify = {"count": 0}

    def fake_append(**kwargs):
        notify["count"] += 1
        return kwargs

    with patch.object(mon, "load_state", return_value=dict(state)):
        with patch.object(mon, "save_state", MagicMock()):
            with patch.object(mon, "append_fired_event", side_effect=fake_append):
                with patch.object(mon, "send_ntfy_alert", return_value=True):
                    with patch.object(mon, "play_alert_sound", return_value=True):
                        mon._fire_alert(key, state[key], 50.0, "t")
                        mon._fire_alert(key, state[key], 50.0, "t")
    assert notify["count"] == 1


def test_forming_fallback_after_reference_seed():
    """Reference anchor set — live forming close must still update and trigger."""
    _reset_monitor_globals()
    sym = "DOGEUSDT"
    state = _sample_state(sym, 100.0, "above", 98.0)
    with patch.object(mon, "load_state", return_value=state):
        mon._sync_alert_reference_seeds()
    mon._fanout_available = False
    fired = []
    with patch.object(mon, "load_state", return_value=state):
        with patch.object(mon, "_fire_alert", side_effect=lambda *a: fired.append(a)):
            mon._check_symbol_prices(sym, [100.0], source="collector_forming")
    assert len(fired) == 1


def test_xau_uses_8788_base():
    assert mon._collector_base_for_symbol("XAUUSDT").endswith(":8788")


def test_should_use_forming_when_fanout_unavailable():
    mon._fanout_available = False
    assert mon._should_use_forming_fallback("DOGEUSDT", time.time()) is True
    mon._fanout_available = True
    assert mon._should_use_forming_fallback("QNTUSDT", time.time()) is True


def test_legacy_engine_ignored():
    _reset_monitor_globals()
    sym = "XRPUSDT"
    key = alert_key(sym, 1.0, "above")
    state = {
        key: {
            "symbol": sym,
            "target_price": 1.0,
            "trigger": "above",
            "engine": "legacy_subprocess",
            "reference_price": 0.9,
        }
    }
    fired = []
    with patch.object(mon, "load_state", return_value=state):
        with patch.object(mon, "_fire_alert", side_effect=lambda *a: fired.append(a)):
            mon._check_symbol_prices(sym, [1.1], source="t")
    assert fired == []