"""PREPARE routes to AMEND when exchange has open order but local state is stale."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc
from research.manual_long_box_live_sync_v1.order_settings.prepare_tokens import PrepareTokenStore
from research.manual_long_box_live_sync_v1.order_settings.reconcile import ConfirmedOrder
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, StateStore

DID = "d-stale-cancel"


@pytest.fixture
def env(tmp_path, monkeypatch):
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    drawings.write_text(
        json.dumps(
            {
                "drawings": [
                    {
                        "drawing_id": DID,
                        "drawing_type": "long_position",
                        "symbol": "DOGEUSDT",
                        "visible": True,
                        "entry_price": 0.0675,
                        "stop_price": 0.0669,
                        "target_price": 0.0682,
                        "start_timestamp": 1,
                        "end_timestamp": 2,
                    }
                ]
            }
        )
    )
    st = StateStore(state)
    st.put(
        BoxSyncState(
            drawing_id=DID,
            symbol="DOGEUSDT",
            exchange_order_id="old-cancelled-id",
            order_link_id=f"mlb-sync-{DID}",
            entry_price=0.0675,
            stop_price=0.0669,
            target_price=0.0678,
            order_status="CANCELLED",
            content_hash="x",
            last_action="CANCEL_LIMIT",
        )
    )
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_path / "tok.json"))
    monkeypatch.setattr(oc, "DEFAULT_STATE_PATH", state)
    monkeypatch.setattr(oc, "DEFAULT_DRAWINGS_PATH", drawings)
    return drawings, state


def test_stale_cancelled_routes_amend_not_create_margin(env, monkeypatch):
    drawings, state = env
    live_row = ConfirmedOrder(
        order_id="live-oid-99",
        order_link_id=f"mlb-sync-{DID}-abc12345",
        order_status="New",
        price=0.0675,
        qty=148.0,
        leaves_qty=148.0,
        cum_exec_qty=0.0,
        stop_loss=0.0669,
        take_profit=0.0678,
        raw={},
    )

    monkeypatch.setattr(oc, "_discover_open_row", lambda *a, **k: (live_row, None))
    monkeypatch.setattr(
        oc,
        "_live_order_row",
        lambda settings, st: (live_row, None),
    )
    monkeypatch.setattr(
        oc,
        "_settings",
        lambda: MagicMock(
            writes_allowed=True,
            live_armed=True,
            environment="mainnet",
            fixed_test_notional_usd=10.0,
        ),
    )

    prep = oc.prepare_place(DID, drawings_path=drawings)
    assert prep["ok"] is True
    assert prep["operation"] == "amend"
    assert prep.get("can_place") is False
    assert "blockers" not in prep or not prep.get("blockers")
    assert prep["order_id"] == "live-oid-99"
    healed = StateStore(state).get(DID)
    assert healed.order_status == "OPEN"
    assert healed.exchange_order_id == "live-oid-99"
