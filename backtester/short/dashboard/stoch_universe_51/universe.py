"""Load the tradeable-51 universe. Uppercase only; never drop or remap symbols."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_tradeable_51(path: Path) -> list[str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"universe file must be an object: {path}")
    symbols_raw = raw.get("symbols")
    if not isinstance(symbols_raw, list) or not symbols_raw:
        raise ValueError(f"universe file has empty symbols: {path}")
    out: list[str] = []
    for item in symbols_raw:
        text = str(item).strip().upper()
        if not text:
            raise ValueError(f"blank symbol in universe file: {path}")
        out.append(text)
    return out


def universe_meta(path: Path) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    symbols = load_tradeable_51(path)
    return {
        "path": str(path),
        "target_size": raw.get("target_size") if isinstance(raw, dict) else None,
        "count": len(symbols),
        "symbols": symbols,
    }
