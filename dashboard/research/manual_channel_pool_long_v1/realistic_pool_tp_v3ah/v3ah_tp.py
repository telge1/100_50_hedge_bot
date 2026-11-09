"""Apply realistic CAP + dominance pool TP to pending rows (pool-only path)."""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any

from zone_density import RealisticTPParams, resolve_realistic_tp, resolution_to_meta


def _lines_by_id(lines: list) -> dict:
    return {ln.drawing_id: ln for ln in lines}


def _decision_close(row: dict) -> datetime | None:
    dc_s = row.get("_pool_decision_close") or ""
    if not dc_s:
        return None
    return datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def apply_realistic_pool_tp(
    pending: list[dict],
    lines: list,
    *,
    l1_module: Any,
    params: RealisticTPParams,
) -> None:
    lb = _lines_by_id(lines)
    for row in pending:
        if not row.get("_entry_idx") or not row.get("_d2_gate_ok"):
            continue
        entry = float(row["entry_price"])
        entry_ts = int(row["_entry_ts"])
        channel_upper, channel_id = l1_module.channel_upper_at(lines, entry_ts, entry)
        ch_line = lb.get(channel_id) if channel_id else None
        if channel_upper is not None and ch_line is not None:
            row["tp_research_model"] = "BASELINE_CHANNEL_UNCHANGED"
            row["_v3ah_meta"] = ""
            continue

        dc = _decision_close(row)
        members = row.get("_pool_members")
        if dc is None or members is None:
            row["tp_research_model"] = f"{params.code}:NO_DC"
            row["_pick_ok"] = False
            row["reject_reason"] = row.get("reject_reason") or "UNRESOLVED_D2_DECISION_CLOSE"
            continue

        res = resolve_realistic_tp(
            members,
            dc,
            entry,
            cap_dist_pct=params.cap_dist_pct,
            dominance=params.dominance,
        )
        row["_v3ah_meta"] = resolution_to_meta(res)
        row["tp_research_model"] = params.code

        if res.ok and res.tp_bottom is not None:
            tp = float(res.tp_bottom)
            dist = (tp - entry) / entry * 100.0
            row["tp_mode"] = f"REALISTIC_POOL/{params.code}"
            row["tp_price"] = f"{tp:.10f}"
            row["_pool_tp"] = tp
            row["tp_target_dist_pct"] = f"{dist:.4f}"
            row["tp_pool_id"] = f"v3ah:{params.code}:{tp:.10f}"
            row["reject_reason"] = row.get("reject_reason") or ""
            row["_pick_ok"] = True
            row["_channel_line"] = None
            continue

        if params.no_tp_policy == "BLOCK":
            row["tp_mode"] = "NO_QUALIFIED_TP"
            row["tp_price"] = ""
            row["_pool_tp"] = None
            row["tp_target_dist_pct"] = ""
            row["reject_reason"] = "NO_QUALIFIED_TP"
            row["_pick_ok"] = False
            continue

        # ALLOW_NOPOOL: support + SL only
        row["tp_mode"] = "NO_QUALIFIED_TP"
        row["tp_price"] = ""
        row["_pool_tp"] = None
        row["tp_target_dist_pct"] = ""
        row["tp_pool_id"] = ""
        row["reject_reason"] = ""
        row["_pick_ok"] = True


def pending_with_realistic_tp(
    baseline_pending: list[dict],
    lines: list,
    *,
    l1_module: Any,
    params: RealisticTPParams,
) -> list[dict]:
    p = copy.deepcopy(baseline_pending)
    apply_realistic_pool_tp(p, lines, l1_module=l1_module, params=params)
    return p
