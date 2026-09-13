"""Canonical OB1000 *live* symbol registry (generic pipeline).

Separate from ``contracts.validate_symbol`` / ``ALLOWED_SYMBOLS``, which remain
BTC/DOGE-only for pilot research contracts (windows, phase2 inventories, etc.).

Live OB1000 materializer + add_symbol preflight use this module instead.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Bybit linear USDT perpetual: uppercase alnum base + USDT suffix.
BYBIT_LINEAR_USDT_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

_DEFAULT_OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")


def default_ob1000_live_symbols_path() -> Path:
    env = (os.environ.get("OB1000_SYMBOLS_FILE") or os.environ.get("OB1000_LIVE_SYMBOLS_FILE") or "").strip()
    if env:
        return Path(env)
    return _DEFAULT_OA_ROOT / "config" / "ob1000_live_symbols.json"


def load_ob1000_live_symbols(path: Path | None = None) -> tuple[str, ...]:
    cfg = path or default_ob1000_live_symbols_path()
    if not cfg.is_file():
        raise FileNotFoundError(f"OB1000 live symbols config missing: {cfg}")
    data = json.loads(cfg.read_text(encoding="utf-8"))
    raw = data.get("symbols") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"empty or invalid symbols in {cfg}")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        sym = str(item).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    if not out:
        raise ValueError(f"empty symbols after normalize in {cfg}")
    return tuple(out)


def normalize_bybit_linear_usdt_symbol(symbol: str) -> str:
    """Syntax-only check (no registry membership)."""
    normalized = str(symbol or "").strip().upper()
    if not BYBIT_LINEAR_USDT_SYMBOL_RE.match(normalized):
        raise ValueError(f"invalid Bybit linear USDT symbol syntax: {symbol!r}")
    return normalized


def validate_ob1000_live_symbol(
    symbol: str,
    *,
    registered: frozenset[str] | None = None,
    path: Path | None = None,
) -> str:
    """Accept only safe syntax *and* membership in the OB1000 live registry."""
    normalized = normalize_bybit_linear_usdt_symbol(symbol)
    reg = registered if registered is not None else frozenset(load_ob1000_live_symbols(path))
    if normalized not in reg:
        raise ValueError(f"symbol not registered for OB1000 live: {normalized}")
    return normalized


def partition_ob1000_live_symbols(
    symbols: list[str] | tuple[str, ...],
    *,
    registered: frozenset[str] | None = None,
    path: Path | None = None,
) -> tuple[tuple[str, ...], list[tuple[str, str]]]:
    """Validate each symbol; return (accepted, [(raw_or_norm, error), ...]).

    Invalid symbols are listed explicitly — never silently dropped without a
    rejected entry. Callers may continue with ``accepted`` for failure isolation.
    """
    reg = registered if registered is not None else frozenset(load_ob1000_live_symbols(path))
    accepted: list[str] = []
    rejected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in symbols:
        token = str(raw or "").strip()
        if not token:
            continue
        try:
            ok = validate_ob1000_live_symbol(token, registered=reg)
        except ValueError as exc:
            rejected.append((token.upper(), str(exc)))
            continue
        if ok in seen:
            continue
        seen.add(ok)
        accepted.append(ok)
    return tuple(accepted), rejected
