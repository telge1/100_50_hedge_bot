"""Order controller — mocked exchange (no live writes)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc
from research.manual_long_box_live_sync_v1.order_settings.prepare_tokens import PrepareTokenStore


@pytest.fixture
def tmp_drawings(tmp_path):
    p = tmp_path / "drawings.json"
    p.write_text(
        json.dumps(
            {
                "drawings": [
                    {
                        "drawing_id": "d-test-long-1",
                        "drawing_type": "long_position",
                        "symbol": "DOGEUSDT",
                        "entry_price": 0.1,
                        "stop_price": 0.09,
                        "target_price": 0.12,
                        "start_timestamp": 1,
                        "end_timestamp": 2,
                        "visible": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return p


def test_prepare_binds_content_hash(tmp_drawings, monkeypatch):
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_drawings.parent / "tok.json"))
    out = oc.prepare_place("d-test-long-1", drawings_path=tmp_drawings)
    assert out["ok"]
    assert out["prepare_token"]
    assert out["content_hash"]


def test_confirm_rejects_stale_token(tmp_drawings, monkeypatch):
    store = PrepareTokenStore(tmp_drawings.parent / "tok.json")
    monkeypatch.setattr(oc, "_STORE", store)
    prep = oc.prepare_place("d-test-long-1", drawings_path=tmp_drawings)
    with patch.object(oc, "_settings") as gs:
        gs.return_value = MagicMock(writes_allowed=True, live_armed=True)
        bad = oc.confirm_place("d-test-long-1", prepare_token="invalid", drawings_path=tmp_drawings)
    assert not bad["ok"]


def test_resolve_inactive_without_state(monkeypatch):
    monkeypatch.setattr(
        oc,
        "StateStore",
        lambda _p: MagicMock(get=lambda _d: None),
    )
    monkeypatch.setattr(oc, "get_activation_row", lambda _a, _d: None)
    row = oc.resolve_order_status("d-x")
    assert row["phase"] == "INACTIVE"
    assert row["lamp"] == "inactive"


def test_short_prepare_create(tmp_drawings, monkeypatch):
    tmp_drawings.write_text(
        json.dumps(
            {
                "drawings": [
                    {
                        "drawing_id": "d-short",
                        "drawing_type": "short_position",
                        "symbol": "DOGEUSDT",
                        "entry_price": 0.1,
                        "stop_price": 0.11,
                        "target_price": 0.08,
                        "start_timestamp": 1,
                        "end_timestamp": 2,
                        "visible": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_drawings.parent / "tok.json"))
    with patch.object(oc, "verify_leverage_one_x", return_value={"ok": True, "leverage": 1.0}), patch.object(
        oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}
    ):
        out = oc.prepare_place("d-short", drawings_path=tmp_drawings)
    assert out["ok"]
    assert out["operation"] == "create"
    assert out["side"] == "short"
    assert out["position_idx"] == 2
