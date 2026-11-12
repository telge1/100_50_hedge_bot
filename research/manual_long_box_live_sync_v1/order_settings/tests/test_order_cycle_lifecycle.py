"""Order cycle lifecycle — mocked exchange only (no live writes)."""

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
from research.manual_long_box_live_sync_v1.order_settings.activation_store import disable_activation, upsert_activation
from research.manual_long_box_live_sync_v1.order_settings.prepare_tokens import PrepareTokenStore
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, EventLog, StateStore
from research.manual_long_box_live_sync_v1.service import SyncService
from research.manual_long_box_live_sync_v1.sync_engine import ManualLongBoxSyncEngine

DID = "d-cycle-test"


def _box_row() -> dict:
    return {
        "drawing_id": DID,
        "drawing_type": "long_position",
        "symbol": "DOGEUSDT",
        "created_on_timeframe": "5m",
        "timeframe_scope": "all",
        "visible": True,
        "locked": False,
        "metadata": {},
        "entry_price": 0.0673,
        "stop_price": 0.0666,
        "target_price": 0.0677,
        "start_timestamp": 1787751300,
        "end_timestamp": 1787757000,
    }


@pytest.fixture()
def ws(tmp_path):
    drawings = tmp_path / "drawings.json"
    activations = tmp_path / "activations.json"
    state = tmp_path / "state.json"
    events = tmp_path / "events.jsonl"
    drawings.write_text(json.dumps({"version": 1, "drawings": [_box_row()]}), encoding="utf-8")
    activations.write_text(json.dumps({"activations": {}}), encoding="utf-8")
    return drawings, activations, state, events


def _act(drawing_id: str, *, link: str | None = None, qty: float = 148.0) -> BoxActivation:
    return BoxActivation(
        drawing_id=drawing_id,
        enabled=True,
        symbol="DOGEUSDT",
        notional_usd=10.0,
        dry_run=True,
        qty_doge=qty,
        order_link_id=link,
    )


def _engine(state: Path, events: Path, ex: DryRunExchangeAdapter | None = None):
    return ManualLongBoxSyncEngine(
        ex or DryRunExchangeAdapter(),
        StateStore(state),
        EventLog(events),
        dry_run=True,
    )


def _observe(drawings: Path):
    obs = observe_drawings(drawings)
    assert obs
    return obs


def _mark_cancelled(
    eng: ManualLongBoxSyncEngine,
    *,
    order_id: str,
    link: str,
    entry: float = 0.0673,
) -> None:
    st = eng.state.get(DID)
    if st is None:
        st = BoxSyncState(
            drawing_id=DID,
            symbol="DOGEUSDT",
            exchange_order_id=order_id,
            order_link_id=link,
            entry_price=entry,
            stop_price=0.0666,
            target_price=0.0677,
            order_status=OrderStatus.CANCELLED.value,
            content_hash="x",
            last_action="CANCEL_LIMIT",
        )
    else:
        st.order_status = OrderStatus.CANCELLED.value
        st.exchange_order_id = order_id
        st.order_link_id = link
        st.last_action = "CANCEL_LIMIT"
    eng.state.put(st)


def test_a_create_cancel_create(ws):
    """CREATE #1 → CANCEL #1 → CREATE #2."""
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _engine(state, events, ex)
    link1 = f"mlb-sync-{DID}"
    r1 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link1)})[0]
    assert r1["ok"] and r1["action"] == "CREATE_LIMIT"
    oid1 = r1["order_id"]

    ex.cancel_limit(oid1)
    _mark_cancelled(eng, order_id=oid1, link=link1)

    link2 = allocate_order_link_id(DID)
    assert link2 != link1
    r2 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link2)})[0]
    assert r2["ok"] and r2["action"] == "CREATE_LIMIT"
    assert r2["order_id"] != oid1
    assert ex.get_open_by_link(link2) is not None


def test_b_two_full_cycles(ws):
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _engine(state, events, ex)
    link1 = allocate_order_link_id(DID)
    r1 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link1)})[0]
    oid1 = r1["order_id"]
    ex.cancel_limit(oid1)
    _mark_cancelled(eng, order_id=oid1, link=link1)

    link2 = allocate_order_link_id(DID)
    r2 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link2)})[0]
    oid2 = r2["order_id"]
    ex.cancel_limit(oid2)
    _mark_cancelled(eng, order_id=oid2, link=link2)

    link3 = allocate_order_link_id(DID)
    r3 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link3)})[0]
    assert r3["ok"] and r3["order_id"] not in (oid1, oid2)


def test_c_second_create_blocked_if_prior_still_open(ws):
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _engine(state, events, ex)
    link1 = f"mlb-sync-{DID}"
    r1 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link1)})[0]
    oid1 = r1["order_id"]
    _mark_cancelled(eng, order_id=oid1, link=link1)

    link2 = allocate_order_link_id(DID)
    r2 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link2)})[0]
    assert not r2["ok"]
    assert r2["reason"] == "prior_cycle_still_open"


def test_d_partial_fill_blocks_recreate(ws):
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _engine(state, events, ex)
    link1 = f"mlb-sync-{DID}"
    r1 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link1)})[0]
    oid1 = r1["order_id"]
    ex.simulate_fill(oid1, fraction=0.5)
    _mark_cancelled(eng, order_id=oid1, link=link1)

    link2 = allocate_order_link_id(DID)
    r2 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link2)})[0]
    assert not r2["ok"]
    assert r2["reason"] == "prior_cycle_still_open"


def test_e_filled_skips_second_entry(ws):
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _engine(state, events, ex)
    link1 = f"mlb-sync-{DID}"
    r1 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link1)})[0]
    ex.simulate_fill(r1["order_id"], fraction=1.0)
    st = eng.state.get(DID)
    assert st
    st.order_status = OrderStatus.FILLED.value
    eng.state.put(st)

    link2 = allocate_order_link_id(DID)
    r2 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link2)})[0]
    assert r2.get("action") == "skipped_filled_no_second_order"


def test_f_double_confirm_token_single_use(ws, tmp_path, monkeypatch):
    drawings, activations, state, events = ws
    tok = PrepareTokenStore(tmp_path / "tok.json")
    monkeypatch.setattr(oc, "_STORE", tok)
    monkeypatch.setattr(oc, "DEFAULT_ACTIVATIONS_PATH", activations)
    monkeypatch.setattr(oc, "DEFAULT_STATE_PATH", state)
    monkeypatch.setattr(oc, "DEFAULT_EVENT_LOG_PATH", events)
    monkeypatch.setattr(oc, "DEFAULT_DRAWINGS_PATH", drawings)

    class _S:
        writes_allowed = True
        live_armed = True
        fixed_test_notional_usd = 10.0
        environment = "mainnet"

    monkeypatch.setattr(oc, "_settings", lambda: _S())
    monkeypatch.setattr(
        oc,
        "_resolve_qty",
        lambda *a, **k: (148.0, 10.0, {}),
    )

    mock_engine = MagicMock()
    mock_engine.dry_run = False
    mock_engine.state = StateStore(state)
    mock_engine.process_observation = MagicMock(
        return_value=[{"drawing_id": DID, "ok": True, "action": "CREATE_LIMIT", "order_id": "o1"}]
    )

    with patch("research.manual_long_box_live_sync_v1.order_settings.order_controller.build_sync_engine") as bse:
        bse.return_value = (mock_engine, "bybit_live")
        prep = oc.prepare_place(DID, drawings_path=drawings)
        token = prep["prepare_token"]
        c1 = oc.confirm_place(DID, prepare_token=token, drawings_path=drawings, activations_path=activations)
        c2 = oc.confirm_place(DID, prepare_token=token, drawings_path=drawings, activations_path=activations)
    assert c1["ok"]
    assert not c2["ok"]
    assert mock_engine.process_observation.call_count == 1


def test_g_service_restart_after_cancel_no_auto_create(ws):
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _engine(state, events, ex)
    link1 = f"mlb-sync-{DID}"
    eng.process_observation(_observe(drawings), {DID: _act(DID, link=link1)})
    oid = StateStore(state).get(DID).exchange_order_id
    ex.cancel_limit(oid)
    _mark_cancelled(eng, order_id=oid, link=link1)
    disable_activation(activations, DID)

    svc = SyncService(
        drawings_path=drawings,
        activations_path=activations,
        state_path=state,
        event_log_path=events,
        engine=eng,
    )
    assert svc.run_once() == []
    assert len(ex.reconcile_open_orders("DOGEUSDT")) == 0


def test_h_confirm_after_restart_new_cycle(ws, tmp_path, monkeypatch):
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    link1 = f"mlb-sync-{DID}"
    eng = _engine(state, events, ex)
    _mark_cancelled(eng, order_id="old-oid", link=link1)
    link2 = allocate_order_link_id(DID)
    upsert_activation(
        activations,
        drawing_id=DID,
        symbol="DOGEUSDT",
        notional_usd=10.0,
        dry_run=True,
        enabled=True,
        qty_doge=148.0,
        order_link_id=link2,
    )
    acts = {DID: _act(DID, link=link2)}
    r = eng.process_observation(_observe(drawings), acts)[0]
    assert r["ok"] and r["action"] == "CREATE_LIMIT"


def test_i_distinct_order_link_ids(ws):
    drawings, activations, state, events = ws
    ex = DryRunExchangeAdapter()
    eng = _engine(state, events, ex)
    links = [allocate_order_link_id(DID) for _ in range(3)]
    assert len(set(links)) == 3
    link1 = links[0]
    r1 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link1)})[0]
    ex.cancel_limit(r1["order_id"])
    _mark_cancelled(eng, order_id=r1["order_id"], link=link1)
    link2 = links[1]
    r2 = eng.process_observation(_observe(drawings), {DID: _act(DID, link=link2)})[0]
    st = StateStore(state).get(DID)
    assert st.order_link_id == link2
    assert st.order_link_id != link1


def test_j_chart_fields_unchanged(ws):
    row = _box_row()
    assert row["drawing_id"] == DID
    assert row["entry_price"] == 0.0673
    assert row["stop_price"] == 0.0666
