"""Tick sizes for OB1000 *live* symbols (generic), separate from Phase-2 TICK_SIZE.

Pilot ``phase2_contracts.TICK_SIZE`` stays BTC/DOGE-only. Additional live symbols
read overrides from ``orderbook_analyse/config/ob1000_tick_sizes.json``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from .phase2_contracts import TICK_SIZE

_DEFAULT_OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")


def default_ob1000_tick_sizes_path() -> Path:
    env = (os.environ.get("OB1000_TICK_SIZES_FILE") or "").strip()
    if env:
        return Path(env)
    return _DEFAULT_OA_ROOT / "config" / "ob1000_tick_sizes.json"


@lru_cache(maxsize=1)
def _load_live_tick_map(path_str: str) -> dict[str, Decimal]:
    path = Path(path_str)
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("tick_sizes") if isinstance(data, dict) else data
    if not isinstance(raw, dict):
        raise ValueError(f"invalid tick sizes file: {path}")
    out: dict[str, Decimal] = {}
    for key, value in raw.items():
        sym = str(key).strip().upper()
        out[sym] = Decimal(str(value))
    return out


def clear_ob1000_tick_cache() -> None:
    _load_live_tick_map.cache_clear()


def resolve_ob1000_tick_size(
    symbol: str,
    *,
    path: Path | None = None,
) -> Decimal:
    """Pilot TICK_SIZE first, then OB1000 live tick config."""
    sym = str(symbol).strip().upper()
    if sym in TICK_SIZE:
        return TICK_SIZE[sym]
    cfg = path or default_ob1000_tick_sizes_path()
    live = _load_live_tick_map(str(cfg.resolve()) if cfg.is_file() else str(cfg))
    if sym not in live:
        raise KeyError(
            f"no OB1000 tick size for {sym}; add it to {cfg} "
            "(or ensure it is a pilot symbol in phase2_contracts.TICK_SIZE)"
        )
    return live[sym]


def upsert_ob1000_tick_size(
    symbol: str,
    tick: Decimal | str,
    *,
    path: Path | None = None,
    allow_overwrite: bool = False,
) -> Path:
    """Write tick size. Refuses silent overwrite unless ``allow_overwrite``."""
    cfg = path or default_ob1000_tick_sizes_path()
    sym = str(symbol).strip().upper()
    incoming = Decimal(str(tick))
    data: dict = {"name": "ob1000_tick_sizes", "tick_sizes": {}}
    if cfg.is_file():
        loaded = json.loads(cfg.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
            data.setdefault("tick_sizes", {})
    ticks = data.setdefault("tick_sizes", {})
    if sym in ticks and not allow_overwrite:
        existing = Decimal(str(ticks[sym]))
        if existing != incoming:
            raise ValueError(
                f"tick conflict for {sym}: registered={existing} incoming={incoming}"
            )
    ticks[sym] = format(incoming, "f").rstrip("0").rstrip(".") if "." in format(incoming, "f") else str(incoming)
    # Prefer exact string form from caller when it is already a clean decimal string.
    if isinstance(tick, str) and tick.strip():
        ticks[sym] = tick.strip()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    clear_ob1000_tick_cache()
    return cfg
