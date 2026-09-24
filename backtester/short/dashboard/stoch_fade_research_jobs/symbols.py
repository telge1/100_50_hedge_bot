from __future__ import annotations

from .config import MAX_SYMBOLS


def validate_symbols(raw: list[str], allowed: list[str]) -> tuple[list[str] | None, str | None]:
    if not isinstance(raw, list) or not raw:
        return None, "EMPTY_SYMBOLS"
    if len(raw) > MAX_SYMBOLS:
        return None, "TOO_MANY_SYMBOLS"
    allowed_set = set(allowed)
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            return None, "INVALID_SYMBOL"
        symbol = item.strip().upper()
        if symbol in {"ALL", "*", "51"}:
            return None, "FULL_51_TOKEN_FORBIDDEN"
        if "," in item or " " in item.strip():
            return None, "MULTI_SYMBOL_TOKEN"
        if not symbol.isascii() or not symbol.isalnum() or not symbol.endswith("USDT"):
            return None, "UNKNOWN_SYMBOL"
        if symbol not in allowed_set:
            return None, "UNKNOWN_SYMBOL"
        if symbol in seen:
            return None, "DUPLICATE_SYMBOLS"
        seen.add(symbol)
        cleaned.append(symbol)
    ordered = [s for s in allowed if s in seen]
    return ordered, None


def filter_testable(symbols: list[str], coins: list[dict]) -> tuple[list[str] | None, str | None]:
    by = {c.get("symbol"): c for c in coins}
    out = []
    for symbol in symbols:
        coin = by.get(symbol)
        if coin is None:
            return None, "UNKNOWN_SYMBOL"
        status = str(coin.get("coverage_status") or "")
        testable = bool(coin.get("testable")) or status in {"FULL", "LISTING_LIMITED"}
        if not testable:
            return None, "SYMBOL_NOT_TESTABLE"
        out.append(symbol)
    if not out:
        return None, "EMPTY_SYMBOLS"
    return out, None
