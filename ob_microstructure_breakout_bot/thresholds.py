from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ob_microstructure_breakout_bot.models import CoinThresholds

_CONFIG_DIR = Path(__file__).resolve().parent / "config"

_SYMBOL_FILE = {
    "DOGEUSDT": "doge_usdt.yaml",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    return data


def load_thresholds(symbol: str) -> CoinThresholds:
    key = symbol.upper().replace("/", "")
    filename = _SYMBOL_FILE.get(key)
    if filename is None:
        raise KeyError(
            f"No threshold config for {symbol!r}. "
            f"Known: {sorted(_SYMBOL_FILE)}"
        )
    path = _CONFIG_DIR / filename
    raw = _load_yaml(path)
    return CoinThresholds(
        symbol=str(raw.get("symbol", key)),
        fakeout_max_confirm_delta=float(raw["fakeout_max_confirm_delta"]),
        tier1_min_confirm_delta=float(raw["tier1_min_confirm_delta"]),
        tier2_min_confirm_delta=float(raw["tier2_min_confirm_delta"]),
        tier1_min_bid_ask_ratio_5bps=float(raw["tier1_min_bid_ask_ratio_5bps"]),
        tier2_min_bid_ask_ratio_5bps=float(raw["tier2_min_bid_ask_ratio_5bps"]),
        fakeout_followthrough_flip_delta=float(raw["fakeout_followthrough_flip_delta"]),
        context_lookback_minutes=int(raw.get("context_lookback_minutes", 30)),
        confirm_window_minutes=int(raw.get("confirm_window_minutes", 5)),
        followthrough_candles=int(raw.get("followthrough_candles", 2)),
    )


def list_configured_symbols() -> list[str]:
    return sorted(_SYMBOL_FILE)
