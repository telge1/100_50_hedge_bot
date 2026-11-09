"""FIRST_STRONG_UPPER_POOL — reuse V3W zone merge + TP2 thresholds; isolated V3AE."""

from __future__ import annotations

import copy
import json
from typing import Any

from config_v3w import TP2Params, TP2_GRID


def _lines_by_id(lines: list) -> dict:
    return {ln.drawing_id: ln for ln in lines}


def apply_first_strong_tp(
    pending: list[dict],
    lines: list,
    *,
    l1_module: Any,
    params: TP2Params,
) -> None:
    """Patch pool-only TP rows after standard D2 gate (channel path unchanged)."""
    from tp_targets import resolve_tp2, resolve_tp4

    lb = _lines_by_id(lines)
    for row in pending:
        if not row.get("_entry_idx") or not row.get("_d2_gate_ok"):
            continue
        entry = float(row["entry_price"])
        entry_ts = int(row["_entry_ts"])
        channel_upper, channel_id = l1_module.channel_upper_at(lines, entry_ts, entry)
        ch_line = lb.get(channel_id) if channel_id else None
        if channel_upper is not None and ch_line is not None:
            row["tp_research_model"] = "BASELINE_CHANNEL_PICK_TP"
            row["_fs_meta"] = ""
            continue

        res = resolve_tp2(row, params)
        fallback = False
        if not res.ok:
            res = resolve_tp4(row)
            fallback = True
        if not res.ok or res.pool_tp is None:
            row["tp_research_model"] = f"FIRST_STRONG/{params.code}:UNRESOLVED"
            continue

        tp = float(res.pool_tp)
        dist = (tp - entry) / entry * 100.0
        mode = f"FIRST_STRONG_UPPER_POOL/{params.code}"
        if fallback:
            mode += "+FALLBACK_FAR_D2"
        row["tp_mode"] = mode
        row["tp_price"] = f"{tp:.10f}"
        row["_pool_tp"] = tp
        row["tp_target_dist_pct"] = f"{dist:.4f}"
        row["tp_pool_id"] = f"fs:{params.code}:{'fb' if fallback else 'strong'}:{tp:.10f}"
        row["tp_research_model"] = params.code if not fallback else f"{params.code}+FALLBACK_FAR_D2"
        meta = dict(res.meta or {})
        meta.update(
            {
                "tp_bottom": tp,
                "dist_from_entry_pct": dist,
                "fallback_far_d2": fallback,
                "params": {
                    "min_width_pct": params.min_width_pct,
                    "min_depth": params.min_depth,
                    "min_origins": params.min_origins,
                },
            }
        )
        if meta.get("zone_top") is None and not fallback:
            meta["zone_bottom"] = tp
        row["_fs_meta"] = json.dumps(meta, sort_keys=True)


def pending_with_first_strong(
    baseline_pending: list[dict],
    lines: list,
    *,
    l1_module: Any,
    params: TP2Params,
) -> list[dict]:
    p = copy.deepcopy(baseline_pending)
    apply_first_strong_tp(p, lines, l1_module=l1_module, params=params)
    return p


def all_tp2_variants() -> tuple[TP2Params, ...]:
    return TP2_GRID
