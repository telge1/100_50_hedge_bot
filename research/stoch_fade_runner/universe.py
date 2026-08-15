"""Read-only tradeable-51 universe allowlist. No alias mapping. No strategy logic."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import CANARY_SYMBOL, sg_root

UNIVERSE_RELATIVE = Path("config") / "universe_tradeable_51.json"
EXPECTED_UNIVERSE_COUNT = 51
REQUIRED_OBJECT_KEYS = ("generated_at", "selection_method", "source", "symbols", "target_size")

UNIVERSE_FILE_MISSING = "UNIVERSE_FILE_MISSING"
UNIVERSE_INVALID_JSON = "UNIVERSE_INVALID_JSON"
UNIVERSE_INVALID_SCHEMA = "UNIVERSE_INVALID_SCHEMA"
UNIVERSE_DUPLICATE_SYMBOL = "UNIVERSE_DUPLICATE_SYMBOL"
UNIVERSE_COUNT_MISMATCH = "UNIVERSE_COUNT_MISMATCH"
SYMBOL_NOT_ALLOWLISTED = "SYMBOL_NOT_ALLOWLISTED"
MULTI_SYMBOL_FORBIDDEN = "MULTI_SYMBOL_FORBIDDEN"
FULL_51_RUN_FORBIDDEN = "FULL_51_RUN_FORBIDDEN"


class UniverseConfigError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        msg = f"{code}:{detail}" if detail else code
        super().__init__(msg)


def default_universe_path(environ: dict | None = None) -> Path:
    return sg_root(environ) / UNIVERSE_RELATIVE


def load_tradeable_universe(path: Path | None = None, *, environ: dict | None = None) -> dict[str, Any]:
    src = Path(path) if path is not None else default_universe_path(environ)
    if not src.is_file():
        raise UniverseConfigError(UNIVERSE_FILE_MISSING, str(src))
    try:
        raw = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UniverseConfigError(UNIVERSE_INVALID_JSON, str(exc)) from exc
    if not isinstance(raw, dict):
        raise UniverseConfigError(UNIVERSE_INVALID_SCHEMA, "root_not_object")
    missing = [k for k in REQUIRED_OBJECT_KEYS if k not in raw]
    if missing:
        raise UniverseConfigError(UNIVERSE_INVALID_SCHEMA, f"missing_keys:{','.join(missing)}")
    symbols_raw = raw["symbols"]
    if not isinstance(symbols_raw, list):
        raise UniverseConfigError(UNIVERSE_INVALID_SCHEMA, "symbols_not_list")
    if not isinstance(raw["target_size"], int):
        raise UniverseConfigError(UNIVERSE_INVALID_SCHEMA, "target_size_not_int")
    if raw["target_size"] != EXPECTED_UNIVERSE_COUNT:
        raise UniverseConfigError(
            UNIVERSE_COUNT_MISMATCH, f"target_size={raw['target_size']}"
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for item in symbols_raw:
        if not isinstance(item, str):
            raise UniverseConfigError(UNIVERSE_INVALID_SCHEMA, f"symbol_not_string:{item!r}")
        symbol = item.strip().upper()
        if not symbol:
            raise UniverseConfigError(UNIVERSE_INVALID_SCHEMA, "empty_symbol")
        if symbol in seen:
            raise UniverseConfigError(UNIVERSE_DUPLICATE_SYMBOL, symbol)
        seen.add(symbol)
        normalized.append(symbol)
    if len(normalized) != EXPECTED_UNIVERSE_COUNT:
        raise UniverseConfigError(UNIVERSE_COUNT_MISMATCH, f"count={len(normalized)}")
    return {
        "path": str(src.resolve()),
        "generated_at": raw["generated_at"],
        "selection_method": raw["selection_method"],
        "source": raw["source"],
        "target_size": int(raw["target_size"]),
        "symbols": tuple(normalized),
        "allowlist": frozenset(normalized),
        "count": len(normalized),
        "canary_default": CANARY_SYMBOL,
    }


def select_single_cli_symbol(argv: list[str], parsed_symbol: str, allowlist: frozenset[str]) -> str:
    symbol_flags = [a for a in argv if a == "--symbol" or a.startswith("--symbol=")]
    if len(symbol_flags) > 1:
        raise UniverseConfigError(MULTI_SYMBOL_FORBIDDEN, "multiple_--symbol")
    raw = str(parsed_symbol or "").strip()
    if "," in raw or " " in raw:
        raise UniverseConfigError(MULTI_SYMBOL_FORBIDDEN, raw)
    symbol = raw.upper()
    if symbol in {"ALL", "*", "51"}:
        raise UniverseConfigError(FULL_51_RUN_FORBIDDEN, symbol)
    if symbol not in allowlist:
        raise UniverseConfigError(SYMBOL_NOT_ALLOWLISTED, symbol)
    return symbol
