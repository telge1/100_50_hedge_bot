"""Join frozen wall-flow impact efficiency onto price-response rows (raw only)."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import ABSORPTION_STATUS, IMPACT_EFFICIENCY_STATUS


def load_wall_flow_impact_index(csv_path: Path) -> dict[str, dict[str, Any]]:
    """Index wall-flow rows by feature_available_at / timestamp."""
    idx: dict[str, dict[str, Any]] = {}
    with Path(csv_path).open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row.get("feature_available_at") or row.get("timestamp") or row.get("bucket_end")
            if not key:
                continue
            # normalize Z
            k = str(key).replace("+00:00", "Z")
            if k.endswith("Z") is False and "T" in k:
                k = k + "Z" if "+" not in k else k
            idx[k] = {
                "impact_efficiency": _f(row.get("impact_efficiency")),
                "hit_notional": _f(row.get("hit_notional")),
                "hit_qty": _f(row.get("hit_qty")),
                "progress_bps": _f(row.get("progress_bps")),
                "midprice_wf": _f(row.get("midprice")),
                "microprice_wf": _f(row.get("microprice")),
                "absorption_ratio_status": row.get("absorption_ratio_status") or ABSORPTION_STATUS,
            }
    return idx


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def join_wall_flow_impact(rows: list[dict[str, Any]], csv_path: Path) -> list[dict[str, Any]]:
    idx = load_wall_flow_impact_index(csv_path)
    # Also build sorted keys for asof join
    keys_sorted = sorted((_as_dt(k), k) for k in idx.keys())
    out: list[dict[str, Any]] = []
    j = 0
    last: dict[str, Any] | None = None
    for row in rows:
        dt = _as_dt(row["decision_time"])
        while j < len(keys_sorted) and keys_sorted[j][0] <= dt:
            last = idx[keys_sorted[j][1]]
            j += 1
        r = dict(row)
        if last is None:
            r.update(
                {
                    "impact_efficiency_raw": None,
                    "impact_efficiency_status": IMPACT_EFFICIENCY_STATUS,
                    "aggressor_wall_notional": None,
                    "wall_flow_progress_bps": None,
                    "absorption_status": ABSORPTION_STATUS,
                    "wall_flow_join_ok": False,
                }
            )
        else:
            # Causal: last wall-flow feature with available_at <= decision_time
            r.update(
                {
                    "impact_efficiency_raw": last.get("impact_efficiency"),
                    "impact_efficiency_status": IMPACT_EFFICIENCY_STATUS,
                    "aggressor_wall_notional": last.get("hit_notional"),
                    "wall_flow_progress_bps": last.get("progress_bps"),
                    "absorption_status": ABSORPTION_STATUS,
                    "wall_flow_join_ok": True,
                }
            )
        out.append(r)
    return out


def build_impact_efficiency_table(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact table linking aggression, progress, micro/mid progress, consumed levels."""
    table = []
    for r in rows:
        if not r.get("coverage_ok"):
            continue
        table.append(
            {
                "decision_time": r.get("decision_time"),
                "aggressor_wall_notional": r.get("aggressor_wall_notional"),
                "price_progress_attack_ticks": r.get("price_progress_attack_ticks"),
                "price_progress_attack_bps": r.get("price_progress_attack_bps"),
                "wall_flow_progress_bps": r.get("wall_flow_progress_bps"),
                "midprice": r.get("midprice"),
                "microprice": r.get("microprice"),
                "consumed_price_levels": r.get("consumed_price_levels"),
                "impact_efficiency_raw": r.get("impact_efficiency_raw"),
                "impact_efficiency_status": IMPACT_EFFICIENCY_STATUS,
                "absorption_status": ABSORPTION_STATUS,
            }
        )
    return table
