from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Mapping
from urllib.parse import urlencode

import requests

logger = logging.getLogger("modular_hedge_runtime.order_manager")
logger.setLevel(logging.INFO)


@dataclass
class OrderPayload:
    category: str
    symbol: str
    side: str
    order_type: str
    price: float | None = None
    qty: float | None = None
    reduce_only: bool = False
    position_idx: int | None = None
    order_link_id: str | None = None
    trigger_price: float | None = None
    trigger_direction: int | None = None
    trigger_by: str | None = None
    order_filter: str | None = None

    def to_json(self) -> str:
        body = {
            "category": self.category,
            "symbol": self.symbol,
            "side": self.side,
            "orderType": self.order_type,
            "qty": f"{self.qty}",
        }
        if self.price is not None:
            body["price"] = f"{self.price}"
        if self.reduce_only:
            body["reduceOnly"] = True
        if self.position_idx is not None:
            body["positionIdx"] = self.position_idx
        if self.order_link_id:
            body["orderLinkId"] = self.order_link_id
        if self.trigger_price is not None:
            body["triggerPrice"] = f"{self.trigger_price}"
        if self.trigger_direction is not None:
            body["triggerDirection"] = self.trigger_direction
        if self.trigger_by:
            body["triggerBy"] = self.trigger_by
        if self.order_filter:
            body["orderFilter"] = self.order_filter
        return json.dumps(body)


class BybitOrderManager:
    def __init__(self, api_key: str, secret_key: str, base_url: str = "https://api.bybit.com") -> None:
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self.recv_window = "20000"
        self._session = requests.Session()
        self._instrument_cache: dict[tuple[str, str], Mapping[str, Any]] = {}
        self._max_leverage_cache: set[tuple[str, str]] = set()
        self.last_post_error: dict[str, Any] | None = None

    def _sign(self, body: str) -> str:
        timestamp = str(int(time.time() * 1_000))
        payload = timestamp + self.api_key + self.recv_window + body
        signature = hmac.new(
            self.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return signature, timestamp

    def _post(
        self,
        path: str,
        body: str,
        timeout: int = 10,
        *,
        allow_ret_codes: tuple[int, ...] | None = None,
    ) -> Mapping[str, Any] | None:
        signature, timestamp = self._sign(body)
        headers = {
            "Content-Type": "application/json",
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
        }
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.post(url, headers=headers, data=body, timeout=timeout)
            if resp.status_code != 200:
                logger.warning("Bybit POST %s failed %s – %s", path, resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            ret_code = data.get("retCode") or 0
            allowed_ret = {0} if allow_ret_codes is None else set(allow_ret_codes)
            if ret_code not in allowed_ret:
                logger.warning(
                    "Bybit POST %s returned %s %s", path, ret_code, data.get("retMsg")
                )
                self.last_post_error = {
                    "path": path,
                    "retCode": ret_code,
                    "retMsg": data.get("retMsg"),
                    "payload": data,
                }
                return None
            self.last_post_error = None
            return data
        except Exception as exc:
            logger.exception("Bybit POST %s exception", path, exc_info=exc)
            return None

    def _get(self, path: str, params: Mapping[str, Any], timeout: int = 10) -> Mapping[str, Any] | None:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        signature, timestamp = self._sign(query)
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-SIGN-TYPE": "2",
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
        }
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, headers=headers, params=params, timeout=timeout)
            if resp.status_code != 200:
                logger.warning("Bybit GET %s failed %s – %s", path, resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            if data.get("retCode") != 0:
                logger.warning(
                    "Bybit GET %s returned %s %s", path, data.get("retCode"), data.get("retMsg")
                )
                return None
            return data
        except Exception as exc:
            logger.exception("Bybit GET %s exception", path, exc_info=exc)
            return None

    def ensure_hedge_mode(self, symbol: str, category: str = "linear") -> bool:
        logger.info(
            "Ensuring hedge mode",
            extra={"symbol": symbol.upper(), "category": category, "mode": 3},
        )
        body = json.dumps({"symbol": symbol.upper(), "category": category, "mode": 3})
        result = self._post("/v5/position/switch-mode", body)
        logger.info(
            "Hedge mode request finished",
            extra={"symbol": symbol.upper(), "category": category, "success": bool(result)},
        )
        return bool(result)

    def set_leverage(
        self,
        symbol: str,
        buy_leverage: int | float | str,
        sell_leverage: int | float | str,
        category: str = "linear",
    ) -> bool:
        logger.info(
            "Submitting leverage update",
            extra={
                "symbol": symbol.upper(),
                "category": category,
                "buy_leverage": str(buy_leverage),
                "sell_leverage": str(sell_leverage),
            },
        )
        body = json.dumps(
            {
                "symbol": symbol.upper(),
                "category": category,
                "buyLeverage": str(buy_leverage),
                "sellLeverage": str(sell_leverage),
            }
        )
        result = self._post("/v5/position/set-leverage", body, allow_ret_codes=(0, 110043))
        logger.info(
            "Leverage update finished",
            extra={
                "symbol": symbol.upper(),
                "category": category,
                "buy_leverage": str(buy_leverage),
                "sell_leverage": str(sell_leverage),
                "success": bool(result),
            },
        )
        return bool(result)

    def _fetch_max_leverage(self, symbol: str, category: str = "linear") -> str | None:
        logger.info(
            "Fetching max leverage from instrument info",
            extra={"symbol": symbol.upper(), "category": category},
        )
        instrument = self.fetch_instrument_info(symbol, category)
        if not instrument:
            logger.warning(
                "Max leverage fetch failed: instrument info missing",
                extra={"symbol": symbol.upper(), "category": category},
            )
            return None
        leverage_filter = instrument.get("leverageFilter") or {}
        max_leverage = leverage_filter.get("maxLeverage")
        if not max_leverage:
            logger.warning("Bybit max leverage missing for %s", symbol.upper())
            return None
        logger.info(
            "Fetched max leverage",
            extra={"symbol": symbol.upper(), "category": category, "max_leverage": str(max_leverage)},
        )
        return str(max_leverage)

    def ensure_max_leverage(self, symbol: str, category: str = "linear") -> bool:
        cache_key = (category, symbol.upper())
        if cache_key in self._max_leverage_cache:
            logger.info(
                "Max leverage already ensured",
                extra={"symbol": symbol.upper(), "category": category},
            )
            return True
        max_leverage = self._fetch_max_leverage(symbol, category)
        if not max_leverage:
            logger.warning(
                "Unable to ensure max leverage",
                extra={"symbol": symbol.upper(), "category": category},
            )
            return False
        max_leverage_decimal = Decimal(str(max_leverage))
        current_leverages = self._current_leverages(symbol, category)
        if current_leverages:
            leverage_set = set(current_leverages)
            if len(leverage_set) == 1 and next(iter(leverage_set)) == max_leverage_decimal:
                logger.info(
                    "Max leverage already set on all positions",
                    extra={
                        "symbol": symbol.upper(),
                        "category": category,
                        "max_leverage": str(max_leverage_decimal),
                        "current_leverage": str(max_leverage_decimal),
                    },
                )
                self._max_leverage_cache.add(cache_key)
                return True
            logger.info(
                "Leverage entries differ from target",
                extra={
                    "symbol": symbol.upper(),
                    "category": category,
                    "requested": str(max_leverage_decimal),
                    "current_values": [str(value) for value in sorted(leverage_set)],
                },
            )
        logger.info(
            "Setting exchange leverage to max for %s",
            symbol.upper(),
            extra={"symbol": symbol.upper(), "category": category, "max_leverage": max_leverage},
        )
        if not self.set_leverage(symbol, max_leverage, max_leverage, category):
            logger.warning(
                "Setting max leverage failed",
                extra={"symbol": symbol.upper(), "category": category, "max_leverage": max_leverage},
            )
            return False
        self._max_leverage_cache.add(cache_key)
        logger.info(
            "Max leverage ensured and cached",
            extra={"symbol": symbol.upper(), "category": category, "max_leverage": max_leverage},
        )
        return True

    def _current_leverages(self, symbol: str, category: str = "linear") -> list[Decimal]:
        positions = self.fetch_positions(symbol=symbol, category=category)
        decimal_leverages: list[Decimal] = []
        for pos in positions:
            leverage = pos.get("leverage")
            if leverage is None:
                continue
            try:
                decimal_leverages.append(Decimal(str(leverage)))
            except InvalidOperation:
                continue
        return decimal_leverages

    def place_limit_order(self, payload: OrderPayload) -> Mapping[str, Any] | None:
        return self._post("/v5/order/create", payload.to_json())

    def place_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        price: float | None,
        position_idx: int,
        category: str = "linear",
        order_link_id: str | None = None,
    ) -> Mapping[str, Any] | None:
        normalized_qty = self.normalize_qty(symbol, qty, category)
        if normalized_qty <= 0:
            logger.warning("Bybit market order skipped due to zero qty for %s", symbol.upper())
            return None
        body = {
            "category": category,
            "symbol": symbol.upper(),
            "side": side,
            "orderType": "Market",
            "qty": f"{normalized_qty}",
            "positionIdx": position_idx,
            "timeInForce": "IOC",
        }
        if order_link_id:
            body["orderLinkId"] = order_link_id
        return self._post("/v5/order/create", json.dumps(body))

    def place_reduce_market_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        position_idx: int,
        category: str = "linear",
        order_link_id: str | None = None,
        trigger_price: float | None = None,
        trigger_direction: int | None = None,
        trigger_by: str | None = None,
        close_on_trigger: bool = False,
    ) -> Mapping[str, Any] | None:
        normalized_qty = self.normalize_qty(symbol, qty, category)
        if normalized_qty <= 0:
            logger.warning("Bybit reduce market order skipped due to zero qty for %s", symbol.upper())
            return None
        body = {
            "category": category,
            "symbol": symbol.upper(),
            "side": side,
            "orderType": "Market",
            "qty": f"{normalized_qty}",
            "positionIdx": position_idx,
            "reduceOnly": True,
            "timeInForce": "IOC",
        }
        if order_link_id:
            body["orderLinkId"] = order_link_id
        if trigger_price is not None:
            body["triggerPrice"] = f"{trigger_price}"
        if trigger_direction is not None:
            body["triggerDirection"] = trigger_direction
        if trigger_by:
            body["triggerBy"] = trigger_by
        if close_on_trigger:
            body["closeOnTrigger"] = True
        return self._post("/v5/order/create", json.dumps(body))

    def cancel_order(
        self,
        order_id: str,
        *,
        symbol: str | None = None,
        category: str = "linear",
    ) -> bool:
        payload: dict[str, Any] = {"orderId": order_id, "category": category}
        if symbol:
            payload["symbol"] = symbol.upper()
        result = self._post("/v5/order/cancel", json.dumps(payload))
        return bool(result)

    def cancel_all_orders(self, *, symbol: str, category: str = "linear") -> bool:
        payload = {"category": category, "symbol": symbol.upper()}
        result = self._post("/v5/order/cancel-all", json.dumps(payload))
        return bool(result)

    def set_take_profit(self, symbol: str, tp_price: float, position_idx: int = 1) -> Mapping[str, Any] | None:
        return self.set_position_trading_stop(symbol=symbol, position_idx=position_idx, take_profit=tp_price)

    def _set_partial_position_exit(
        self,
        *,
        symbol: str,
        position_idx: int,
        category: str,
        trigger_field: str,
        trigger_price: float,
        size_field: str,
        position_size: float,
        trigger_by_field: str,
        trigger_by: str,
        order_type_field: str,
        side: str,
    ) -> Mapping[str, Any] | None:
        normalized_qty = self.normalize_qty(symbol, position_size, category)
        if normalized_qty <= 0 or trigger_price <= 0:
            return None
        payload = {
            "category": category,
            "symbol": symbol.upper(),
            "positionIdx": position_idx,
            trigger_field: f"{trigger_price}",
            size_field: f"{normalized_qty}",
            trigger_by_field: trigger_by,
            "tpslMode": "Partial",
            order_type_field: "Market",
            "reduceOnly": True,
            "closeOnTrigger": True,
            "side": side,
        }
        return self._post("/v5/position/trading-stop", json.dumps(payload))

    def _clear_partial_position_exit(
        self,
        *,
        symbol: str,
        position_idx: int,
        category: str,
        field_name: str,
    ) -> Mapping[str, Any] | None:
        payload = {"category": category, "symbol": symbol.upper(), "positionIdx": position_idx, field_name: ""}
        return self._post("/v5/position/trading-stop", json.dumps(payload))

    def set_long_take_profit(
        self,
        *,
        symbol: str,
        tp_price: float,
        position_size: float,
        position_idx: int = 1,
        category: str = "linear",
        trigger_by: str = "MarkPrice",
    ) -> Mapping[str, Any] | None:
        return self._set_partial_position_exit(
            symbol=symbol,
            position_idx=position_idx,
            category=category,
            trigger_field="takeProfit",
            trigger_price=tp_price,
            size_field="tpSize",
            position_size=position_size,
            trigger_by_field="tpTriggerBy",
            trigger_by=trigger_by,
            order_type_field="tpOrderType",
            side="Sell",
        )

    def set_short_stop_loss(
        self,
        *,
        symbol: str,
        sl_price: float,
        position_size: float,
        position_idx: int = 2,
        category: str = "linear",
        trigger_by: str = "MarkPrice",
    ) -> Mapping[str, Any] | None:
        return self._set_partial_position_exit(
            symbol=symbol,
            position_idx=position_idx,
            category=category,
            trigger_field="stopLoss",
            trigger_price=sl_price,
            size_field="slSize",
            position_size=position_size,
            trigger_by_field="slTriggerBy",
            trigger_by=trigger_by,
            order_type_field="slOrderType",
            side="Buy",
        )

    def clear_long_take_profit(
        self,
        *,
        symbol: str,
        position_idx: int = 1,
        category: str = "linear",
    ) -> Mapping[str, Any] | None:
        return self._clear_partial_position_exit(
            symbol=symbol,
            position_idx=position_idx,
            category=category,
            field_name="takeProfit",
        )

    def clear_short_stop_loss(
        self,
        *,
        symbol: str,
        position_idx: int = 2,
        category: str = "linear",
    ) -> Mapping[str, Any] | None:
        return self._clear_partial_position_exit(
            symbol=symbol,
            position_idx=position_idx,
            category=category,
            field_name="stopLoss",
        )

    def set_position_trading_stop(
        self,
        *,
        symbol: str,
        position_idx: int,
        category: str = "linear",
        take_profit: float | None = None,
        stop_loss: float | None = None,
    ) -> Mapping[str, Any] | None:
        payload = {"symbol": symbol.upper(), "positionIdx": position_idx, "category": category}
        if take_profit is not None:
            payload["tpPrice"] = f"{take_profit}"
            payload["tpTriggerBy"] = "MarkPrice"
        if stop_loss is not None:
            payload["slPrice"] = f"{stop_loss}"
            payload["slTriggerBy"] = "MarkPrice"
        payload["triggerBy"] = "MarkPrice"
        return self._post("/v5/position/trading-stop", json.dumps(payload))

    def fetch_open_orders(
        self,
        symbol: str | None = None,
        category: str = "linear",
        settle_coin: str | None = None,
    ) -> list[Mapping[str, Any]] | None:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol.upper()
        elif settle_coin:
            params["settleCoin"] = settle_coin.upper()
        data = self._get("/v5/order/realtime", params)
        if not data:
            return None
        result = data.get("result") or {}
        return result.get("list", []) or result.get("data", []) or []

    def fetch_order_history(
        self,
        symbol: str | None = None,
        category: str = "linear",
        *,
        order_id: str | None = None,
        order_link_id: str | None = None,
        limit: int = 20,
    ) -> list[Mapping[str, Any]] | None:
        params: dict[str, Any] = {"category": category, "limit": limit}
        if symbol:
            params["symbol"] = symbol.upper()
        if order_id:
            params["orderId"] = order_id
        if order_link_id:
            params["orderLinkId"] = order_link_id
        data = self._get("/v5/order/history", params)
        if not data:
            return None
        result = data.get("result") or {}
        return result.get("list", []) or result.get("data", []) or []

    def fetch_positions(
        self,
        symbol: str | None = None,
        category: str = "linear",
        settle_coin: str | None = None,
    ) -> list[Mapping[str, Any]]:
        params: dict[str, Any] = {"category": category}
        if symbol:
            params["symbol"] = symbol.upper()
        elif settle_coin:
            params["settleCoin"] = settle_coin.upper()
        data = self._get("/v5/position/list", params)
        if not data:
            return []
        result = data.get("result") or {}
        return result.get("list", []) or []

    def fetch_instrument_info(self, symbol: str, category: str = "linear") -> Mapping[str, Any] | None:
        cache_key = (category, symbol.upper())
        cached = self._instrument_cache.get(cache_key)
        if cached:
            return cached
        data = self._get("/v5/market/instruments-info", {"category": category, "symbol": symbol.upper()})
        if not data:
            return None
        result = data.get("result") or {}
        instruments = result.get("list") or []
        if not instruments:
            logger.warning("Bybit instrument info missing for %s", symbol.upper())
            return None
        instrument = instruments[0]
        self._instrument_cache[cache_key] = instrument
        return instrument

    def fetch_mark_price(self, symbol: str, category: str = "linear") -> float | None:
        data = self._get("/v5/market/tickers", {"symbol": symbol.upper(), "category": category})
        if not data:
            return None
        result = data.get("result") or {}
        tickers = result.get("list") or []
        if not tickers:
            return None
        ticker = tickers[0]
        price = ticker.get("markPrice") or ticker.get("lastPrice")
        try:
            return float(price)
        except (TypeError, ValueError):
            return None

    def normalize_qty(self, symbol: str, qty: float, category: str = "linear") -> float:
        if qty <= 0:
            return 0.0
        instrument = self.fetch_instrument_info(symbol, category)
        if not instrument:
            return qty
        lot_size_filter = instrument.get("lotSizeFilter") or {}
        qty_step = Decimal(str(lot_size_filter.get("qtyStep") or "0"))
        min_order_qty = Decimal(str(lot_size_filter.get("minOrderQty") or "0"))
        max_order_qty = Decimal(str(lot_size_filter.get("maxOrderQty") or "0"))
        requested = Decimal(str(qty))

        normalized = requested
        if qty_step > 0:
            normalized = (requested / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step

        if normalized <= 0 and min_order_qty > 0:
            normalized = min_order_qty
        elif min_order_qty > 0 and normalized < min_order_qty:
            normalized = min_order_qty

        if max_order_qty > 0 and normalized > max_order_qty:
            normalized = max_order_qty
            if qty_step > 0:
                normalized = (normalized / qty_step).to_integral_value(rounding=ROUND_DOWN) * qty_step

        return float(normalized)
