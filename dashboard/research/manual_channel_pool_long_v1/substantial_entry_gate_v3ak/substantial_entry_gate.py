"""Substantial upper D2 merge-zone entry gate (V3W thresholds, research only)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from config import MIN_POOL_DIST_PCT
from config_v3w import TP2Params, TP2_GRID
from pool_d2_metrics import analyze_d2_landscape
from zone_features import ResearchZone, enrich_zone_origins, merge_qualified_zones

NEAR_CAP_PCT = 2.50
MERGE_GAP_PCT = 0.15


@dataclass(frozen=True)
class EntryGateSpec:
    code: str
    params: TP2Params | None  # None = E0 baseline (no extra gate)


ENTRY_GATE_SPECS: tuple[EntryGateSpec, ...] = (
    EntryGateSpec("E0_BASELINE", None),
    EntryGateSpec("E1_SUBSTANTIAL_TP2a", TP2_GRID[0]),
    EntryGateSpec("E2_SUBSTANTIAL_TP2b", TP2_GRID[1]),
    EntryGateSpec("E3_SUBSTANTIAL_TP2c", TP2_GRID[2]),
)


@dataclass
class GateResult:
    pass_gate: bool
    reason: str
    qualifying_zone: ResearchZone | None
    fail_detail: str


def _decision_close(row: dict) -> datetime | None:
    dc_s = row.get("_pool_decision_close") or ""
    if not dc_s:
        return None
    return datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def evaluate_substantial_near_zone(
    members: list[dict] | None,
    dc: datetime,
    entry: float,
    params: TP2Params,
) -> GateResult:
    if members is None:
        return GateResult(False, "NO_SSOT", None, "missing pool members")
    _m, _bands, qualified = analyze_d2_landscape(
        members, dc, entry, apply_provisional_research_filter=False
    )
    if not qualified:
        return GateResult(False, "NO_QUALIFIED_D2", None, "no qualified D2 bands")
    zones = merge_qualified_zones(qualified, entry, merge_gap_pct=MERGE_GAP_PCT)
    enrich_zone_origins(zones, members, dc, entry)
    best_fail = ""
    for z in zones:
        dist = z.dist_from_entry_pct
        if dist < MIN_POOL_DIST_PCT - 1e-9:
            continue
        if dist > NEAR_CAP_PCT + 1e-9:
            continue
        fails: list[str] = []
        if z.width_pct + 1e-12 < params.min_width_pct:
            fails.append(f"width<{params.min_width_pct}")
        if z.max_depth < params.min_depth:
            fails.append(f"depth<{params.min_depth}")
        if z.unique_origins < params.min_origins:
            fails.append(f"origins<{params.min_origins}")
        if not fails:
            return GateResult(True, "", z, "")
        best_fail = (
            f"zone {z.bottom:.5f}-{z.top:.5f} dist={dist:.2f}% "
            f"w={z.width_pct:.3f}% d={z.max_depth} o={z.unique_origins}: {','.join(fails)}"
        )
    detail = best_fail or "no merge zone within 2.5% above entry"
    return GateResult(False, "SUBSTANTIAL_NEAR_ZONE_MISSING", None, detail)


def gate_result_to_meta(res: GateResult, *, spec_code: str, params: TP2Params | None) -> str:
    z = res.qualifying_zone
    payload: dict[str, Any] = {
        "entry_gate_model": spec_code,
        "pass": res.pass_gate,
        "reason": res.reason,
        "fail_detail": res.fail_detail,
        "near_cap_pct": NEAR_CAP_PCT,
    }
    if params:
        payload["thresholds"] = {
            "min_width_pct": params.min_width_pct,
            "min_depth": params.min_depth,
            "min_origins": params.min_origins,
        }
    if z:
        payload["qualifying_zone"] = {
            "bottom": z.bottom,
            "top": z.top,
            "width_pct": z.width_pct,
            "max_depth": z.max_depth,
            "unique_origins": z.unique_origins,
            "dist_from_entry_pct": z.dist_from_entry_pct,
        }
    return json.dumps(payload, sort_keys=True)


def apply_substantial_entry_gate(pending: list[dict], spec: EntryGateSpec) -> None:
    """Patch pending rows in place. E0 is a no-op."""
    if spec.params is None:
        return
    for row in pending:
        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            continue
        dc = _decision_close(row)
        members = row.get("_pool_members")
        if dc is None:
            row["entry_gate_pass"] = "no"
            row["entry_gate_reason"] = "UNRESOLVED_DECISION_CLOSE"
            row["_v3ak_entry_meta"] = gate_result_to_meta(
                GateResult(False, "NO_DC", None, ""), spec_code=spec.code, params=spec.params
            )
            row["_pick_ok"] = False
            row["reject_reason"] = row.get("reject_reason") or "UNRESOLVED_DECISION_CLOSE"
            continue
        entry = float(row["entry_price"])
        res = evaluate_substantial_near_zone(members, dc, entry, spec.params)
        row["_v3ak_entry_meta"] = gate_result_to_meta(res, spec_code=spec.code, params=spec.params)
        if res.pass_gate:
            row["entry_gate_pass"] = "yes"
            row["entry_gate_reason"] = ""
            continue
        row["entry_gate_pass"] = "no"
        row["entry_gate_reason"] = res.reason
        row["reject_reason"] = f"{spec.code}:{res.reason}"
        row["_pick_ok"] = False
