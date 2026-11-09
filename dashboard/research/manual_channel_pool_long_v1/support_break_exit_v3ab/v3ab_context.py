"""Build pilot context from V3AB immutable drawings snapshot."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
V3AB = Path(__file__).resolve().parent
V3Y = PROBE / "support_break_exit_v3y"
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))
if str(V3Y) not in sys.path:
    sys.path.insert(0, str(V3Y))

import pilot_engine as pe
import run_l1_pilot as l1
from config import L1_V3E_VARIANT_A, L1_V3E_VARIANT_B, L1_V3E_VARIANT_C
from drawings_auto_window import compute_auto_window, fetch_last_available_bar_open
from line_geometry import BAR_SEC_5M
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_INDEPENDENT

VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}


def build_context_from_snapshot(
    snapshot_path: Path,
    *,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    snap = snapshot_path.resolve()
    if not snap.is_file():
        raise FileNotFoundError(snap)
    text = snap.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    if expected_sha256 and sha != expected_sha256:
        raise ValueError(f"drawings sha mismatch: {sha} != {expected_sha256}")

    l1._bootstrap_import_paths()
    scratch = V3AB / "_scratch"
    scratch.mkdir(exist_ok=True)
    prev_out = l1.OUT_DIR
    prev_path = l1.DRAWINGS_PATH
    l1.OUT_DIR = scratch
    l1.DRAWINGS_PATH = snap
    try:
        payload = json.loads(text)
        lines = [ln for ln in (l1.line_from_drawing(d) for d in payload.get("drawings") or []) if ln]
        data_end = fetch_last_available_bar_open(l1.SYMBOL, l1.TIMEFRAME, bootstrap=l1._bootstrap_import_paths)
        auto = compute_auto_window(
            lines, upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID, data_end_bar_open=data_end
        )
        saved = l1.CANDLE_LOAD_LIMIT
        l1.CANDLE_LOAD_LIMIT = 100000
        try:
            ctx = l1.build_pilot_context(
                line_selection_mode=LINE_SELECTION_INDEPENDENT,
                window_start=auto.window_start_ts,
                window_end=auto.window_end_ts,
                simulation_end=data_end + BAR_SEC_5M,
            )
        finally:
            l1.CANDLE_LOAD_LIMIT = saved
    finally:
        l1.OUT_DIR = prev_out
        l1.DRAWINGS_PATH = prev_path

    if ctx is None:
        raise RuntimeError("build_pilot_context failed")

    mod = sys.modules[l1.__name__]
    pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod, independent=True)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod, independent=True)

    drawing_rows = payload.get("drawings") or []
    inventory = []
    for d in drawing_rows:
        inventory.append(
            {
                "drawing_id": d.get("drawing_id"),
                "drawing_type": d.get("drawing_type"),
                "start_ts": d.get("start_timestamp"),
                "start_price": d.get("start_price"),
                "end_ts": d.get("end_timestamp"),
                "end_price": d.get("end_price"),
                "extend_right": (d.get("metadata") or {}).get("extend_right"),
                "visible": d.get("visible"),
            }
        )

    return {
        "ctx": ctx,
        "pending": pending,
        "lines_by_id": ctx["lines_by_id"],
        "candles": ctx["candles"],
        "upper_line": ctx["upper_line"],
        "auto": auto,
        "data_end": data_end,
        "drawings_sha256": sha,
        "drawings_snapshot": str(snap),
        "drawing_inventory": inventory,
    }
