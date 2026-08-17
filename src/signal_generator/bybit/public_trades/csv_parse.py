"""Parse Bybit historical public-trade CSV rows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

REQUIRED_COLUMNS = (
    "timestamp",
    "symbol",
    "side",
    "size",
    "price",
    "tickDirection",
    "trdMatchID",
)

# Taker aggression, same as Bybit publicTrade WS field S / archive CSV side.
VALID_SIDES = frozenset({"Buy", "Sell"})


class PublicTradeParseError(ValueError):
    """Invalid public-trade CSV row."""


@dataclass(frozen=True, slots=True)
class ParsedPublicTrade:
    trade_ts: datetime
    symbol: str
    side: str
    size: Decimal
    price: Decimal
    notional: Decimal
    trade_id: str
    tick_direction: str
    is_rpi_trade: int
    source_file: str
    source_line: int


def unix_seconds_str_to_utc(value: str | Any) -> datetime:
    """Convert Unix seconds (decimal string) to UTC without float precision loss."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PublicTradeParseError(f"invalid timestamp: {value!r}") from exc
    if d < 0:
        raise PublicTradeParseError(f"negative timestamp: {value!r}")
    # Reject millisecond-looking integers (e.g. 1.7e12) — Bybit archive uses seconds.
    if d >= Decimal("100000000000"):
        raise PublicTradeParseError(f"timestamp looks like milliseconds, not seconds: {value!r}")
    whole = int(d)
    frac = d - Decimal(whole)
    micros = int((frac * Decimal("1000000")).to_integral_value(rounding=ROUND_HALF_UP))
    if micros >= 1_000_000:
        whole += 1
        micros -= 1_000_000
    return datetime.fromtimestamp(whole, tz=timezone.utc).replace(microsecond=int(micros))


def _as_decimal(value: Any, *, field: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PublicTradeParseError(f"invalid {field}: {value!r}") from exc


def parse_rpi_flag(row: Mapping[str, str]) -> int:
    raw = str(row.get("RPI") or "0").strip().lower()
    return 1 if raw in {"1", "true", "yes"} else 0


def parse_csv_trade_row(
    row: Mapping[str, str],
    *,
    expected_symbol: str | None = None,
    source_file: str = "",
    source_line: int = 0,
) -> ParsedPublicTrade:
    missing = [c for c in REQUIRED_COLUMNS if c not in row or row[c] is None]
    if missing:
        raise PublicTradeParseError(
            f"missing columns {missing} at {source_file}:{source_line}"
        )

    symbol = str(row["symbol"]).strip().upper()
    if expected_symbol is not None and symbol != expected_symbol.upper():
        raise PublicTradeParseError(
            f"symbol mismatch: expected {expected_symbol}, got {symbol} "
            f"({source_file}:{source_line})"
        )

    side = str(row["side"]).strip()
    if side not in VALID_SIDES:
        raise PublicTradeParseError(
            f"invalid side={side!r} at {source_file}:{source_line}"
        )

    trade_id = str(row["trdMatchID"]).strip()
    if not trade_id:
        raise PublicTradeParseError(f"empty trdMatchID at {source_file}:{source_line}")

    size = _as_decimal(row["size"], field="size")
    price = _as_decimal(row["price"], field="price")
    if size <= 0:
        raise PublicTradeParseError(f"non-positive size={size} at {source_file}:{source_line}")
    if price <= 0:
        raise PublicTradeParseError(f"non-positive price={price} at {source_file}:{source_line}")

    foreign_raw = str(row.get("foreignNotional") or "").strip()
    computed = price * size
    if foreign_raw:
        notional = _as_decimal(foreign_raw, field="foreignNotional")
        if notional < 0:
            raise PublicTradeParseError(
                f"negative notional={notional} at {source_file}:{source_line}"
            )
    else:
        notional = computed

    return ParsedPublicTrade(
        trade_ts=unix_seconds_str_to_utc(row["timestamp"]),
        symbol=symbol,
        side=side,
        size=size,
        price=price,
        notional=notional,
        trade_id=trade_id,
        tick_direction=str(row.get("tickDirection") or "").strip(),
        is_rpi_trade=parse_rpi_flag(row),
        source_file=source_file,
        source_line=source_line,
    )
