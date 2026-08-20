"""Parse Bybit historical public-trade CSV rows into NormalizedPublicTrade."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Mapping

from orderbook_analyse.public_trade_source.protocol import NormalizedPublicTrade

REQUIRED_COLUMNS = (
    "timestamp",
    "symbol",
    "side",
    "size",
    "price",
    "tickDirection",
    "trdMatchID",
    "foreignNotional",
)


class PublicTradeParseError(ValueError):
    """Invalid public-trade CSV row."""


def unix_seconds_str_to_utc(value: str | Any) -> datetime:
    """Convert Unix seconds (decimal string) to UTC without float precision loss."""
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PublicTradeParseError(f"invalid timestamp: {value!r}") from exc
    if d < 0:
        raise PublicTradeParseError(f"negative timestamp: {value!r}")
    whole = int(d)
    frac = d - Decimal(whole)
    # round to nearest microsecond
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


def parse_csv_trade_row(
    row: Mapping[str, str],
    *,
    expected_symbol: str | None = None,
    source: str = "files",
    source_file: str = "",
    source_line: int = 0,
    notional_rel_tol: Decimal = Decimal("1e-6"),
) -> NormalizedPublicTrade:
    missing = [c for c in REQUIRED_COLUMNS if c not in row or row[c] is None]
    if missing:
        raise PublicTradeParseError(f"missing columns {missing} at {source_file}:{source_line}")

    symbol = str(row["symbol"]).strip()
    if expected_symbol is not None and symbol != expected_symbol:
        raise PublicTradeParseError(
            f"symbol mismatch: expected {expected_symbol}, got {symbol} "
            f"({source_file}:{source_line})"
        )

    side = str(row["side"]).strip()
    if side not in ("Buy", "Sell"):
        raise PublicTradeParseError(
            f"invalid side={side!r} at {source_file}:{source_line}"
        )

    trade_ts = unix_seconds_str_to_utc(row["timestamp"])
    size = _as_decimal(row["size"], field="size")
    price = _as_decimal(row["price"], field="price")
    trade_id = str(row["trdMatchID"]).strip()
    if not trade_id:
        raise PublicTradeParseError(f"empty trdMatchID at {source_file}:{source_line}")
    tick_direction = str(row.get("tickDirection") or "").strip()

    computed = price * size
    foreign_raw = str(row.get("foreignNotional") or "").strip()
    notional_source = "price_times_size"
    notional = computed
    mismatch = False
    if foreign_raw != "":
        try:
            foreign = _as_decimal(foreign_raw, field="foreignNotional")
            notional = foreign
            notional_source = "foreignNotional"
            if computed != 0:
                rel = abs(foreign - computed) / abs(computed)
            else:
                rel = abs(foreign - computed)
            if rel > notional_rel_tol:
                mismatch = True
        except PublicTradeParseError:
            # fall back to price*size
            notional = computed
            notional_source = "price_times_size"

    return NormalizedPublicTrade(
        trade_ts=trade_ts,
        symbol=symbol,
        side=side,
        size=size,
        price=price,
        notional=notional,
        trade_id=trade_id,
        tick_direction=tick_direction,
        source=source,
        source_file=source_file,
        source_line=source_line,
        notional_source=notional_source,
        notional_mismatch=mismatch,
    )
