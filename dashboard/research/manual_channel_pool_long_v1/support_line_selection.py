"""L1 V3I — dynamic manual support trend selection (research only)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from line_geometry import LineDef, fmt_utc

LINE_SELECTION_OLD = "OLD_ALLOWLIST"
LINE_SELECTION_DYNAMIC = "DYNAMIC_SUPPORT"
# V3L — support lines independent of legacy upper-main channel compare
LINE_SELECTION_INDEPENDENT = "INDEPENDENT_SUPPORT"


@dataclass(frozen=True)
class LineCausality:
    geometry_active: bool
    anchor_status: str
    causal_at_touch: bool
    label: str  # CAUSAL | RETROSPECTIVE_DRAWING | INACTIVE_GEOMETRY


def line_causality_at_touch(line: LineDef, touch_bar_open_ts: float) -> LineCausality:
    active = line.active_for_candle(touch_bar_open_ts)
    if line.drawing_type == "hline":
        return LineCausality(active, "HLINE", True, "CAUSAL")
    if line.drawing_type != "trend":
        return LineCausality(active, "NOT_TREND", False, "RETROSPECTIVE_DRAWING")
    st = line.anchor_status(touch_bar_open_ts)
    t_lo = line.t_lo
    t_hi = line.t_hi
    if t_lo is None or t_hi is None:
        return LineCausality(active, st, False, "RETROSPECTIVE_DRAWING")
    if touch_bar_open_ts + 1e-9 < t_hi:
        return LineCausality(active, st, False, "RETROSPECTIVE_DRAWING")
    if not active:
        return LineCausality(False, st, False, "RETROSPECTIVE_DRAWING")
    return LineCausality(active, st, True, "CAUSAL")


def line_anchor_record(line: LineDef) -> dict[str, Any]:
    if line.drawing_type != "trend":
        return {
            "drawing_id": line.drawing_id,
            "drawing_type": line.drawing_type,
            "extend_right": line.extend_right,
        }
    return {
        "drawing_id": line.drawing_id,
        "drawing_type": line.drawing_type,
        "extend_right": line.extend_right,
        "anchor_t1_utc": fmt_utc(int(line.t1)) if line.t1 else "",
        "anchor_t2_utc": fmt_utc(int(line.t2)) if line.t2 else "",
        "anchor_p1": f"{line.p1:.10f}" if line.p1 is not None else "",
        "anchor_p2": f"{line.p2:.10f}" if line.p2 is not None else "",
        "valid_from_utc": fmt_utc(int(min(line.t1, line.t2))) if line.t1 and line.t2 else "",
        "valid_to_utc": (
            "extend_right"
            if line.extend_right
            else (fmt_utc(int(max(line.t1, line.t2))) if line.t1 and line.t2 else "")
        ),
    }
