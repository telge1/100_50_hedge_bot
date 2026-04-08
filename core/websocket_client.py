import asyncio
import json
import websockets
import time
import hmac
import hashlib
import threading
import logging
import os
from typing import Callable, Optional, Any

logger = logging.getLogger("WebSocketClient")
logger.setLevel(logging.INFO)

ws_url = "wss://stream.bybit.com/v5/private"


class BybitWebSocketClient:
    def __init__(self, api_key: str, api_secret: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.ws_url = ws_url
        self.callbacks: list[Callable] = []
        self.pong_callback: Callable | None = None
        self.on_fill_callback: Callable[..., None] | None = None
        self.running = True
        self.last_message_time = time.time()
        self.reconnect_attempts = 0
        self.max_reconnect_delay = 60

    def add_callback(self, callback: Callable) -> None:
        self.callbacks.append(callback)

    def set_pong_callback(self, callback: Callable) -> None:
        self.pong_callback = callback

    def set_fill_callback(self, callback: Callable[..., None]) -> None:
        self.on_fill_callback = callback

    async def send_heartbeat(self, ws: websockets.WebSocketClientProtocol) -> None:
        while self.running:
            try:
                ping_msg = {
                    "req_id": str(int(time.time() * 1000)),
                    "op": "ping",
                }
                await ws.send(json.dumps(ping_msg))
                await asyncio.sleep(30)
            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket closed, stopping heartbeat.")
                break
            except Exception as exc:
                logger.error("Heartbeat error: %s", exc, exc_info=True)
                break

    async def connect_and_subscribe(self) -> None:
        while self.running:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.reconnect_attempts = 0
                    logger.info("WebSocket connected, authenticating...")
                    await self.authenticate(ws)
                    asyncio.create_task(self.send_heartbeat(ws))
                    await self.subscribe_and_listen(ws)
            except websockets.exceptions.ConnectionClosed:
                await self._schedule_reconnect("connection closed")
            except Exception as exc:
                logger.error("WebSocket error: %s", exc, exc_info=True)
                await self._schedule_reconnect("unexpected error")

    async def _schedule_reconnect(self, reason: str) -> None:
        self.reconnect_attempts += 1
        delay = min(5 * (2 ** (self.reconnect_attempts - 1)), self.max_reconnect_delay)
        logger.warning(
            "WebSocket %s. Reconnecting in %s seconds (attempt %s).",
            reason,
            delay,
            self.reconnect_attempts,
        )
        await asyncio.sleep(delay)

    async def authenticate(self, ws: websockets.WebSocketClientProtocol) -> None:
        expires = int((time.time() + 1) * 1000)
        signature = hmac.new(
            bytes(self.api_secret, "utf-8"),
            bytes(f"GET/realtime{expires}", "utf-8"),
            hashlib.sha256,
        ).hexdigest()
        auth_msg = {
            "op": "auth",
            "args": [self.api_key, expires, signature],
        }
        await ws.send(json.dumps(auth_msg))
        response = await ws.recv()
        logger.info("WebSocket authenticated, response: %s", response)

    async def subscribe_and_listen(self, ws: websockets.WebSocketClientProtocol) -> None:
        sub_msg = {
            "op": "subscribe",
            "args": ["order", "stop_order", "position", "execution"],
        }
        await ws.send(json.dumps(sub_msg))
        logger.info("Subscribed to order, stop_order, position and execution channels.")
        await self.listen(ws)

    async def listen(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for message in ws:
            if not self.running:
                break
            try:
                data = json.loads(message)
                self.last_message_time = time.time()
                if "topic" in data:
                    topic = data.get("topic", "")
                    if topic:
                        logger.debug("WS topic=%s", topic)
                        self.process_message(data)
                elif data.get("op") == "pong":
                    logger.debug("Received pong")
                    if self.pong_callback:
                        try:
                            self.pong_callback()
                        except Exception as exc:
                            logger.error("Pong callback error: %s", exc, exc_info=True)
                else:
                    self.process_message(data)
            except json.JSONDecodeError as exc:
                logger.error("JSON decode error: %s", exc)
            except Exception as exc:
                logger.error("Error processing WS message: %s", exc, exc_info=True)

    def process_message(self, data: dict[str, Any]) -> None:
        topic = data.get("topic", "")
        payload = data.get("data", [])
        if not payload:
            return
        logger.debug("Processing message topic=%s count=%s", topic, len(payload))
        for item in payload:
            if topic in {"order", "stop_order"}:
                self.process_order_message(item)
            elif topic == "execution":
                self.process_execution_message(item)
            elif topic == "position":
                self.process_position_message(item)
        for callback in self.callbacks:
            try:
                callback(topic, payload)
            except Exception as exc:
                logger.error("Callback error: %s", exc, exc_info=True)

    def process_position_message(self, item: dict[str, Any]) -> None:
        symbol = item.get("symbol", "N/A")
        side = item.get("side", "")
        size = item.get("size", "0")
        entry_price = item.get("entryPrice", "0")
        position_idx = self._safe_int(item.get("positionIdx"))
        take_profit = item.get("takeProfit", "0")
        stop_loss = item.get("stopLoss", "0")
        position_data = {
            "symbol": symbol,
            "side": side,
            "size": size,
            "entryPrice": entry_price,
            "positionIdx": position_idx,
            "takeProfit": take_profit,
            "stopLoss": stop_loss,
        }
        logger.info(
            "WS position update: symbol=%s side=%s size=%s entry=%s positionIdx=%s",
            symbol,
            side,
            size,
            entry_price,
            position_idx,
        )
        for callback in self.callbacks:
            try:
                callback("position", position_data)
            except Exception as exc:
                logger.error("Position callback error: %s", exc, exc_info=True)

    def process_order_message(self, item: dict[str, Any]) -> None:
        order_id = item.get("orderId", "N/A")
        symbol = item.get("symbol", "N/A")
        order_status = item.get("orderStatus", "N/A")
        stop_order_type = item.get("stopOrderType", "N/A")
        qty = item.get("qty", "N/A")
        price = item.get("price", "N/A")
        trigger_price = item.get("triggerPrice", "N/A")
        side = item.get("side", "N/A")
        order_type = item.get("orderType", "N/A")
        last_price_on_created = item.get("lastPriceOnCreated", "N/A")
        reject_reason = item.get("rejectReason", "N/A")
        cancel_type = item.get("cancelType", "N/A")
        position_idx = self._safe_int(item.get("positionIdx"))
        logger.info(
            "WS order update: id=%s symbol=%s status=%s side=%s type=%s",
            order_id,
            symbol,
            order_status,
            side,
            order_type,
        )
        order_data = {
            "orderId": order_id,
            "symbol": symbol,
            "orderStatus": order_status,
            "stopOrderType": stop_order_type,
            "qty": qty,
            "price": price,
            "triggerPrice": trigger_price,
            "side": side,
            "orderType": order_type,
            "lastPriceOnCreated": last_price_on_created,
            "positionIdx": position_idx,
            "rejectReason": reject_reason,
            "cancelType": cancel_type,
        }
        for callback in self.callbacks:
            try:
                callback("order", order_data)
            except Exception as exc:
                logger.error("Order callback error: %s", exc, exc_info=True)

    def process_execution_message(self, item: dict[str, Any]) -> None:
        symbol = item.get("symbol", "N/A")
        side = item.get("side", "N/A")
        order_id = item.get("orderId")
        order_link_id = item.get("orderLinkId")
        exec_id = item.get("execId")
        exec_qty = self._safe_float(item.get("execQty"))
        exec_price = self._safe_float(item.get("execPrice"))
        cumulative_qty = self._safe_float(
            item.get("cumExecQty")
            or item.get("cumFilledQty")
            or item.get("cumFilledOrderQty")
        )
        logger.info(
            "WS execution update: symbol=%s side=%s orderId=%s orderLinkId=%s execId=%s execQty=%s execPrice=%s",
            symbol,
            side,
            order_id,
            order_link_id,
            exec_id,
            exec_qty,
            exec_price,
        )
        if not self.on_fill_callback:
            return
        if exec_qty <= 0 or exec_price <= 0:
            return
        fill_order_id = order_id or order_link_id
        if not fill_order_id:
            logger.warning(
                "WS execution ignored without order identifier: symbol=%s execId=%s",
                symbol,
                exec_id,
            )
            return
        kwargs = {
            "exec_id": exec_id,
            "cumulative_qty": cumulative_qty if cumulative_qty > 0 else None,
            "order_link_id": order_link_id,
            "order_side": side,
        }
        try:
            self.on_fill_callback(fill_order_id, exec_qty, exec_price, **kwargs)
        except TypeError as exc:
            if "order_side" in str(exc):
                kwargs.pop("order_side", None)
                try:
                    self.on_fill_callback(fill_order_id, exec_qty, exec_price, **kwargs)
                    return
                except Exception as exc_inner:
                    logger.error("Execution fill callback error: %s", exc_inner, exc_info=True)
                    return
            logger.error("Execution fill callback error: %s", exc, exc_info=True)
        except Exception as exc:
            logger.error("Execution fill callback error: %s", exc, exc_info=True)

    @staticmethod
    def _safe_float(value: Optional[str]) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_int(value: Optional[str]) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def run(self) -> None:
        asyncio.run(self.connect_and_subscribe())

    def stop(self) -> None:
        self.running = False

    def get_last_activity_time(self) -> float:
        return self.last_message_time

    def is_healthy(self, max_inactivity_seconds: int = 120) -> bool:
        if not self.running:
            return False
        return (time.time() - self.last_message_time) < max_inactivity_seconds


_websocket_client_instance: Optional[BybitWebSocketClient] = None


def start_websocket(
    callback: Callable,
    api_key: str = None,
    api_secret: str = None,
    health_file: str | None = None,
    pong_callback: Callable | None = None,
    fill_callback: Callable[[str, float, float], None] | None = None,
) -> None:
    global _websocket_client_instance
    if api_key is None or api_secret is None:
        raise ValueError("api_key und api_secret müssen übergeben werden")
    client = BybitWebSocketClient(api_key, api_secret)
    client.add_callback(callback)
    if pong_callback:
        client.set_pong_callback(pong_callback)
    if fill_callback:
        client.set_fill_callback(fill_callback)
    _websocket_client_instance = client
    if health_file:
        health_thread = threading.Thread(
            target=write_health_status, args=(client, health_file), daemon=True
        )
        health_thread.start()
        logger.info("Health-check thread started for %s", health_file)
    client.run()


def write_health_status(client: BybitWebSocketClient, health_file: str) -> None:
    import json

    while client.running:
        try:
            health_data = {
                "last_activity_time": client.last_message_time,
                "is_healthy": client.is_healthy(120),
                "running": client.running,
                "reconnect_attempts": client.reconnect_attempts,
                "timestamp": time.time(),
            }
            temp_file = f"{health_file}.tmp"
            with open(temp_file, "w") as fh:
                json.dump(health_data, fh, indent=2)
            os.replace(temp_file, health_file)
        except Exception as exc:
            logger.error("Health write failed: %s", exc, exc_info=True)
        time.sleep(10)


def get_websocket_client() -> Optional[BybitWebSocketClient]:
    return _websocket_client_instance
