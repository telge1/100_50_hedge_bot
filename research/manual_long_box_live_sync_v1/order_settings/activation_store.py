"""Read/write manual_long_box_activations.json (per-drawing enablement)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"activations": {}}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {"activations": {}}
    acts = raw.get("activations")
    if not isinstance(acts, dict):
        acts = {}
    return {"activations": acts}


def _save_raw(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def upsert_activation(
    path: Path,
    *,
    drawing_id: str,
    symbol: str,
    notional_usd: float,
    dry_run: bool,
    enabled: bool = True,
    qty_doge: float | None = None,
    order_link_id: str | None = None,
) -> None:
    data = _load_raw(path)
    acts = data["activations"]
    row = {
        "enabled": bool(enabled),
        "symbol": str(symbol).upper(),
        "notional_usd": float(notional_usd),
        "dry_run": bool(dry_run),
        "note": "chart_button_v1",
    }
    if qty_doge is not None:
        row["qty_doge"] = float(qty_doge)
    if order_link_id:
        row["order_link_id"] = str(order_link_id)
    acts[str(drawing_id)] = row
    _save_raw(path, data)


def disable_activation(path: Path, drawing_id: str) -> None:
    data = _load_raw(path)
    acts = data["activations"]
    row = acts.get(str(drawing_id))
    if isinstance(row, dict):
        row["enabled"] = False
        row["dry_run"] = True
    _save_raw(path, data)


def get_activation_row(path: Path, drawing_id: str) -> dict[str, Any] | None:
    data = _load_raw(path)
    row = data["activations"].get(str(drawing_id))
    return row if isinstance(row, dict) else None
