#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import hmac
import hashlib
import urllib.parse
from pathlib import Path

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check whether the Bybit exchange is flat for a symbol.")
    parser.add_argument("--symbol", required=True, help="Symbol to inspect")
    parser.add_argument("--config", required=True, help="Runtime config JSON file that contains category/base_url")
    parser.add_argument("--category", default=None, help="Override category from config")
    parser.add_argument("--base-url", default=None, help="Override Bybit base URL")
    return parser.parse_args()


def load_config(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError(f"config missing at {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        raise RuntimeError(f"invalid config {path}: {exc}")


def float_size(value: object | None) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


RECV_WINDOW = "20000"


def _signed_headers(api_key: str, secret: str, payload: str) -> dict[str, str]:
    timestamp = str(int(time.time() * 1000))
    to_sign = timestamp + api_key + RECV_WINDOW + payload
    signature = hmac.new(secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": signature,
        "X-BAPI-SIGN-TYPE": "2",
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    }


def _call_bybit_get(api_key: str, secret: str, base_url: str, path: str, params: dict[str, object]) -> dict | None:
    payload = urllib.parse.urlencode({k: str(v) for k, v in sorted(params.items()) if v is not None})
    url = f"{base_url.rstrip('/')}{path}"
    if payload:
        url = f"{url}?{payload}"
    headers = _signed_headers(api_key, secret, payload)
    try:
        resp = requests.get(url, headers=headers, timeout=10)
    except Exception as exc:
        print(f"ERROR: bybit GET request failed: {exc}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"ERROR: bybit GET {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
        return None
    data = resp.json()
    if data.get("retCode") != 0:
        print(f"ERROR: bybit GET retCode {data.get('retCode')}: {data.get('retMsg')}", file=sys.stderr)
        return None
    return data


def _fetch_positions(api_key: str, secret: str, base_url: str, symbol: str, category: str) -> list[dict[str, object]]:
    data = _call_bybit_get(
        api_key,
        secret,
        base_url,
        "/v5/position/list",
        {"category": category, "symbol": symbol},
    )
    if not data:
        return []
    result = data.get("result") or {}
    return result.get("list") or []


def _fetch_open_orders(api_key: str, secret: str, base_url: str, symbol: str, category: str) -> list[dict[str, object]]:
    data = _call_bybit_get(
        api_key,
        secret,
        base_url,
        "/v5/order/realtime",
        {"category": category, "symbol": symbol},
    )
    if not data:
        return []
    result = data.get("result") or {}
    return result.get("list") or result.get("data") or []


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    config = load_config(config_path)
    symbol = (args.symbol or str(config.get("symbol") or "")).upper()
    if not symbol:
        print("ERROR: symbol not provided", file=sys.stderr)
        return 2

    category = args.category or str(config.get("category") or "linear")
    base_url = args.base_url or str(config.get("base_url") or "https://api.bybit.com")

    api_key = os.environ.get("BYBIT_API_KEY")
    secret = os.environ.get("BYBIT_API_SECRET")
    if not api_key or not secret:
        print("ERROR: BYBIT_API_KEY/BYBIT_API_SECRET not set", file=sys.stderr)
        return 2

    positions = _fetch_positions(api_key, secret, base_url, symbol, category)
    long_qty = 0.0
    short_qty = 0.0
    for pos in positions:
        side = str(pos.get("side") or "").lower()
        size = float_size(pos.get("size") or pos.get("positionQty") or pos.get("position_size"))
        if side == "buy":
            long_qty += size
        elif side == "sell":
            short_qty += size

    open_orders = _fetch_open_orders(api_key, secret, base_url, symbol, category)
    open_order_count = len(open_orders)

    flat = (
        abs(long_qty) < 1e-9
        and abs(short_qty) < 1e-9
        and open_order_count == 0
    )

    payload = {
        "symbol": symbol,
        "category": category,
        "long_qty": long_qty,
        "short_qty": short_qty,
        "open_order_count": open_order_count,
        "flat": flat,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
