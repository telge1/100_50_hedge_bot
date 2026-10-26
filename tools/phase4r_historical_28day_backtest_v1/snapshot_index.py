"""Causal per-bar pool snapshots (Phase 3A JSONL format)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_JSONL = ROOT / "results/phase3a_dynamic_pool_landscape_timeline_v1/landscape_timeline.jsonl"


def load_snapshot_index(path: Path | None = None) -> dict[str, dict[str, Any]]:
    path = path or DEFAULT_JSONL
    if not path.is_file():
        return {}
    idx: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            snap = json.loads(line)
            idx[snap["candle_close_utc"]] = snap
    return idx


def depth2_pools(snap: dict[str, Any], side: str) -> list[dict[str, Any]]:
    key = "upper_pools" if side == "upper" else "lower_pools"
    out = []
    for p in snap.get(key) or []:
        if int(p.get("depth") or 0) < 2:
            continue
        if p.get("all_origins_active") is False:
            continue
        out.append(p)
    return out


def intervals_from_snapshot(snap: dict[str, Any]) -> list[dict[str, Any]]:
    ivs: list[dict[str, Any]] = []
    for side in ("lower", "upper"):
        for p in depth2_pools(snap, side):
            ivs.append(
                {
                    "side": side,
                    "bottom_price": float(p["bottom_price"]),
                    "top_price": float(p["top_price"]),
                    "depth": int(p["depth"]),
                    "origins": list(p.get("origins") or []),
                }
            )
    return ivs


def upper_tp_rows_from_snapshot(snap: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for p in depth2_pools(snap, "upper"):
        rows.append(
            {
                "side": "upper",
                "bottom_price": float(p["bottom_price"]),
                "top_price": float(p["top_price"]),
                "depth": int(p["depth"]),
                "origins": list(p.get("origins") or []),
            }
        )
    return rows
