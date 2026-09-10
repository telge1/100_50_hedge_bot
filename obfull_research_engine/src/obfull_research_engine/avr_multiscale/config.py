"""Config hash and constants for multiscale context."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import (
    CANDLE_5M_S,
    CONTRACT_VERSION,
    JOIN_VERSION,
    LOOKBACK_S,
    OI_MAX_AGE_SECONDS,
    WINDOWS_S,
)

# Dashboard raw AVR state → research persistence bucket
AVR_STATE_MAP: dict[str, str] = {
    "BUYER_CONTROL": "BUY_CONTROL",
    "SELLER_CONTROL": "SELL_CONTROL",
    "BUY_ABSORPTION_CANDIDATE": "BUY_ABSORPTION",
    "SELL_ABSORPTION_CANDIDATE": "SELL_ABSORPTION",
    "VACUUM_UP_PROXY": "VACUUM_UP",
    "VACUUM_DOWN_PROXY": "VACUUM_DOWN",
    "BALANCED": "UNCLEAR",
    "UNCLEAR": "UNCLEAR",
    "INSUFFICIENT_BASELINE": "INSUFFICIENT_BASELINE",
    "INSUFFICIENT_DATA": "INSUFFICIENT_DATA",
}

PERSISTENCE_BUCKETS = (
    "BUY_CONTROL",
    "SELL_CONTROL",
    "BUY_ABSORPTION",
    "SELL_ABSORPTION",
    "VACUUM_UP",
    "VACUUM_DOWN",
    "UNCLEAR",
    "INSUFFICIENT_BASELINE",
    "INSUFFICIENT_DATA",
)

CLASSIFIABLE_BUCKETS = frozenset(
    {
        "BUY_CONTROL",
        "SELL_CONTROL",
        "BUY_ABSORPTION",
        "SELL_ABSORPTION",
        "VACUUM_UP",
        "VACUUM_DOWN",
        "UNCLEAR",
    }
)

PROXY_OR_UNCLEAR = frozenset({"UNCLEAR", "BUY_ABSORPTION", "SELL_ABSORPTION", "VACUUM_UP", "VACUUM_DOWN"})


def map_avr_state(raw: str | None) -> str:
    if raw is None or (isinstance(raw, float) and str(raw) == "nan"):
        return "INSUFFICIENT_DATA"
    s = str(raw)
    return AVR_STATE_MAP.get(s, s if s in PERSISTENCE_BUCKETS else "UNCLEAR")


def multiscale_config_payload() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "windows_s": list(WINDOWS_S),
        "candle_5m_s": CANDLE_5M_S,
        "lookback_s": LOOKBACK_S,
        "oi_max_age_seconds": OI_MAX_AGE_SECONDS,
        "oi_liq_join_version": JOIN_VERSION,
        "avr_state_map": AVR_STATE_MAP,
        "tie_rule": "equal_max_count_yields_TIE",
        "window_cut": "[t-window, t)",
        "fp_formulas": "footprint_candles.response_engine.SecondSeries.features_at",
        "fp_source_fqn": "orderbook_analysis.public_trades_canonical",
    }


def multiscale_config_hash() -> str:
    blob = json.dumps(multiscale_config_payload(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
