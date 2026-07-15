"""Deterministic hashing helpers for research-run outputs."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from research.regime_scanner.research_runs.schema import HASH_NOT_AVAILABLE, HASH_NOT_EXPORTED


def json_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def combined_output_hash(
    *,
    trend_state_hash: str | None,
    structure_event_hash: str | None,
    price_action_hash: str | None,
    momentum_hash: str | None,
    signal_hash: str | None,
) -> str:
    """Hash only groups that are explicitly available (not placeholder tokens)."""
    groups: dict[str, str] = {}
    for name, value in (
        ("trend_states", trend_state_hash),
        ("structure_events", structure_event_hash),
        ("price_action", price_action_hash),
        ("momentum", momentum_hash),
        ("signals", signal_hash),
    ):
        if value and value not in {HASH_NOT_AVAILABLE, HASH_NOT_EXPORTED}:
            groups[name] = value
    return json_hash(groups)


def _json_default(value: Any) -> Any:
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(f"{value:.17g}")
    return str(value)
