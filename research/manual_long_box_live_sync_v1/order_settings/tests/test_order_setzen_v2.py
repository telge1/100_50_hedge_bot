"""Setzen V2 — CREATE vs manual AMEND (mocked exchange only)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.manual_long_box_live_sync_v1.activation import BoxActivation
from research.manual_long_box_live_sync_v1.drawing_watch import observe_drawings
from research.manual_long_box_live_sync_v1.exchange_dry_run import DryRunExchangeAdapter, OrderStatus
from research.manual_long_box_live_sync_v1.order_links import allocate_order_link_id
from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc
from research.manual_long_box_live_sync_v1.order_settings.order_amend import execute_confirmed_amend_dry
from research.manual_long_box_live_sync_v1.order_settings.prepare_tokens import PrepareTokenStore
from research.manual_long_box_live_sync_v1.order_settings.reconcile import ConfirmedOrder
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, EventLog, StateStore
from research.manual_long_box_live_sync_v1.sync_engine import ManualLongBoxSyncEngine

DID = "d-amend-v2"


def _row(entry=0.0673, stop=0.0666, target=0.0677):
    return {
        "drawing_id": DID,
        "drawing_type": "long_position",
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


@pytest.fixture()
def ws(tmp_path):
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(json.dumps({"drawings": [_row()]}), encoding="utf-8")
    return drawings, state, events


def _eng(state: Path, events: Path, ex: DryRunExchangeAdapter | None = None):
    return ManualLongBoxSyncEngine(
        ex or DryRunExchangeAdapter(),
        StateStore(state),
        EventLog(events),
        dry_run=True,
    )


def _create_open(eng, drawings, *, link=None, sl=0.0666, tp=0.0677):
    link = link or f"mlb-sync-{DID}"
    act = BoxActivation(DID, True, "DOGEUSDT", 10.0, True, qty_doge=148.0, order_link_id=link)
    obs = observe_drawings(drawings)
    return eng.process_observation(obs, {DID: act})[0]


def test_k_sl_tp_drag_no_exchange_amend(ws):
    drawings, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _eng(state, events, ex)
    r1 = _create_open(eng, drawings, sl=0.0666, tp=0.0677)
    oid = r1["order_id"]
    o0 = ex.get_order(oid)
    data = json.loads(drawings.read_text())
    data["drawings"][0]["target_price"] = 0.0688
    drawings.write_text(json.dumps(data), encoding="utf-8")
    r2 = eng.process_observation(observe_drawings(drawings), {
        DID: BoxActivation(DID, True, "DOGEUSDT", 10.0, True, qty_doge=148.0, order_link_id=eng.state.get(DID).order_link_id),
    })[0]
    assert r2.get("action") == "no_entry_change"
    assert ex.get_order(oid).take_profit == o0.take_profit


def test_b_tp_amend_on_confirm(ws):
    drawings, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _eng(state, events, ex)
    r1 = _create_open(eng, drawings)
    oid = r1["order_id"]
    st = eng.state.get(DID)
    data = json.loads(drawings.read_text())
    data["drawings"][0]["target_price"] = 0.0688
    drawings.write_text(json.dumps(data), encoding="utf-8")
    box = {
        "entry_price": 0.0673,
        "stop_price": 0.0666,
        "target_price": 0.0688,
        "content_hash": "h1",
    }
    bound = {
        "action": "amend",
        "changes": {"entry_price": False, "stop_loss": False, "take_profit": True},
        "order_id": oid,
        "order_link_id": st.order_link_id,
    }
    out = execute_confirmed_amend_dry(DID, bound, engine=eng, box=box)
    assert out["ok"] and out["action"] == "AMEND_SL_TP"
    assert ex.get_order(oid).take_profit == pytest.approx(0.0688)
    assert out["order_id"] == oid


def test_c_sl_amend(ws):
    drawings, state, events = ws
    eng = _eng(state, events, DryRunExchangeAdapter())
    r1 = _create_open(eng, drawings)
    oid = r1["order_id"]
    bound = {
        "changes": {"entry_price": False, "stop_loss": True, "take_profit": False},
        "order_id": oid,
    }
    box = {"entry_price": 0.0673, "stop_price": 0.0665, "target_price": 0.0677, "content_hash": "x"}
    out = execute_confirmed_amend_dry(DID, bound, engine=eng, box=box)
    assert out["ok"]
    assert eng.exchange.get_order(oid).stop_loss == pytest.approx(0.0665)


def test_d_entry_amend(ws):
    drawings, state, events = ws
    eng = _eng(state, events, DryRunExchangeAdapter())
    r1 = _create_open(eng, drawings)
    oid = r1["order_id"]
    bound = {"changes": {"entry_price": True, "stop_loss": False, "take_profit": False}, "order_id": oid}
    box = {"entry_price": 0.0671, "stop_price": 0.0666, "target_price": 0.0677, "content_hash": "x"}
    out = execute_confirmed_amend_dry(DID, bound, engine=eng, box=box)
    assert out["ok"] and out["action"] == "AMEND_LIMIT"
    assert eng.exchange.get_order(oid).price == pytest.approx(0.0671)


def test_f_no_change_amend_skipped(ws, tmp_path, monkeypatch):
    drawings, state, events = ws
    eng = _eng(state, events, DryRunExchangeAdapter())
    _create_open(eng, drawings)
    st = eng.state.get(DID)

    def fake_row(_s, _st):
        return (
            ConfirmedOrder(
                order_id=st.exchange_order_id,
                order_link_id=st.order_link_id,
                order_status="New",
                price=0.0673,
                qty=148.0,
                leaves_qty=148.0,
                cum_exec_qty=0.0,
                stop_loss=0.0666,
                take_profit=0.0677,
                raw={},
            ),
            None,
        )

    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_path / "tok.json"))
    monkeypatch.setattr(oc, "DEFAULT_STATE_PATH", state)
    monkeypatch.setattr(oc, "DEFAULT_DRAWINGS_PATH", drawings)
    monkeypatch.setattr(oc, "resolve_order_status", lambda *a, **k: {"phase": "ORDER_OPEN", "lamp": "pending"})
    monkeypatch.setattr("research.manual_long_box_live_sync_v1.order_settings.order_controller._live_order_row", fake_row)
    prep = oc.prepare_place(DID, drawings_path=drawings)
    assert not prep["ok"] and prep["error"] == "no_changes"


def test_prepare_routes_create_vs_amend(ws, tmp_path, monkeypatch):
    drawings, state, events = ws
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_path / "tok.json"))
    monkeypatch.setattr(oc, "DEFAULT_STATE_PATH", state)
    monkeypatch.setattr(oc, "DEFAULT_DRAWINGS_PATH", drawings)
    monkeypatch.setattr(oc, "resolve_order_status", lambda *a, **k: {"phase": "INACTIVE", "lamp": "inactive"})
    with patch.object(oc, "_resolve_qty", return_value=(148.0, 10.0, {})):
        with patch.object(oc, "_settings") as gs:
            gs.return_value = MagicMock(
                writes_allowed=True,
                live_armed=True,
                fixed_test_notional_usd=10.0,
                environment="mainnet",
            )
            create_prep = oc.prepare_place(DID, drawings_path=drawings)
    assert create_prep.get("operation") == "create"

    st = StateStore(state)
    st.put(
        BoxSyncState(
            drawing_id=DID,
            symbol="DOGEUSDT",
            exchange_order_id="oid-1",
            order_link_id=f"mlb-sync-{DID}",
            entry_price=0.0673,
            stop_price=0.0666,
            target_price=0.0677,
            order_status="OPEN",
            content_hash="x",
            last_action="CREATE_LIMIT",
        )
    )

    def fake_row(_s, _st):
        return (
            ConfirmedOrder(
                order_id="oid-1",
                order_link_id=f"mlb-sync-{DID}",
                order_status="New",
                price=0.0673,
                qty=148.0,
                leaves_qty=148.0,
                cum_exec_qty=0.0,
                stop_loss=0.0666,
                take_profit=0.0677,
                raw={},
            ),
            None,
        )

    data = json.loads(drawings.read_text())
    data["drawings"][0]["target_price"] = 0.0688
    drawings.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(oc, "resolve_order_status", lambda *a, **k: {"phase": "ORDER_OPEN", "lamp": "pending"})
    monkeypatch.setattr("research.manual_long_box_live_sync_v1.order_settings.order_controller._live_order_row", fake_row)
    amend_prep = oc.prepare_place(DID, drawings_path=drawings)
    assert amend_prep.get("operation") == "amend"
    assert amend_prep.get("order_id") == "oid-1"


def test_j_new_link_only_on_create_cycle(ws):
    drawings, state, events = ws
    eng = _eng(state, events, DryRunExchangeAdapter())
    link1 = allocate_order_link_id(DID)
    r1 = _create_open(eng, drawings, link=link1)
    ex = eng.exchange
    ex.cancel_limit(r1["order_id"])
    st = eng.state.get(DID)
    st.order_status = OrderStatus.CANCELLED.value
    eng.state.put(st)
    link2 = allocate_order_link_id(DID)
    assert link2 != link1
    _create_open(eng, drawings, link=link2)
    assert eng.state.get(DID).order_link_id == link2
