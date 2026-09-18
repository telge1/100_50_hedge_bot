"""Load LEGACY_ZONE_PROXY enrichment features (never overwrite canonical)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from . import ENRICHMENT_FEATURES_PARQUET, LEGACY_PROXY_TAG


LEGACY_FIELD_MAP = {
    "legacy_hit_qty": "hit_qty",
    "legacy_pull_qty": "pull_qty",
    "legacy_add_qty": "add_qty",
    "legacy_refill_ratio": "refill_ratio",
    "legacy_wall_persistence": "wall_survival_after_hits",
    "legacy_wall_moved_with_price": "wall_moved_with_price",
}


def load_legacy_features(
    repo_root: Path,
    event_ids: list[str],
    *,
    parquet_rel: str = ENRICHMENT_FEATURES_PARQUET,
) -> dict[str, dict[str, Any]]:
    path = Path(repo_root) / parquet_rel
    if not path.is_file():
        return {eid: {"_tag": LEGACY_PROXY_TAG, "_status": "LEGACY_MISSING"} for eid in event_ids}
    df = pd.read_parquet(path)
    out: dict[str, dict[str, Any]] = {}
    for eid in event_ids:
        row = df[df["event_id"] == eid]
        if row.empty:
            out[eid] = {"_tag": LEGACY_PROXY_TAG, "_status": "LEGACY_MISSING"}
            continue
        r = row.iloc[0]
        payload: dict[str, Any] = {"_tag": LEGACY_PROXY_TAG, "_status": "OK"}
        for leg, src in LEGACY_FIELD_MAP.items():
            if src in r.index and pd.notna(r[src]):
                payload[leg] = float(r[src]) if isinstance(r[src], (int, float)) else r[src]
            else:
                payload[leg] = None
                payload[f"{leg}_status"] = "LEGACY_MISSING"
        out[eid] = payload
    return out


def classify_legacy_vs_canonical(
    *,
    legacy_val: float | None,
    canonical_val: float | None,
    comparable: bool,
) -> str:
    if not comparable:
        return "SEMANTICALLY_NOT_COMPARABLE"
    if legacy_val is None:
        return "LEGACY_MISSING"
    if canonical_val is None:
        return "CANONICAL_MISSING"
    # Same sign or both near zero → SAME_DIRECTION; else MATERIAL_DIFFERENCE
    if abs(float(legacy_val)) < 1e-12 and abs(float(canonical_val)) < 1e-12:
        return "SAME_DIRECTION"
    if float(legacy_val) == 0.0 or float(canonical_val) == 0.0:
        return "MATERIAL_DIFFERENCE"
    if (float(legacy_val) > 0) == (float(canonical_val) > 0):
        # magnitude check: >2x relative → material
        ratio = abs(float(canonical_val) / float(legacy_val))
        return "SAME_DIRECTION" if 0.5 <= ratio <= 2.0 else "MATERIAL_DIFFERENCE"
    return "MATERIAL_DIFFERENCE"
