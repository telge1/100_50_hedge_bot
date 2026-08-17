"""Parse Bybit publicTrade WebSocket payloads (field ``i`` = trade_id)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from signal_generator.bybit.history import millis_to_utc
from signal_generator.bybit.public_trades.csv_parse import VALID_SIDES

LIVE_SOURCE = "live"


@dataclass(frozen=True, slots=True)
class WsPublicTrade:
    symbol: str
    trade_id: str
    trade_ts: datetime
    side: str
    price: Decimal
    size: Decimal
    notional: Decimal
    tick_direction: str
    is_rpi_trade: int
    source_file: str = "bybit_ws"


def public_trade_topic_for_symbol(symbol: str) -> str:
    return f"publicTrade.{symbol.upper()}"


def parse_public_trade_topic(topic: str) -> str | None:
    parts = str(topic).split(".", 1)
    if len(parts) == 2 and parts[0] == "publicTrade":
        return parts[1].upper()
    return None


def parse_ws_public_trade_item(
    item: Mapping[str, Any],
    *,
    symbol: str | None = None,
    source_file: str = "bybit_ws",
) -> WsPublicTrade | None:
    trade_id = str(item.get("i") or "").strip()
    if not trade_id:
        return None
    sym = str(item.get("s") or symbol or "").upper()
    if not sym:
        return None
    side = str(item.get("S") or "").strip()
    if side not in VALID_SIDES:
        return None
    ts_ms = item.get("T")
    if ts_ms is None:
        return None
    trade_ts = millis_to_utc(int(ts_ms))
    price = Decimal(str(item.get("p")))
    size = Decimal(str(item.get("v")))
    notional = price * size
    tick = str(item.get("L") or "")
    rpi = 1 if bool(item.get("BT")) else 0
    return WsPublicTrade(
        symbol=sym,
        trade_id=trade_id,
        trade_ts=trade_ts,
        side=side,
        price=price,
        size=size,
        notional=notional,
        tick_direction=tick,
        is_rpi_trade=rpi,
        source_file=source_file,
    )


def parse_ws_public_trade_payload(
    payload: Mapping[str, Any],
) -> list[WsPublicTrade]:
    topic = str(payload.get("topic") or "")
    symbol = parse_public_trade_topic(topic)
    if not symbol:
        return []
    out: list[WsPublicTrade] = []
    for item in payload.get("data") or []:
        if not isinstance(item, Mapping):
            continue
        trade = parse_ws_public_trade_item(item, symbol=symbol)
        if trade is not None:
            out.append(trade)
    return out


def ws_trade_to_parsed(trade: WsPublicTrade) -> "ParsedPublicTrade":
    from signal_generator.bybit.public_trades.csv_parse import ParsedPublicTrade

    return ParsedPublicTrade(
        trade_ts=trade.trade_ts,
        symbol=trade.symbol,
        side=trade.side,
        size=trade.size,
        price=trade.price,
        notional=trade.notional,
        trade_id=trade.trade_id,
        tick_direction=trade.tick_direction,
        is_rpi_trade=trade.is_rpi_trade,
        source_file=trade.source_file,
        source_line=0,
    )


def public_trade_topics(symbols: Sequence[str]) -> list[str]:
    return [public_trade_topic_for_symbol(s) for s in symbols]
