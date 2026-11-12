"""Prepare/confirm gates for fixed 10 USDT path."""

from __future__ import annotations

import json
from decimal import Decimal
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
                        "entry_price": 0.06731,
                        "stop_price": 0.06,
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
    return p


def _fixed_settings(**overrides):
    base = dict(
        api_key="k",
        api_secret="s",
        environment="mainnet",
        category="linear",
        symbol="DOGEUSDT",
        position_idx=1,
        live_armed=True,
        order_qty=None,
        time_in_force="GTC",
        attach_tpsl_on_create=True,
        tpsl_mode="Full",
        tp_order_type="Market",
        sl_order_type="Market",
        sl_trigger_by="LastPrice",
        tp_trigger_by="LastPrice",
        use_activation_notional=True,
        fixed_test_notional_usd=10.0,
    )
    base.update(overrides)
    if "writes_allowed" not in overrides:
        overrides["writes_allowed"] = bool(base.get("live_armed")) and bool(base.get("api_key"))
    m = MagicMock()
    for k, v in {**base, **overrides}.items():
        setattr(m, k, v)
    return m


def test_prepare_fixed_notional_qty_at_entry(tmp_drawings, monkeypatch):
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_drawings.parent / "tok.json"))
    mock_settings = _fixed_settings(writes_allowed=False, live_armed=False)
    monkeypatch.setattr(oc, "_settings", lambda: mock_settings)
    with patch.object(oc, "_instrument_steps", return_value=((Decimal("1"), Decimal("1")), None)):
        out = oc.prepare_place("d-test-long-1", drawings_path=tmp_drawings)
    assert out["ok"]
    assert out["qty_doge"] == 148.0
    assert out["notional_usd"] == pytest.approx(9.96188, rel=1e-4)
    assert out["notional_cap_usdt"] == 10.0
    assert not out["can_place"]


def test_prepare_blocks_leverage_not_1x(tmp_drawings, monkeypatch):
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_drawings.parent / "tok.json"))
    mock_settings = _fixed_settings()
    monkeypatch.setattr(oc, "_settings", lambda: mock_settings)
    om = MagicMock()
    with patch.object(oc, "_instrument_steps", return_value=((Decimal("1"), Decimal("1")), om)):
        with patch.object(oc, "verify_leverage_one_x", return_value={"ok": False, "error": "leverage_not_1x", "leverage": 10.0}):
            with patch.object(oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}):
                out = oc.prepare_place("d-test-long-1", drawings_path=tmp_drawings)
    assert out["ok"]
    assert not out["order_valid"]
    assert out["blockers"].get("leverage") == "leverage_not_1x"


def test_confirm_rejects_when_blockers(tmp_drawings, monkeypatch):
    store = PrepareTokenStore(tmp_drawings.parent / "tok.json")
    monkeypatch.setattr(oc, "_STORE", store)
    mock_settings = _fixed_settings()
    monkeypatch.setattr(oc, "_settings", lambda: mock_settings)
    om = MagicMock()
    with patch.object(oc, "_instrument_steps", return_value=((Decimal("1"), Decimal("1")), om)):
        with patch.object(oc, "verify_leverage_one_x", return_value={"ok": False, "error": "leverage_not_1x"}):
            with patch.object(oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}):
                prep = oc.prepare_place("d-test-long-1", drawings_path=tmp_drawings)
    token = prep["prepare_token"]
    with patch.object(oc, "_instrument_steps", return_value=((Decimal("1"), Decimal("1")), om)):
        with patch.object(oc, "verify_leverage_one_x", return_value={"ok": False, "error": "leverage_not_1x"}):
            with patch.object(oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}):
                conf = oc.confirm_place("d-test-long-1", prepare_token=token, drawings_path=tmp_drawings)
    assert not conf["ok"]
    assert conf["error"] == "order_blocked"
