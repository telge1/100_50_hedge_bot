"""Reporting writers for stage-1 outputs."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.integrity import json_safe


def ensure_outdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(json_safe(payload), indent=2, default=str) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    flat_rows = [_flatten(r) for r in rows]
    keys: list[str] = []
    seen = set()
    for r in flat_rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in flat_rows:
            w.writerow({k: _cell(r.get(k)) for k in keys})


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
        if isinstance(v, dict) and k in {
            "trap_checkpoints",
            "acceptance_checkpoints",
            "decision_detail_5s",
            "decision_detail_10s",
            "decision_detail_30s",
            "decision_detail_60s",
        }:
            # keep nested as JSON string for CSV compactness
            out[key if prefix else k] = json.dumps(json_safe(v), default=str)
        elif isinstance(v, list):
            out[key if prefix else k] = json.dumps(json_safe(v), default=str)
        elif isinstance(v, dict):
            out.update(_flatten(v, prefix=key + "."))
        else:
            out[key if prefix else k] = v
    return out


def _cell(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return ""
    return v


def try_write_parquet(path: Path, rows: list[dict[str, Any]]) -> bool:
    if not rows:
        path.write_bytes(b"")
        return False
    try:
        import pandas as pd

        flat = [_flatten(r) for r in rows]
        pd.DataFrame(flat).to_parquet(path, index=False)
        return True
    except Exception:
        # fallback: write empty marker
        path.write_text("# parquet unavailable; see CSV twin\n", encoding="utf-8")
        return False
