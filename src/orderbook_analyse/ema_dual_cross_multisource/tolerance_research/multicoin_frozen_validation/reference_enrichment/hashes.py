"""Stable hashes for checkpoint resume gating."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from . import constants as C
from .schema_mapping import SOURCE_SCHEMA_AUDIT


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode("utf-8")).hexdigest()


def reference_strategy_hash() -> str:
    return sha256_hex(
        {
            "timeframe": C.REF_TIMEFRAME,
            "mode": C.REF_MODE,
            "group": C.REF_GROUP,
            "strategy_key": C.REF_STRATEGY_KEY,
            "tp_pct": C.REF_TP_PCT,
            "sl_pct": C.REF_SL_PCT,
            "horizon": C.REF_HORIZON,
            "cost_pct": C.REF_COST_PCT,
            "notional": C.REF_NOTIONAL,
            "entry_rule": C.ENTRY_RULE,
            "exclude": sorted(C.EXCLUDE_SYMBOLS),
        }
    )


def feature_definition_hash() -> str:
    from .feature_spec import FEATURE_SPECIFICATION

    return sha256_hex(FEATURE_SPECIFICATION)


def source_schema_hash() -> str:
    return sha256_hex(SOURCE_SCHEMA_AUDIT)


def all_hashes() -> dict[str, str]:
    return {
        "schema_version": str(C.CHECKPOINT_SCHEMA_VERSION),
        "feature_definition_hash": feature_definition_hash(),
        "reference_strategy_hash": reference_strategy_hash(),
        "source_schema_hash": source_schema_hash(),
    }
