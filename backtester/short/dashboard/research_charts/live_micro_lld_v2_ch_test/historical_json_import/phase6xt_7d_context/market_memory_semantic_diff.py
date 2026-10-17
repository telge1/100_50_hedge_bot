"""Field-level semantic state comparison for restart parity (research only)."""

from __future__ import annotations

import json
from typing import Any

from market_memory_state import MarketMemoryState


def _flatten(prefix: str, obj: Any, out: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        for k in sorted(obj.keys()):
            _flatten(f"{prefix}.{k}" if prefix else k, obj[k], out)
    elif isinstance(obj, list):
        out[prefix] = json.dumps(obj, sort_keys=True, default=str)
    else:
        out[prefix] = obj


def classify_diff(path: str, a: Any, b: Any) -> str:
    """A=serialization/order, B=metadata, C=semantic."""
    meta_paths = ("schema_code_version",)
    if any(path.endswith(p) for p in meta_paths):
        return "B_metadata"
    if path.endswith("spatial_steps") or "pool_ids" in path:
        return "A_serialization_order"
    return "C_semantic"


def semantic_diff(left: MarketMemoryState, right: MarketMemoryState) -> dict[str, Any]:
    la = left.semantic_payload()
    ra = right.semantic_payload()
    flat_l: dict[str, Any] = {}
    flat_r: dict[str, Any] = {}
    _flatten("", la, flat_l)
    _flatten("", ra, flat_r)
    keys = sorted(set(flat_l) | set(flat_r))
    diffs = []
    for k in keys:
        if flat_l.get(k) != flat_r.get(k):
            diffs.append(
                {
                    "path": k,
                    "left": flat_l.get(k),
                    "right": flat_r.get(k),
                    "class": classify_diff(k, flat_l.get(k), flat_r.get(k)),
                }
            )
    by_class = {c: [d for d in diffs if d["class"] == c] for c in ("A_serialization_order", "B_metadata", "C_semantic")}
    return {
        "left_hash": left.semantic_state_hash(),
        "right_hash": right.semantic_state_hash(),
        "hash_match": left.semantic_state_hash() == right.semantic_state_hash(),
        "diff_count": len(diffs),
        "diffs": diffs[:500],
        "diffs_by_class": {k: len(v) for k, v in by_class.items()},
        "semantic_mismatch": any(d["class"] == "C_semantic" for d in diffs),
    }
