"""Build frozen V3U pilot context without touching parent artifacts."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
V3W = Path(__file__).resolve().parent
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))

import pilot_engine as pe
import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
from config import L1_V3E_VARIANT_A, L1_V3E_VARIANT_B, L1_V3E_VARIANT_C
from drawings_auto_window import DRAWINGS_AUTO_WINDOW, compute_auto_window, fetch_last_available_bar_open
from line_geometry import BAR_SEC_5M
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_INDEPENDENT

from config_v3w import DRAWINGS_SHA256, DRAWINGS_SNAPSHOT

VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}


def build_frozen_context() -> dict[str, Any]:
    l1._bootstrap_import_paths()
    snap = (V3W / DRAWINGS_SNAPSHOT).resolve()
    if not snap.is_file():
        raise FileNotFoundError(snap)
    text = snap.read_text(encoding="utf-8")
    import hashlib

    sha = hashlib.sha256(text.encode()).hexdigest()
    if sha != DRAWINGS_SHA256:
        raise ValueError(f"drawings sha mismatch got {sha}")

    scratch = V3W / "_scratch"
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
    indie = True
    pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod, independent=indie)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod, independent=indie)

    return {
        "ctx": ctx,
        "pending": pending,
        "lines": lines,
        "auto": auto,
        "data_end": data_end,
        "drawings_sha256": sha,
        "drawings_snapshot": str(snap),
        "window_mode": DRAWINGS_AUTO_WINDOW,
    }


def admitted_rows(pending: list[dict], variant_code: str) -> list[dict]:
    var = VARIANTS[variant_code]
    out = []
    for row in pending:
        if not row.get("entry_utc"):
            continue
        dec = v3g._filter_decisions(row, VARIANTS)
        if dec[variant_code] != "ADMIT":
            continue
        out.append(row)
    return out
