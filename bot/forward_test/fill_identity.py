"""One fill identity for backtest and forward signals.

A trade is the entry bar plus stop and take-profit. Rank, cluster id, and a
one-tick entry-price difference do not make a second signal.
"""

from __future__ import annotations

from typing import Any


def _price_key(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return round(float(value), 6)


def fill_key(
    *,
    symbol: str = "",
    side: str = "",
    entry_ts: str | None = None,
    stop_price: Any = None,
    tp_price: Any = None,
) -> tuple[str, str, str, float | None, float | None]:
    return (
        str(symbol or "").upper(),
        str(side or ""),
        str(entry_ts or ""),
        _price_key(stop_price),
        _price_key(tp_price),
    )


def fill_key_text(**kwargs: Any) -> str:
    return "|".join("" if part is None else str(part) for part in fill_key(**kwargs))
