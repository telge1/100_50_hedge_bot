#!/usr/bin/env python3
"""READ-ONLY Bybit connectivity check — never logs secrets."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone

import requests

from research.manual_long_box_live_sync_v1.order_settings.config import (
    ENV_LOCAL_PATH,
    load_settings,
)
from research.manual_long_box_live_sync_v1.order_settings.credentials import (
    build_order_manager,
)

logging.disable(logging.CRITICAL)


def _probe_environment(api_key: str, api_secret: str, symbol: str) -> dict:
    """Signed wallet probe on mainnet vs testnet — no secrets in output."""
    import hashlib
    import hmac

    def wallet_ok(base_url: str) -> tuple[bool, str]:
        url = f"{base_url}/v5/account/wallet-balance"
        params = {"accountType": "UNIFIED", "coin": "USDT,USDC"}
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        ts = str(int(time.time() * 1000))
        recv = "20000"
        sign = hmac.new(
            api_secret.encode(),
            f"{ts}{api_key}{recv}{qs}".encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
        }
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            data = r.json()
            code = data.get("retCode")
            if code == 0:
                return True, "mainnet" if "api.bybit.com" in base_url else "testnet"
            return False, f"retCode={code} retMsg={data.get('retMsg','')}"
        except Exception as exc:
            return False, type(exc).__name__

    main_ok, main_msg = wallet_ok("https://api.bybit.com")
    if main_ok:
        return {"environment": "mainnet", "probe": "wallet_balance", "detail": main_msg}
    test_ok, test_msg = wallet_ok("https://api-testnet.bybit.com")
    if test_ok:
        return {"environment": "testnet", "probe": "wallet_balance", "detail": test_msg}
    return {"environment": "unknown", "mainnet": main_msg, "testnet": test_msg}


def _position_mode_from_api(om, symbol: str) -> dict:
    url = f"{om.base_url}/v5/position/list"
    import hashlib
    import hmac
    import urllib.parse

    params = {"category": "linear", "symbol": symbol}
    qs = "&".join(
        f"{urllib.parse.quote(k)}={urllib.parse.quote(str(v))}"
        for k, v in sorted(params.items())
    )
    ts = str(int(time.time() * 1000))
    recv = "20000"
    sign = hmac.new(
        om.secret_key.encode(),
        f"{ts}{om.api_key}{recv}{qs}".encode(),
        hashlib.sha256,
    ).hexdigest()
    headers = {
        "X-BAPI-API-KEY": om.api_key,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv,
    }
    r = om._session.get(url, headers=headers, params=params, timeout=10)
    data = r.json()
    if data.get("retCode") != 0:
        return {"ok": False, "error": data.get("retMsg")}
    rows = data.get("result", {}).get("list", [])
    idx_seen = sorted({int(p.get("positionIdx", 0)) for p in rows})
    long_size = 0.0
    for p in rows:
        if str(p.get("side", "")).lower() == "buy" or int(p.get("positionIdx", 0)) == 1:
            long_size += float(p.get("size", 0) or 0)
    hedge_hint = len([i for i in idx_seen if i in (1, 2)]) >= 1
    return {
        "ok": True,
        "position_idx_seen": idx_seen,
        "hedge_capable": hedge_hint,
        "long_size_doge": long_size,
        "raw_count": len(rows),
    }


def main() -> int:
    settings = load_settings()
    report = {
        "env_local_path": str(ENV_LOCAL_PATH),
        "env_local_exists": ENV_LOCAL_PATH.is_file(),
        "keys_present": {
            "BYBIT_API_KEY": bool(settings.api_key),
            "BYBIT_API_SECRET": bool(settings.api_secret),
        },
        "live_armed": settings.live_armed,
        "writes_allowed": settings.writes_allowed,
        "symbol": settings.symbol,
        "category": settings.category,
    }
    if not settings.api_key or not settings.api_secret:
        print(json.dumps({**report, "auth": "MISSING_CREDENTIALS"}, indent=2))
        return 2

    env_probe = _probe_environment(
        settings.api_key, settings.api_secret, settings.symbol
    )
    report["environment_probe"] = env_probe
    eff_env = env_probe.get("environment", settings.environment)
    settings = type(settings)(
        **{
            **settings.__dict__,
            "environment": eff_env if eff_env in ("mainnet", "testnet") else settings.environment,
        }
    )

    om = build_order_manager(settings, require_armed=False)
    sym = settings.symbol

    report["order_manager_base_url"] = om.base_url
    report["auth_wallet"] = (
        "OK" if env_probe.get("environment") in ("mainnet", "testnet") else "FAIL"
    )

    price = om.get_current_price(sym)
    ticker_ts = datetime.now(timezone.utc).isoformat()
    qty_step, min_qty = om.get_symbol_info(sym)
    tick = om.get_symbol_tick_size(sym)

    min_notional = None
    try:
        url = f"{om.base_url}/v5/market/instruments-info?category=linear&symbol={sym}"
        inst = requests.get(url, timeout=10).json()
        if inst.get("retCode") == 0:
            lot = inst["result"]["list"][0].get("lotSizeFilter", {})
            pf = inst["result"]["list"][0].get("priceFilter", {})
            min_notional = lot.get("minNotionalValue") or lot.get("minOrderQty")
            report["instrument_status"] = inst["result"]["list"][0].get("status")
    except Exception:
        pass

    pos = om.get_long_position(sym)
    open_orders = om.fetch_open_orders_direct(
        sym, timeout=10, order_filter="Order"
    )
    report["market"] = {
        "last_price": price,
        "queried_at_utc": ticker_ts,
        "tick_size": tick,
        "qty_step": qty_step,
        "min_qty": min_qty,
        "min_notional_hint": min_notional,
    }
    report["position_long"] = pos
    report["open_orders_count"] = len(open_orders or [])
    report["open_orders_sample"] = [
        {
            "orderId": (o.get("info") or o).get("orderId"),
            "price": o.get("price"),
            "qty": (o.get("info") or o).get("qty"),
            "status": (o.get("info") or o).get("orderStatus"),
        }
        for o in (open_orders or [])[:5]
    ]

    report["balance"] = {
        "equity_usdt": om.get_account_equity(),
        "available_usdt": om.get_account_available_balance(),
        "margin_balance_usdt": om.get_account_margin_balance(),
    }
    report["position_mode"] = _position_mode_from_api(om, sym)
    report["configured_position_idx"] = settings.position_idx
    report["methods_present"] = {
        "open_long_limit": hasattr(om, "open_long_limit"),
        "amend_order_direct": hasattr(om, "amend_order_direct"),
        "cancel_order_direct": hasattr(om, "cancel_order_direct"),
        "fetch_open_orders_direct": hasattr(om, "fetch_open_orders_direct"),
    }
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["auth_wallet"] == "OK" else 3


if __name__ == "__main__":
    sys.path.insert(0, "/home/telgenbuescher/projects/pools+ob+delta_bot")
    raise SystemExit(main())
