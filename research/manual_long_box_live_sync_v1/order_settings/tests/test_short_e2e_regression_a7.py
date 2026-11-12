"""
Phase A7 — full SHORT order cycle (mock / dry-run only).

Mirrors LONG-V6 lifecycle tests; no production code changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.manual_long_box_live_sync_v1.activation import BoxActivation
from research.manual_long_box_live_sync_v1.drawing_watch import observe_drawings
from research.manual_long_box_live_sync_v1.exchange_dry_run import (
    DryRunExchangeAdapter,
    OrderStatus,
)
from research.manual_long_box_live_sync_v1.order_links import allocate_order_link_id
from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc
from research.manual_long_box_live_sync_v1.order_settings.activation_store import disable_activation
from research.manual_long_box_live_sync_v1.order_settings.config import LiveOrderSettings
from research.manual_long_box_live_sync_v1.order_settings.order_amend import (
    execute_confirmed_amend,
    execute_confirmed_amend_dry,
)
from research.manual_long_box_live_sync_v1.order_settings.prepare_tokens import PrepareTokenStore
from research.manual_long_box_live_sync_v1.order_settings.price_tick import build_amend_price_snapshot
from research.manual_long_box_live_sync_v1.order_settings.reconcile import (
    ConfirmedOrder,
    discover_managed_open_order_ex,
)
from research.manual_long_box_live_sync_v1.order_settings.tests.test_live_bybit_integration import (
    _armed_settings,
)
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, EventLog, StateStore
from research.manual_long_box_live_sync_v1.sync_engine import ManualLongBoxSyncEngine

DID = "d-short-a7"


def _short_row(*, entry=0.07, stop=0.08, target=0.06) -> dict:
    return {
        "drawing_id": DID,
        "drawing_type": "short_position",
        "symbol": "DOGEUSDT",
        "created_on_timeframe": "5m",
        "timeframe_scope": "all",
        "visible": True,
        "locked": False,
        "metadata": {},
        "entry_price": entry,
        "stop_price": stop,
        "target_price": target,
        "start_timestamp": 1,
        "end_timestamp": 2,
    }


def _settings_live() -> LiveOrderSettings:
    return LiveOrderSettings(
        **{
            **_armed_settings().__dict__,
            "order_qty": None,
            "fixed_test_notional_usd": 10.0,
            "use_activation_notional": True,
        }
    )


def _eng(state: Path, events: Path, ex: DryRunExchangeAdapter | None = None):
    return ManualLongBoxSyncEngine(
        ex or DryRunExchangeAdapter(),
        StateStore(state),
        EventLog(events),
        dry_run=True,
    )


def _eng_live_mode(state: Path, events: Path):
    return ManualLongBoxSyncEngine(
        DryRunExchangeAdapter(),
        StateStore(state),
        EventLog(events),
        dry_run=False,
    )


def _act(link: str | None = None, qty: float = 140.0) -> BoxActivation:
    return BoxActivation(
        drawing_id=DID,
        enabled=True,
        symbol="DOGEUSDT",
        notional_usd=10.0,
        dry_run=True,
        qty_doge=qty,
        order_link_id=link or f"mlb-sync-{DID}",
    )


def _bootstrap_controller_paths(tmp_path, monkeypatch, drawings: Path, state: Path, events: Path):
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_path / "tok.json"))
    monkeypatch.setattr(oc, "DEFAULT_STATE_PATH", state)
    monkeypatch.setattr(oc, "DEFAULT_EVENT_LOG_PATH", events)
    monkeypatch.setattr(oc, "DEFAULT_DRAWINGS_PATH", drawings)
    monkeypatch.setattr(oc, "DEFAULT_ACTIVATIONS_PATH", tmp_path / "activations.json")
    (tmp_path / "activations.json").write_text('{"activations": {}}', encoding="utf-8")
    monkeypatch.setattr(oc, "_settings", lambda: _settings_live())


def test_a7_t1_short_create_full_cycle(tmp_path, monkeypatch):
    """TEST 1 — CREATE: inactive → prepare → confirm → Sell short on book → pending lamp."""
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(json.dumps({"version": 1, "drawings": [_short_row()]}), encoding="utf-8")
    _bootstrap_controller_paths(tmp_path, monkeypatch, drawings, state, events)

    inactive = oc.resolve_order_status(DID, settings=_settings_live())
    assert inactive["phase"] == "INACTIVE"
    assert inactive["lamp"] == "inactive"

    with patch.object(oc, "verify_leverage_one_x", return_value={"ok": True, "leverage": 1.0}), patch.object(
        oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}
    ), patch.object(oc, "_resolve_qty", return_value=(140.0, 10.0, {})), patch.object(
        oc, "resolve_order_status", return_value={"phase": "INACTIVE", "lamp": "inactive"}
    ):
        prep = oc.prepare_place(DID, drawings_path=drawings)

    assert prep["ok"] is True
    assert prep["operation"] == "create"
    assert prep["side"] == "short"
    assert prep["position_idx"] == 2

    eng = _eng_live_mode(state, events)
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.build_sync_engine",
        return_value=(eng, "bybit_live"),
    ), patch.object(oc, "verify_leverage_one_x", return_value={"ok": True, "leverage": 1.0}), patch.object(
        oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}
    ), patch.object(oc, "_resolve_qty", return_value=(140.0, 10.0, {})), patch.object(
        oc,
        "resolve_order_status",
        side_effect=lambda *a, **k: (
            {"phase": "CREATE_PENDING", "lamp": "loading"}
            if k.get("phase_hint") == "CREATE_PENDING"
            else {
                "phase": "ORDER_OPEN",
                "lamp": "pending",
                "order_id": eng.state.get(DID).exchange_order_id if eng.state.get(DID) else None,
            }
        ),
    ):
        conf = oc.confirm_place(DID, prepare_token=prep["prepare_token"], drawings_path=drawings)

    assert conf["ok"] is True
    st = eng.state.get(DID)
    assert st and st.exchange_order_id
    assert st.order_link_id
    o = eng.exchange.get_order(st.exchange_order_id)
    assert o.side == "Sell"
    assert o.stop_loss == pytest.approx(0.08)
    assert o.take_profit == pytest.approx(0.06)
    assert conf["lamp"] in ("pending", "loading")

    with patch.object(oc, "verify_leverage_one_x", return_value={"ok": True, "leverage": 1.0}), patch.object(
        oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}
    ), patch.object(oc, "_resolve_qty", return_value=(140.0, 10.0, {})):
        prep2 = oc.prepare_place(DID, drawings_path=drawings)
    assert prep2.get("operation") in ("amend", "blocked") or not prep2.get("ok")


def test_a7_t1_no_double_create_on_confirm(tmp_path, monkeypatch):
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(json.dumps({"drawings": [_short_row()]}), encoding="utf-8")
    _bootstrap_controller_paths(tmp_path, monkeypatch, drawings, state, events)
    eng = _eng_live_mode(state, events)
    with patch.object(oc, "_resolve_qty", return_value=(140.0, 10.0, {})), patch.object(
        oc, "verify_leverage_one_x", return_value={"ok": True, "leverage": 1.0}
    ), patch.object(oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}):
        prep = oc.prepare_place(DID, drawings_path=drawings)
    token = prep["prepare_token"]
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.build_sync_engine",
        return_value=(eng, "bybit_live"),
    ), patch.object(oc, "resolve_order_status", return_value={"phase": "CREATE_PENDING", "lamp": "loading"}), patch.object(
        oc, "_resolve_qty", return_value=(140.0, 10.0, {})
    ), patch.object(oc, "verify_leverage_one_x", return_value={"ok": True, "leverage": 1.0}), patch.object(
        oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}
    ):
        c1 = oc.confirm_place(DID, prepare_token=token, drawings_path=drawings)
        c2 = oc.confirm_place(DID, prepare_token=token, drawings_path=drawings)
    assert c1["ok"]
    assert not c2["ok"]
    assert len(eng.exchange.reconcile_open_orders("DOGEUSDT")) == 1


def test_a7_t2_short_amend_tp_sl_entry(tmp_path):
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(json.dumps({"drawings": [_short_row()]}), encoding="utf-8")
    eng = _eng(state, events, DryRunExchangeAdapter())
    link = f"mlb-sync-{DID}"
    r1 = eng.process_observation(observe_drawings(drawings), {DID: _act(link=link)})[0]
    oid = r1["order_id"]
    o0 = eng.exchange.get_order(oid)
    data = json.loads(drawings.read_text())
    data["drawings"][0]["target_price"] = 0.059
    drawings.write_text(json.dumps(data), encoding="utf-8")
    r_drag = eng.process_observation(
        observe_drawings(drawings),
        {DID: _act(link=eng.state.get(DID).order_link_id)},
    )[0]
    assert r_drag.get("action") == "no_entry_change"
    assert eng.exchange.get_order(oid).take_profit == o0.take_profit

    st = eng.state.get(DID)
    bound_tp = {
        "action": "amend",
        "changes": {"entry_price": False, "stop_loss": False, "take_profit": True},
        "order_id": oid,
        "order_link_id": st.order_link_id,
    }
    out_tp = execute_confirmed_amend_dry(
        DID,
        bound_tp,
        engine=eng,
        box={"entry_price": 0.07, "stop_price": 0.08, "target_price": 0.059, "content_hash": "h"},
    )
    assert out_tp["ok"] and out_tp["order_id"] == oid
    assert eng.exchange.get_order(oid).take_profit == pytest.approx(0.059)

    bound_sl = {
        "action": "amend",
        "changes": {"entry_price": False, "stop_loss": True, "take_profit": False},
        "order_id": oid,
    }
    execute_confirmed_amend_dry(
        DID,
        bound_sl,
        engine=eng,
        box={"entry_price": 0.07, "stop_price": 0.081, "target_price": 0.059, "content_hash": "h2"},
    )
    assert eng.exchange.get_order(oid).stop_loss == pytest.approx(0.081)

    bound_en = {
        "action": "amend",
        "changes": {"entry_price": True, "stop_loss": False, "take_profit": False},
        "order_id": oid,
    }
    out_en = execute_confirmed_amend_dry(
        DID,
        bound_en,
        engine=eng,
        box={"entry_price": 0.0695, "stop_price": 0.081, "target_price": 0.059, "content_hash": "h3"},
    )
    assert out_en["action"] == "AMEND_LIMIT"
    assert eng.exchange.get_order(oid).price == pytest.approx(0.0695)


def test_a7_t2_prepare_amend_not_create_when_open(tmp_path, monkeypatch):
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(json.dumps({"drawings": [_short_row()]}), encoding="utf-8")
    eng = _eng(state, events)
    r1 = eng.process_observation(observe_drawings(drawings), {DID: _act()})[0]
    st = eng.state.get(DID)
    _bootstrap_controller_paths(tmp_path, monkeypatch, drawings, state, events)
    live = ConfirmedOrder(
        order_id=r1["order_id"],
        order_link_id=st.order_link_id,
        order_status="New",
        price=0.07,
        qty=140.0,
        leaves_qty=140.0,
        cum_exec_qty=0.0,
        stop_loss=0.08,
        take_profit=0.06,
        side="Sell",
        position_idx=2,
        raw={},
    )
    data = json.loads(drawings.read_text())
    data["drawings"][0]["target_price"] = 0.059
    drawings.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(oc, "resolve_order_status", lambda *a, **k: {"phase": "ORDER_OPEN", "lamp": "pending"})
    monkeypatch.setattr("research.manual_long_box_live_sync_v1.order_settings.order_controller._live_order_row", lambda s, _st: (live, None))
    with patch.object(oc, "_resolve_qty", return_value=(140.0, 10.0, {})):
        prep = oc.prepare_place(DID, drawings_path=drawings)
    assert prep["ok"] and prep["operation"] == "amend"
    assert prep["order_id"] == r1["order_id"]
    assert prep.get("changes", {}).get("take_profit") is True


def test_a7_t2_v6_amend_pipeline_mocked(tmp_path, monkeypatch):
    state = tmp_path / "st.json"
    store = StateStore(state)
    store.put(
        BoxSyncState(
            drawing_id=DID,
            symbol="DOGEUSDT",
            exchange_order_id="oid-s",
            order_link_id=f"mlb-sync-{DID}",
            entry_price=0.07,
            stop_price=0.08,
            target_price=0.06,
            order_status="OPEN",
            content_hash="h",
            last_action="CREATE_LIMIT",
        )
    )
    row = ConfirmedOrder(
        order_id="oid-s",
        order_link_id=f"mlb-sync-{DID}",
        order_status="New",
        price=0.07,
        qty=140.0,
        leaves_qty=140.0,
        cum_exec_qty=0.0,
        stop_loss=0.08,
        take_profit=0.06,
        side="Sell",
        position_idx=2,
        raw={},
    )
    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend._live_order_row",
        lambda s, st: (row, None),
    )
    adapter = MagicMock()
    adapter.amend_limit_fields.return_value = MagicMock(ok=True, message=None)
    adapter.order_manager.get_symbol_tick_size.return_value = 0.00001
    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend.BybitLiveExchangeAdapter.from_settings",
        lambda settings: adapter,
    )
    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend.wait_for_amend_fields",
        lambda *a, **k: (True, row, "ok", {}),
    )
    monkeypatch.setattr(
        "research.manual_long_box_live_sync_v1.order_settings.order_amend.audit_event",
        lambda *a, **k: None,
    )
    snap = build_amend_price_snapshot(
        chart_raw={"entry_price": 0.07, "stop_price": 0.08, "target_price": 0.059},
        exchange_current={"entry_price": 0.07, "stop_loss": 0.08, "take_profit": 0.06},
        tick_size=0.00001,
        tick_source="test",
    )
    bound = {
        "action": "amend",
        "order_id": "oid-s",
        "changes": snap["changes"],
        "exchange_snapshot": snap["exchange_snapshot"],
        "normalized_targets": snap["normalized_targets"],
        "tick_size": 0.00001,
        "tick_source": "test",
    }
    box = {"entry_price": 0.07, "stop_price": 0.08, "target_price": 0.059, "content_hash": "h"}
    out = execute_confirmed_amend(DID, bound, settings=MagicMock(writes_allowed=True), state_path=state, box=box)
    assert out["ok"]
    assert out["order_id"] == "oid-s"
    adapter.amend_limit_fields.assert_called_once()
    kwargs = adapter.amend_limit_fields.call_args.kwargs
    assert kwargs.get("price") is None
    assert kwargs.get("take_profit") == pytest.approx(0.059)


def test_a7_t3_short_cancel(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    drawings = tmp_path / "drawings.json"
    drawings.write_text(json.dumps({"drawings": [_short_row()]}), encoding="utf-8")
    _bootstrap_controller_paths(tmp_path, monkeypatch, drawings, state, tmp_path / "ev.jsonl")
    store = StateStore(state)
    store.put(
        BoxSyncState(
            drawing_id=DID,
            symbol="DOGEUSDT",
            exchange_order_id="oid-cancel",
            order_link_id=f"mlb-sync-{DID}",
            entry_price=0.07,
            stop_price=0.08,
            target_price=0.06,
            order_status="OPEN",
            content_hash="x",
            last_action="CREATE_LIMIT",
        )
    )
    monkeypatch.setattr(
        oc,
        "resolve_order_status",
        lambda *a, **k: (
            {"phase": "CANCEL_PENDING", "lamp": "loading"}
            if k.get("phase_hint") == "CANCEL_PENDING"
            else {"phase": "ORDER_OPEN", "lamp": "pending", "order_id": "oid-cancel"}
        ),
    )
    prep = oc.prepare_cancel(DID)
    assert prep["ok"] and prep["prepare_token"]

    adapter = MagicMock()
    adapter.cancel_limit.return_value = MagicMock(ok=True)
    adapter.order_manager = MagicMock()
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.BybitLiveExchangeAdapter.from_settings",
        return_value=adapter,
    ), patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.wait_for_order_rest",
        return_value=(True, None, "gone"),
    ), patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.find_limit_order",
        return_value=None,
    ), patch.object(oc, "resolve_order_status", return_value={"phase": "INACTIVE", "lamp": "inactive"}):
        out = oc.confirm_cancel(DID, prepare_token=prep["prepare_token"])
    assert out["ok"]
    assert out["lamp"] == "inactive"
    store.load()
    assert store.get(DID).order_status == OrderStatus.CANCELLED.value


def test_a7_t4_cancel_then_recreate(tmp_path):
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(json.dumps({"drawings": [_short_row()]}), encoding="utf-8")
    eng = _eng(state, events)
    link1 = f"mlb-sync-{DID}"
    r1 = eng.process_observation(observe_drawings(drawings), {DID: _act(link=link1)})[0]
    oid1 = r1["order_id"]
    eng.exchange.cancel_limit(oid1)
    st = eng.state.get(DID)
    st.order_status = OrderStatus.CANCELLED.value
    eng.state.put(st)
    link2 = allocate_order_link_id(DID)
    assert link2 != link1
    r2 = eng.process_observation(observe_drawings(drawings), {DID: _act(link=link2)})[0]
    assert r2["ok"] and r2["action"] == "CREATE_LIMIT"
    assert r2["order_id"] != oid1
    assert eng.state.get(DID).order_link_id == link2
    o = eng.exchange.get_order(r2["order_id"])
    assert o.side == "Sell"


def test_a7_t5_filled_green_and_edge_cases(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    store = StateStore(state)
    store.put(
        BoxSyncState(
            drawing_id=DID,
            symbol="DOGEUSDT",
            exchange_order_id="oid-filled",
            order_link_id=f"mlb-sync-{DID}",
            entry_price=0.07,
            stop_price=0.08,
            target_price=0.06,
            order_status="FILLED",
            content_hash="x",
            last_action="CREATE_LIMIT",
        )
    )
    _bootstrap_controller_paths(tmp_path, monkeypatch, tmp_path / "d.json", state, tmp_path / "e.jsonl")
    om = MagicMock()
    om.get_short_position.return_value = (140.0, 0.07)
    adapter = MagicMock(order_manager=om)
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.BybitLiveExchangeAdapter.from_settings",
        return_value=adapter,
    ), patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller._load_manual_box",
        return_value=({"side": "short", "symbol": "DOGEUSDT", "position_idx": 2}, None),
    ), patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.discover_managed_open_order_ex",
        return_value=(None, None),
    ):
        status = oc.resolve_order_status(DID, settings=_settings_live())
    assert status["phase"] == "POSITION_FILLED"
    assert status["lamp"] == "filled"
    om.get_short_position.assert_called_once()

    drawings = tmp_path / "drawings2.json"
    state2 = tmp_path / "state2.json"
    events = tmp_path / "events2.jsonl"
    drawings.write_text(json.dumps({"drawings": [_short_row()]}), encoding="utf-8")
    eng = _eng(state2, events)
    link = f"mlb-sync-{DID}"
    r1 = eng.process_observation(observe_drawings(drawings), {DID: _act(link=link)})[0]
    eng.exchange.simulate_fill(r1["order_id"], fraction=1.0)
    st = eng.state.get(DID)
    st.order_status = OrderStatus.FILLED.value
    eng.state.put(st)
    r2 = eng.process_observation(
        observe_drawings(drawings),
        {DID: _act(link=allocate_order_link_id(DID))},
    )[0]
    assert r2.get("action") == "skipped_filled_no_second_order"

    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.discover_managed_open_order_ex",
        return_value=(None, None),
    ), patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller._load_manual_box",
        return_value=({"side": "short", "symbol": "DOGEUSDT"}, None),
    ), patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.BybitLiveExchangeAdapter.from_settings",
        return_value=MagicMock(order_manager=MagicMock(get_short_position=MagicMock(return_value=(None, None)))),
    ):
        store.put(
            BoxSyncState(
                drawing_id="d-unclear",
                symbol="DOGEUSDT",
                exchange_order_id="oid-x",
                order_link_id="mlb-sync-d-unclear",
                entry_price=0.07,
                stop_price=0.08,
                target_price=0.06,
                order_status="OPEN",
                content_hash="x",
                last_action="CREATE",
            )
        )
        unclear = oc.resolve_order_status("d-unclear", settings=_settings_live())
    assert unclear["phase"] == "RECONCILE_REQUIRED"
    assert unclear["lamp"] == "warning"


def test_a7_t6_side_isolation_discover():
    om = MagicMock()
    om.fetch_open_orders_direct.return_value = [
        {
            "id": "long-1",
            "info": {
                "orderId": "long-1",
                "orderLinkId": f"mlb-sync-{DID}",
                "side": "Buy",
                "positionIdx": 1,
                "orderStatus": "New",
                "price": "0.07",
                "qty": "140",
                "leavesQty": "140",
                "cumExecQty": "0",
            },
        },
        {
            "id": "short-1",
            "info": {
                "orderId": "short-1",
                "orderLinkId": f"mlb-sync-{DID}",
                "side": "Sell",
                "positionIdx": 2,
                "orderStatus": "New",
                "price": "0.07",
                "qty": "140",
                "leavesQty": "140",
                "cumExecQty": "0",
            },
        },
    ]
    row, err = discover_managed_open_order_ex(
        om,
        "DOGEUSDT",
        DID,
        expected_side="Sell",
        expected_position_idx=2,
    )
    assert err is None
    assert row and row.order_id == "short-1"
    row_l, err_l = discover_managed_open_order_ex(
        om,
        "DOGEUSDT",
        DID,
        expected_side="Buy",
        expected_position_idx=1,
    )
    assert row_l and row_l.order_id == "long-1"

    om.fetch_open_orders_direct.return_value = [
        {
            "id": "s1",
            "info": {
                "orderId": "s1",
                "orderLinkId": f"mlb-sync-{DID}-a",
                "side": "Sell",
                "positionIdx": 2,
                "orderStatus": "New",
                "price": "0.07",
            },
        },
        {
            "id": "s2",
            "info": {
                "orderId": "s2",
                "orderLinkId": f"mlb-sync-{DID}-b",
                "side": "Sell",
                "positionIdx": 2,
                "orderStatus": "New",
                "price": "0.071",
            },
        },
    ]
    row_amb, err_amb = discover_managed_open_order_ex(
        om,
        "DOGEUSDT",
        DID,
        expected_side="Sell",
        expected_position_idx=2,
    )
    assert row_amb is None
    assert err_amb == "ambiguous_managed_orders"


def test_a7_t6_direction_from_drawing_only(tmp_path, monkeypatch):
    drawings = tmp_path / "drawings.json"
    drawings.write_text(
        json.dumps(
            {
                "drawings": [
                    _short_row(),
                    {
                        **_short_row(entry=0.05),
                        "drawing_id": "d-long-fake",
                        "drawing_type": "long_position",
                        "stop_price": 0.04,
                        "target_price": 0.06,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    box, err = oc._load_manual_box(DID, drawings_path=drawings)
    assert err is None
    assert box["side"] == "short"
    assert box["position_idx"] == 2


DASH_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")


def test_a7_ui_static_short_enabled_contract():
    """Static on-disk UI contract (no running browser / no dashboard restart)."""
    chart = (DASH_ROOT / "static/research_trp/chart.js").read_text(encoding="utf-8")
    host = (DASH_ROOT / "static/js/research/research_charts.js").read_text(encoding="utf-8")
    assert "short_position: true" in chart
    assert 'p.side === "short" ? "short" : "long"' in chart
    assert "Short-Limit auf Bybit setzen" in chart
    assert "SHORT" in host or "short" in host.lower()
    assert "short_orders_enabled" not in chart or "short_orders_enabled: false" not in chart
