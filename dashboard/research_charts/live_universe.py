"""Read the existing live collector universe. Do not duplicate the symbol list."""

from __future__ import annotations

import json
from pathlib import Path

LIVE_UNIVERSE_PATH = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/"
    "signal_generator_stoch_waves/config/live_universe.json"
)

BTC_SYMBOL = "BTCUSDT"

HISTORY_AVAILABLE_AND_LIVE_CONFIGURED = "HISTORY_AVAILABLE_AND_LIVE_CONFIGURED"
HISTORY_AVAILABLE_BUT_NOT_LIVE_CONFIGURED = "HISTORY_AVAILABLE_BUT_NOT_LIVE_CONFIGURED"
NO_HISTORY = "NO_HISTORY"


def load_live_universe_symbols(path: Path | None = None) -> list[str]:
    target = path or LIVE_UNIVERSE_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))
    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw.get("symbols") or []:
        sym = str(item).strip().upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        ordered.append(sym)
    return ordered


def is_btc_rejected(symbol: str) -> bool:
    return str(symbol or "").strip().upper() == BTC_SYMBOL


def is_live_configured(symbol: str, universe: list[str] | None = None) -> bool:
    """True only if the existing collector may subscribe this symbol."""
    sym = str(symbol or "").strip().upper()
    if not sym or is_btc_rejected(sym):
        return False
    names = universe if universe is not None else load_live_universe_symbols()
    return sym in {s.upper() for s in names}


def classify_live_capability(*, history_available: bool, live_configured: bool) -> str:
    if not history_available:
        return NO_HISTORY
    if live_configured:
        return HISTORY_AVAILABLE_AND_LIVE_CONFIGURED
    return HISTORY_AVAILABLE_BUT_NOT_LIVE_CONFIGURED
