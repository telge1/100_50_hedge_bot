"""Tick-normalized amend diff + REST verify (mocked)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from research.manual_long_box_live_sync_v1.order_settings.order_amend import (
    build_amend_diff,
    execute_confirmed_amend,
)
from research.manual_long_box_live_sync_v1.order_settings.price_tick import (
    prices_equal_tick,
    round_price_to_tick,
)
from research.manual_long_box_live_sync_v1.order_settings.reconcile import (
    ConfirmedOrder,
    wait_for_amend_fields,
)
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, StateStore

TICK = 0.00001
CURRENT = {"entry_price": 0.06751, "stop_loss": 0.06731, "take_profit": 0.0677}
INCIDENT_BYBIT = {"entry_price": 0.06725, "stop_loss": 0.06702, "take_profit": 0.06759}
INCIDENT_CHART = {
    "entry_price": 0.06724612496871611,
    "stop_price": 0.06688178397691248,
    "target_price": 0.06759049803954284,
}


def test_incident_04f4_only_sl_changed_with_tick():
    from research.manual_long_box_live_sync_v1.order_settings.price_tick import normalize_box_prices

    norm = normalize_box_prices(
        {
            "entry_price": INCIDENT_CHART["entry_price"],
            "stop_price": INCIDENT_CHART["stop_price"],
            "target_price": INCIDENT_CHART["target_price"],
        },
        TICK,
    )
    ch = build_amend_diff(
        current=INCIDENT_BYBIT,
        proposed={
            "entry_price": norm["entry_price"],
            "stop_price": norm["stop_price"],
            "target_price": norm["target_price"],
        },
        tick_size=TICK,
    )
    assert ch == {"entry_price": False, "stop_loss": True, "take_profit": False}
    assert norm["stop_price"] == pytest.approx(0.06688)


def test_incident_04f4_tick_none_blocks_prepare_normalization():
    from research.manual_long_box_live_sync_v1.order_settings.price_tick import normalize_box_prices

    with pytest.raises(ValueError, match="tick_size_required"):
        normalize_box_prices(
            {
                "entry_price": INCIDENT_CHART["entry_price"],
                "stop_price": INCIDENT_CHART["stop_price"],
                "target_price": INCIDENT_CHART["target_price"],
            },
            None,
        )


def test_regression_24293b_snapshot_only_tp_changed():
    from research.manual_long_box_live_sync_v1.order_settings.price_tick import build_amend_price_snapshot

    snap = build_amend_price_snapshot(
        chart_raw={
            "entry_price": 0.06724668914127521,
            "stop_price": 0.06690378946461159,
            "target_price": 0.06751235582960388,
        },
        exchange_current={
            "entry_price": 0.06725,
            "stop_loss": 0.06690,
            "take_profit": 0.06758,
        },
        tick_size=0.00001,
        tick_source="test",
    )
    assert snap["changes"] == {
        "entry_price": False,
        "stop_loss": False,
        "take_profit": True,
    }
    assert snap["normalized_targets"]["take_profit"] == pytest.approx(0.06751)
    assert snap["normalized_targets"]["entry_price"] == pytest.approx(0.06725)
    assert snap["normalized_targets"]["stop_loss"] == pytest.approx(0.06690)


def test_public_tick_fallback_doge():
    from research.manual_long_box_live_sync_v1.order_settings.price_tick import fetch_tick_size_public

    ts = fetch_tick_size_public("DOGEUSDT")
    assert ts == pytest.approx(0.00001)


def test_a_entry_unchanged_after_tick():
    assert prices_equal_tick(0.06751, 0.06750868403847968, TICK)


def test_b_tp_unchanged_after_tick():
    assert prices_equal_tick(0.0677, 0.0677037372956301, TICK)


def test_c_sl_changed_after_tick():
    assert not prices_equal_tick(0.06731, 0.06724344560241169, TICK)
    assert round_price_to_tick(0.06724344560241169, TICK) == pytest.approx(0.06724)


def test_d_only_sl_in_diff():
    ch = build_amend_diff(
        current=CURRENT,
        proposed={
            "entry_price": 0.06750868403847968,
            "stop_price": 0.06724344560241169,
            "target_price": 0.0677037372956301,
        },
        tick_size=TICK,
    )
    assert ch == {
        "entry_price": False,
        "stop_loss": True,
        "take_profit": False,
    }


def test_e_verify_pass_sl():
    om = MagicMock()
    row = ConfirmedOrder(
        order_id="oid",
        order_link_id="link",
        order_status="New",
        price=0.06751,
        qty=148.0,
        leaves_qty=148.0,
        cum_exec_qty=0.0,
        stop_loss=0.06724,
        take_profit=0.0677,
        raw={},
    )
    om.fetch_open_orders_direct = MagicMock(return_value=[{"info": row.raw}])

    def fake_find(*a, **k):
        return row

    from research.manual_long_box_live_sync_v1.order_settings import reconcile as rec

    orig = rec.find_limit_order
    rec.find_limit_order = fake_find
    try:
        ok, _, reason, _ = wait_for_amend_fields(
            om,
            "DOGEUSDT",
            order_id="oid",
            expected_sl=0.06724,
            tick_size=TICK,
            attempts=1,
        )
    finally:
        rec.find_limit_order = orig
    assert ok and reason == "amend_confirmed"


def test_f_verify_pass_sl_string_rest():
    assert prices_equal_tick("0.06724", 0.06724, TICK)


def test_g_verify_fail_old_sl():
    assert not prices_equal_tick(0.06731, 0.06724, TICK)


def test_h_missing_sl_no_pass():
    om = MagicMock()

    def fake_find(*a, **k):
        return ConfirmedOrder(
            order_id="oid",
            order_link_id=None,
            order_status="New",
            price=0.06751,
            qty=1.0,
            leaves_qty=1.0,
            cum_exec_qty=0.0,
            stop_loss=None,
            take_profit=0.0677,
            raw={},
        )

    from research.manual_long_box_live_sync_v1.order_settings import reconcile as rec

    orig = rec.find_limit_order
    rec.find_limit_order = fake_find
    try:
        ok, _, reason, _ = wait_for_amend_fields(
            om,
            "DOGEUSDT",
            order_id="oid",
            expected_sl=0.06724,
            tick_size=TICK,
            attempts=1,
        )
    finally:
        rec.find_limit_order = orig
    assert not ok
    assert "stop_loss_missing" in reason


def test_j_ack_ok_verify_fail_no_double_amend(tmp_path, monkeypatch):
    """execute_confirmed_amend makes exactly one adapter amend call."""
    state = tmp_path / "st.json"
    store = StateStore(state)
    store.put(
        BoxSyncState(
            drawing_id="d-x",
            symbol="DOGEUSDT",
            exchange_order_id="oid",
            order_link_id="mlb-sync-d-x",
            entry_price=0.06751,
            stop_price=0.06731,
            target_price=0.0677,
            order_status="OPEN",
            content_hash="h",
            last_action="CREATE",
        )
    )
    row = ConfirmedOrder(
        order_id="oid",
        order_link_id="mlb-sync-d-x",
        order_status="New",
        price=0.06751,
        qty=148.0,
        leaves_qty=148.0,
        cum_exec_qty=0.0,
        stop_loss=0.06731,
        take_profit=0.0677,
        raw={},
    )
    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend._live_order_row",
        lambda s, st: (row, None),
    )

    adapter = MagicMock()
    ack = MagicMock(ok=True, message=None)
    adapter.amend_limit_fields.return_value = ack
    adapter.order_manager.get_symbol_tick_size.return_value = TICK

    def fake_wait(*a, **k):
        return False, row, "stop_loss_mismatch:rest=0.06731 expected=0.06724", {"failures": "x"}

    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend.wait_for_amend_fields",
        fake_wait,
    )
    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend.BybitLiveExchangeAdapter.from_settings",
        lambda settings: adapter,
    )
    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend.audit_event",
        lambda *a, **k: {},
    )

    settings = MagicMock(writes_allowed=True)
    bound = {
        "action": "amend",
        "order_id": "oid",
        "changes": {"entry_price": True, "stop_loss": True, "take_profit": True},
        "exchange_snapshot": CURRENT,
        "tick_size": TICK,
        "tick_source": "test",
        "normalized_targets": {
            "entry_price": 0.06751,
            "stop_loss": 0.0665,
            "take_profit": 0.0677,
        },
    }
    box = {
        "entry_price": 0.06750868403847968,
        "stop_price": 0.06724344560241169,
        "target_price": 0.0677037372956301,
        "content_hash": "h",
    }
    out = execute_confirmed_amend("d-x", bound, settings=settings, state_path=state, box=box)
    assert not out["ok"]
    adapter.amend_limit_fields.assert_called_once()
    _args, kwargs = adapter.amend_limit_fields.call_args
    assert kwargs.get("stop_loss") == pytest.approx(0.0665)
    assert kwargs.get("price") is None
    assert kwargs.get("take_profit") is None
