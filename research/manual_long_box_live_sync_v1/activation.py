"""Explicit per-drawing activation (no auto-trade from drawings alone)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BoxActivation:
    drawing_id: str
    enabled: bool
    symbol: str
    notional_usd: float
    dry_run: bool = True
    qty_doge: float | None = None
    order_link_id: str | None = None


def load_activations(path: Path) -> dict[str, BoxActivation]:
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, BoxActivation] = {}
    items = raw.get("activations") or {}
    if not isinstance(items, dict):
        return out
    for did, row in items.items():
        if not isinstance(row, dict) or not row.get("enabled"):
            continue
        qraw = row.get("qty_doge")
        qty_doge = float(qraw) if qraw is not None and str(qraw).strip() != "" else None
        link_raw = row.get("order_link_id")
        order_link_id = (
            str(link_raw).strip() if link_raw is not None and str(link_raw).strip() else None
        )
        out[str(did)] = BoxActivation(
            drawing_id=str(did),
            enabled=True,
            symbol=str(row.get("symbol") or "DOGEUSDT").upper(),
            notional_usd=float(row.get("notional_usd") or 100.0),
            dry_run=bool(row.get("dry_run", True)),
            qty_doge=qty_doge,
            order_link_id=order_link_id,
        )
    return out
