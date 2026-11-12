"""Mocked live Bybit integration tests — no exchange writes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.manual_long_box_live_sync_v1.activation import BoxActivation
from research.manual_long_box_live_sync_v1.drawing_watch import (
    DrawingFileObservation,
    hash_payload,
)
from research.manual_long_box_live_sync_v1.exchange_dry_run import OrderStatus
from research.manual_long_box_live_sync_v1.persistence import BoxSyncState, EventLog, StateStore
from research.manual_long_box_live_sync_v1.sync_engine import ManualLongBoxSyncEngine

from research.manual_long_box_live_sync_v1.order_settings.config import LiveOrderSettings, load_settings
from research.manual_long_box_live_sync_v1.order_settings.exchange_adapter import BybitLiveExchangeAdapter
from research.manual_long_box_live_sync_v1.order_settings.order_sync import build_sync_engine
from research.manual_long_box_live_sync_v1.order_settings.reconcile import (
    ConfirmedOrder,
    confirmed_to_dry_view,
    wait_for_order_rest,
)


def _armed_settings() -> LiveOrderSettings:
    return LiveOrderSettings(
        api_key="k",
        api_secret="s",
        environment="mainnet",
        category="linear",
        symbol="DOGEUSDT",
        position_idx=1,
        live_armed=True,
        order_qty=100.0,
        time_in_force="PostOnly",
        attach_tpsl_on_create=True,
        tpsl_mode="Full",
        tp_order_type="Market",
        sl_order_type="Market",
        sl_trigger_by="LastPrice",
        tp_trigger_by="LastPrice",
        use_activation_notional=False,
        fixed_test_notional_usd=None,
    )


def test_writes_blocked_by_default(monkeypatch):
    monkeypatch.delenv("MANUAL_LONG_BOX_LIVE_ARMED", raising=False)
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    s = load_settings(env_file=Path("/nonexistent"))
    assert not s.writes_allowed


def test_unarmed_adapter_rejects_writes():
    s = LiveOrderSettings(
        api_key="",
        api_secret="",
        environment="mainnet",
        category="linear",
        symbol="DOGEUSDT",
        position_idx=1,
        live_armed=False,
        order_qty=None,
        time_in_force="GTC",
        attach_tpsl_on_create=True,
        tpsl_mode="Full",
        tp_order_type="Market",
        sl_order_type="Market",
        sl_trigger_by="LastPrice",
        tp_trigger_by="LastPrice",
        use_activation_notional=False,
        fixed_test_notional_usd=None,
    )
    ad = BybitLiveExchangeAdapter(s)
    with pytest.raises(RuntimeError, match="Live writes blocked"):
        ad.create_limit_long(
            symbol="DOGEUSDT",
            price=0.05,
            qty=1,
            order_link_id="mlb-sync-x",
        )


def test_fixed_notional_skips_leverage_and_rounding():
    s = _armed_settings()
    s = LiveOrderSettings(
        **{**s.__dict__, "order_qty": None, "fixed_test_notional_usd": 10.0, "use_activation_notional": True}
    )
    om = MagicMock()
    om.open_long_limit.return_value = {"orderId": "ord-f"}
    ad = BybitLiveExchangeAdapter(s, order_manager=om)
    confirmed = ConfirmedOrder(
        order_id="ord-f",
        order_link_id="mlb-sync-f",
        order_status="New",
        price=0.06731,
        qty=148.0,
        leaves_qty=148.0,
        cum_exec_qty=0.0,
        raw={},
    )
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.exchange_adapter.wait_for_order_rest",
        return_value=(True, confirmed, "ok"),
    ):
        ad.create_limit_long(
            symbol="DOGEUSDT",
            price=0.06731,
            qty=148.0,
            order_link_id="mlb-sync-f",
            stop_loss=0.06,
            take_profit=0.08,
        )
    _, kwargs = om.open_long_limit.call_args
    assert kwargs.get("skip_leverage_adjustment") is True
    assert kwargs.get("skip_qty_rounding") is True


def test_create_passes_sl_tp_to_open_long_limit():
    s = _armed_settings()
    om = MagicMock()
    om.open_long_limit.return_value = {"orderId": "ord-1"}
    ad = BybitLiveExchangeAdapter(s, order_manager=om)
    confirmed = ConfirmedOrder(
        order_id="ord-1",
        order_link_id="mlb-sync-x",
        order_status="New",
        price=0.05,
        qty=100.0,
        leaves_qty=100.0,
        cum_exec_qty=0.0,
        raw={},
    )

    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.exchange_adapter.wait_for_order_rest",
        return_value=(True, confirmed, "ok"),
    ):
        ack = ad.create_limit_long(
            symbol="DOGEUSDT",
            price=0.05,
            qty=999,
            order_link_id="mlb-sync-x",
            stop_loss=0.04,
            take_profit=0.06,
        )
    assert ack.ok
    om.open_long_limit.assert_called_once()
    args, kwargs = om.open_long_limit.call_args
    assert args == ("DOGEUSDT", 0.05, 100.0)
    assert kwargs["stop_loss"] == 0.04
    assert kwargs["take_profit"] == 0.06
    assert kwargs["time_in_force"] == "PostOnly"
    assert kwargs["order_link_id"] == "mlb-sync-x"
    assert kwargs["sl_trigger_by"] == "LastPrice"
    assert kwargs["tp_trigger_by"] == "LastPrice"


def test_create_ack_without_reconcile_not_open():
    s = _armed_settings()
    om = MagicMock()
    om.open_long_limit.return_value = {"orderId": "ord-2"}
    ad = BybitLiveExchangeAdapter(s, order_manager=om)
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.exchange_adapter.wait_for_order_rest",
        return_value=(False, None, "not_found_on_book"),
    ):
        ack = ad.create_limit_long(
            symbol="DOGEUSDT",
            price=0.05,
            qty=1,
            order_link_id="mlb-sync-y",
            stop_loss=0.04,
            take_profit=0.06,
        )
    assert ack.ok
    assert ack.status == OrderStatus.PENDING_ACK.value


def test_amend_skips_when_filled():
    s = _armed_settings()
    om = MagicMock()
    ad = BybitLiveExchangeAdapter(s, order_manager=om)
    filled = ConfirmedOrder(
        order_id="o1",
        order_link_id="mlb-sync-z",
        order_status="Filled",
        price=0.05,
        qty=100.0,
        leaves_qty=0.0,
        cum_exec_qty=100.0,
        raw={},
    )
    with patch(
        "research.manual_long_box_live_sync_v1.order_settings.exchange_adapter.wait_for_order_rest",
        return_value=(True, filled, "ok"),
    ):
        ack = ad.amend_limit("o1", new_price=0.051)
    assert not ack.ok
    om.amend_order_direct.assert_not_called()


def test_amend_order_direct_only_price_fields():
    import importlib

    from research.manual_long_box_live_sync_v1.order_settings import credentials as cred

    cred._bootstrap_vendor_core()
    import core.bybit_order_manager as bom

    importlib.reload(bom)
    BybitOrderManager = bom.BybitOrderManager

    om = BybitOrderManager("k", "s")
    captured: dict = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"retCode": 0, "result": {}}

    def fake_post(url, headers, data, timeout=5):
        captured["body"] = json.loads(data)
        return Resp()

    with patch.object(om, "get_symbol_tick_size", return_value=0.00001), patch(
        "requests.post", fake_post
    ):
        om.amend_order_direct("DOGEUSDT", order_id="abc", price=0.12345)
    assert captured["body"]["price"] == "0.12345"
    assert "takeProfit" not in captured["body"]
    assert "stopLoss" not in captured["body"]


def test_build_sync_engine_defaults_dry_run(tmp_path, monkeypatch):
    monkeypatch.delenv("MANUAL_LONG_BOX_LIVE_ARMED", raising=False)
    monkeypatch.delenv("BYBIT_API_KEY", raising=False)
    monkeypatch.delenv("BYBIT_API_SECRET", raising=False)
    settings = load_settings(env_file=Path("/nonexistent"))
    assert not settings.writes_allowed
    state = tmp_path / "s.json"
    ev = tmp_path / "e.jsonl"
    eng, mode = build_sync_engine(state, ev, force_dry_run=False, settings=settings)
    assert mode == "dry_run"
    assert eng.dry_run is True


def test_live_restart_no_blind_create(tmp_path):
    state = tmp_path / "s.json"
    ev = tmp_path / "e.jsonl"
    store = StateStore(state)
    store.put(
        BoxSyncState(
            drawing_id="d-stale",
            symbol="DOGEUSDT",
            exchange_order_id="old-oid",
            order_link_id="mlb-sync-d-stale",
            entry_price=0.05,
            stop_price=0.04,
            target_price=0.06,
            order_status="OPEN",
            content_hash="x",
            last_action="CREATE_LIMIT",
        )
    )

    class StubExchange:
        supports_native_amend = True

        def get_open_by_link(self, _link):
            return None

        def get_order(self, _oid):
            return None

        def create_limit_long(self, **kwargs):
            raise AssertionError("blind create")

        def amend_limit(self, *a, **k):
            raise AssertionError("amend")

        def reconcile_open_orders(self, symbol=None):
            return []

    eng = ManualLongBoxSyncEngine(
        StubExchange(), store, EventLog(ev), dry_run=False
    )
    payload = {
        "version": 1,
        "drawings": [
            {
                "drawing_id": "d-stale",
                "drawing_type": "long_position",
                "symbol": "DOGEUSDT",
                "entry_price": 0.051,
                "stop_price": 0.04,
                "target_price": 0.06,
                "start_timestamp": 1,
                "end_timestamp": 2,
                "visible": True,
                "locked": False,
                "metadata": {},
                "created_on_timeframe": "5m",
                "timeframe_scope": "all",
            }
        ],
    }
    obs = DrawingFileObservation(
        path=tmp_path / "d.json",
        mtime=1.0,
        content_hash=hash_payload(payload),
        payload=payload,
        observed_at=1.0,
    )
    act = BoxActivation(
        drawing_id="d-stale",
        symbol="DOGEUSDT",
        enabled=True,
        notional_usd=50,
        dry_run=False,
    )
    r = eng._sync_one(obs, act)
    assert r["ok"] is False
    assert r["reason"] == "reconcile_required"


def test_confirmed_to_dry_view_partial():
    c = ConfirmedOrder(
        order_id="p1",
        order_link_id="l1",
        order_status="PartiallyFilled",
        price=0.05,
        qty=10.0,
        leaves_qty=4.0,
        cum_exec_qty=6.0,
        raw={},
    )
    v = confirmed_to_dry_view(c)
    assert v.status == OrderStatus.PARTIALLY_FILLED
