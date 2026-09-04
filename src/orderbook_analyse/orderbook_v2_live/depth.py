"""Depth validation and topic helpers for OB200 / OB1000."""

from __future__ import annotations

SUPPORTED_DEPTHS: frozenset[int] = frozenset({200, 1000})
DEFAULT_DEPTH = 200


class UnsupportedDepthError(ValueError):
    pass


def validate_depth(depth: int) -> int:
    d = int(depth)
    if d not in SUPPORTED_DEPTHS:
        raise UnsupportedDepthError(f"unsupported depth {d}; allowed: {sorted(SUPPORTED_DEPTHS)}")
    return d


def orderbook_topic(symbol: str, depth: int = DEFAULT_DEPTH) -> str:
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("empty symbol")
    d = validate_depth(depth)
    return f"orderbook.{d}.{sym}"


def parse_orderbook_topic(topic: str) -> tuple[str, int] | None:
    parts = str(topic or "").split(".")
    if len(parts) != 3 or parts[0] != "orderbook":
        return None
    try:
        depth = validate_depth(int(parts[1]))
    except (TypeError, ValueError, UnsupportedDepthError):
        return None
    sym = parts[2].strip().upper()
    if not sym:
        return None
    return sym, depth
