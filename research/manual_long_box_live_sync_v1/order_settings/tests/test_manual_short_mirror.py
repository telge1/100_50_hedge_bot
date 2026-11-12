"""Short mirror tests — mocked exchange only."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from research.manual_long_box_live_sync_v1.order_settings import order_controller as oc
from research.manual_long_box_live_sync_v1.order_settings.prepare_tokens import PrepareTokenStore
from research.manual_long_box_live_sync_v1.order_settings.tests.test_live_bybit_integration import (
    _armed_settings,
)


def test_open_short_limit_backward_compatible_signature():
    from research.manual_long_box_live_sync_v1.order_settings.credentials import (
        _bootstrap_vendor_core,
    )

    _bootstrap_vendor_core()
    from core.bybit_order_manager import BybitOrderManager

    om = BybitOrderManager("k", "s")
    with patch.object(om, "ensure_hedge_mode", return_value=True), patch.object(
        om, "ensure_max_leverage", return_value=True
    ), patch.object(om, "get_symbol_info", return_value=(1.0, 1.0)), patch.object(
        om, "get_symbol_tick_size", return_value=0.00001
    ), patch("core.bybit_order_manager.requests.post") as post:
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"retCode": 0, "result": {"orderId": "x"}}
        res = om.open_short_limit("DOGEUSDT", 0.07, 100, order_link_id="link-legacy")
    assert res is not None
    body = json.loads(post.call_args.kwargs.get("data") or post.call_args[0][1])
    assert body["side"] == "Sell"
    assert body["positionIdx"] == 2
    assert "stopLoss" not in body


def test_create_limit_short_passes_sl_tp_and_skip_leverage():
    from research.manual_long_box_live_sync_v1.order_settings.config import LiveOrderSettings
    from research.manual_long_box_live_sync_v1.order_settings.exchange_adapter import (
        BybitLiveExchangeAdapter,
    )

    s = LiveOrderSettings(
        **{
            **_armed_settings().__dict__,
            "order_qty": None,
            "fixed_test_notional_usd": 10.0,
            "use_activation_notional": True,
        }
    )
    om = MagicMock()
    om.open_short_limit.return_value = {"orderId": "s1"}
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.exchange_adapter.wait_for_order_rest",
        return_value=(True, MagicMock(order_status="New"), "confirmed_on_book"),
    ):
        ad = BybitLiveExchangeAdapter(s, order_manager=om)
        ad.create_limit_short(
            symbol="DOGEUSDT",
            price=0.07,
            qty=140.0,
            order_link_id="mlb-sync-d-short",
            stop_loss=0.08,
            take_profit=0.06,
        )
    om.open_short_limit.assert_called_once()
    _, kwargs = om.open_short_limit.call_args
    assert kwargs["stop_loss"] == 0.08
    assert kwargs["take_profit"] == 0.06
    assert kwargs["position_idx"] == 2
    assert kwargs.get("skip_leverage_adjustment") is True
    assert kwargs.get("time_in_force") == "PostOnly"


def test_short_prepare_create_operation(tmp_path, monkeypatch):
    drawings = tmp_path / "drawings.json"
    drawings.write_text(
        json.dumps(
            {
                "drawings": [
                    {
                        "drawing_id": "d-short-x",
                        "drawing_type": "short_position",
                        "symbol": "DOGEUSDT",
                        "entry_price": 0.07,
                        "stop_price": 0.08,
                        "target_price": 0.06,
                        "start_timestamp": 1,
                        "end_timestamp": 2,
                        "visible": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(oc, "_STORE", PrepareTokenStore(tmp_path / "tok.json"))
    monkeypatch.setattr(
        oc,
        "_settings",
        lambda: _armed_settings(),
    )
    with patch.object(oc, "verify_leverage_one_x", return_value={"ok": True, "leverage": 1.0}), patch.object(
        oc, "verify_available_margin", return_value={"ok": True, "available_margin_usdt": 100.0}
    ):
        out = oc.prepare_place("d-short-x", drawings_path=drawings)
    assert out["ok"] is True
    assert out["operation"] == "create"
    assert out["side"] == "short"
    assert out["position_idx"] == 2


def test_short_v6_amend_reuses_long_snapshot_logic():
    from research.manual_long_box_live_sync_v1.order_settings.price_tick import (
        build_amend_price_snapshot,
    )

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
