"""Forensics-fix: drag gate, ambiguous discovery, audit log (mocked exchange)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc
from research.manual_long_box_live_sync_v1.order_settings.drag_sync import (
    drawing_drag_triggers_entry_sync,
)
from research.manual_long_box_live_sync_v1.order_settings.mlb_audit_log import (
    DEFAULT_AUDIT_PATH,
    audit_event,
)
from research.manual_long_box_live_sync_v1.order_settings.prepare_tokens import PrepareTokenStore
from research.manual_long_box_live_sync_v1.order_settings.reconcile import (
    ConfirmedOrder,
    discover_managed_open_order_ex,
)
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, StateStore

DID = "d-forensics"


def test_a_sl_drag_does_not_trigger_entry_sync():
    body = {
        "type": "drag",
        "mode": "resize-sl",
        "entry_price": 0.067,
        "stop_price": 0.066,
        "target_price": 0.068,
    }
    assert drawing_drag_triggers_entry_sync(body) is False


def test_b_tp_drag_does_not_trigger_entry_sync():
    body = {"type": "drag", "mode": "resize-tp", "entry_price": 0.067}
    assert drawing_drag_triggers_entry_sync(body) is False


def test_c_entry_drag_triggers_sync():
    assert drawing_drag_triggers_entry_sync({"type": "drag", "mode": "resize-entry", "entry_price": 1}) is True
    assert drawing_drag_triggers_entry_sync({"type": "drag", "mode": "move", "entry_price": 1}) is True


def test_e_ambiguous_open_orders_blocks_prepare(tmp_path, monkeypatch):
    drawings = tmp_path / "drawings.json"
    state = tmp_path / "state.json"
    audit = tmp_path / "audit.jsonl"
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
    monkeypatch.setattr(oc, "DEFAULT_DRAWINGS_PATH", drawings)
    monkeypatch.setattr(oc, "DEFAULT_STATE_PATH", state)
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_path / "tok.json"))
    monkeypatch.setattr(
        oc,
        "_discover_open_row",
        lambda *a, **k: (None, "ambiguous_managed_orders"),
    )
    monkeypatch.setattr(
        oc,
        "_settings",
        lambda: MagicMock(writes_allowed=True, live_armed=True, environment="mainnet"),
    )
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.audit_event",
        lambda *a, **k: audit_event(*a, path=audit, **k),
    ):
        prep = oc.prepare_place(DID, drawings_path=drawings)
    assert prep["ok"] is False
    assert prep["error"] == "ambiguous_managed_orders"


def test_discover_ex_ambiguous_two_prefix_hits():
    om = MagicMock()
    prefix = f"mlb-sync-{DID}"
    om.fetch_open_orders_direct.return_value = [
        {"info": {"orderId": "1", "orderLinkId": prefix + "-a", "orderStatus": "New", "price": "0.1", "qty": "1"}},
        {"info": {"orderId": "2", "orderLinkId": prefix + "-b", "orderStatus": "New", "price": "0.1", "qty": "1"}},
    ]
    row, err = discover_managed_open_order_ex(om, "DOGEUSDT", DID)
    assert row is None
    assert err == "ambiguous_managed_orders"


def test_p_audit_log_no_secrets(tmp_path):
    path = tmp_path / "audit.jsonl"
    audit_event(
        "test",
        source="manual_confirm",
        drawing_id=DID,
        path=path,
        api_secret="must-not-appear",
        operation="amend",
    )
    text = path.read_text(encoding="utf-8")
    assert "must-not-appear" not in text
    assert "manual_confirm" in text


def test_run_sync_pass_audits_drawing_sync(tmp_path, monkeypatch):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(
        oc,
        "_settings",
        lambda: MagicMock(writes_allowed=True),
    )
    monkeypatch.setattr(oc, "observe_drawings", lambda *a, **k: None)
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.order_controller.audit_event",
        lambda *a, **k: audit_event(*a, path=audit, **k),
    ):
        out = oc.run_sync_pass(source="drawing_sync", drag_mode="move")
    assert out == []
