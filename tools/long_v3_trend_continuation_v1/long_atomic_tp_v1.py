"""Atomic upper TP at fill (LONG A) via strategy_rules.select_long_tp_depth2."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools/phase4d_pool_landscape_backtester_v2"))
from strategy_rules import select_long_tp_depth2  # noqa: E402


def intervals_from_snapshot(snapshot: dict[str, Any]) -> list[dict]:
    out = []
    for b in snapshot.get("upper_bands") or []:
        if str(b.get("pool_status") or "").upper() != "ACTIVE":
            continue
        if int(b.get("unique_origin_count") or 0) < 2:
            continue
        out.append(
            {
                "side": "upper",
                "depth": 2,
                "bottom_price": float(b["lower_price"]),
                "top_price": float(b["upper_price"]),
            }
        )
    return out


def select_long_a_tp_at_fill(
    snapshot: dict[str, Any],
    *,
    entry_fill: float,
    min_tp_pct: float = 0.5,
) -> tuple[float | None, dict[str, Any] | None, str | None]:
    ivs = intervals_from_snapshot(snapshot)
    tp_iv, reason = select_long_tp_depth2(entry_fill, ivs, min_tp_pct)
    if not tp_iv:
        return None, None, reason
    return float(tp_iv["bottom"]), tp_iv, None
