"""Inventory canonical drawings for V3AS report."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import run_l1_pilot as l1
from line_geometry import fmt_utc


def _ts_utc(ts: int | float | None) -> str:
    if ts is None:
        return ""
    return fmt_utc(int(ts))


def build_drawings_inventory(payload: dict, lines: list, ctx: dict) -> tuple[list[dict], dict]:
    mod = l1
    upper = ctx.get("upper_line")
    candles = ctx.get("candles") or []
    rows: list[dict] = []
    trend_ids = {ln.drawing_id for ln in lines}
    for d in payload.get("drawings") or []:
        did = str(d.get("drawing_id") or "")
        dtype = d.get("drawing_type") or ""
        visible = d.get("visible") is not False
        parsed = did in trend_ids
        eligible = "yes" if parsed and dtype == "trend" else "n/a"
        retrospective = "yes"
        if parsed and dtype == "trend" and candles and upper:
            ln = next((x for x in lines if x.drawing_id == did), None)
            if ln and upper:
                i0 = 0
                for i, c in enumerate(candles):
                    if ln.active_for_candle(float(c["time"])):
                        i0 = i
                        break
                from support_line_selection import LINE_SELECTION_INDEPENDENT

                sc, rej = l1.assess_long_touch_eligibility(
                    ln, candles, i0, upper=upper, line_selection_mode=LINE_SELECTION_INDEPENDENT
                )
                eligible = "yes" if rej is None else f"no:{rej}"
        rows.append(
            {
                "drawing_id": did,
                "drawing_type": dtype,
                "visible": visible,
                "parsed_as_line": parsed,
                "long_support_eligible_sample": eligible,
                "retrospective_causality": retrospective,
                "anchor_start_utc": _ts_utc(d.get("start_timestamp")),
                "anchor_start_price": d.get("start_price", ""),
                "anchor_end_utc": _ts_utc(d.get("end_timestamp")),
                "anchor_end_price": d.get("end_price", ""),
                "extend_right": (d.get("metadata") or {}).get("extend_right", ""),
                "timeframe_note": "Geometry used on 5m pilot only; anchors are bar-open timestamps",
            }
        )

    trend_in_backtest = sum(1 for r in rows if r["drawing_type"] == "trend" and r["parsed_as_line"])
    long_eligible = sum(1 for r in rows if r["long_support_eligible_sample"] == "yes")

    summary = {
        "total_drawings_in_json": len(payload.get("drawings") or []),
        "parsed_line_geometry": len(lines),
        "trend_lines": sum(1 for r in rows if r["drawing_type"] == "trend"),
        "trend_lines_in_backtest": trend_in_backtest,
        "vlines_unparsed_for_touch": sum(1 for r in rows if r["drawing_type"] == "vline"),
        "long_eligible_trends_sample": long_eligible,
        "long_support_lines_for_metrics": trend_in_backtest if upper is None else max(long_eligible, trend_in_backtest),
        "lookahead_warning": (
            "All manual chart lines are retrospective; anchors may post-date early touches."
        ),
        "multi_timeframe": "Single TF backtest (5m); line anchors are not re-sampled per TF",
    }
    return rows, summary
