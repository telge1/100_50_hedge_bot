"""V3AS — pilot context from canonical research_charts drawings.json (exploratory retrospective)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
for p in (PROBE, PROBE / "watcher_integration_v3ao"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from drawing_refresh import CANONICAL_DRAWINGS_PATH, refresh_drawings_for_run
from drawings_auto_window import compute_auto_window, fetch_last_available_bar_open
from line_geometry import BAR_SEC_5M, fmt_utc
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_INDEPENDENT


def load_live_research_context(
    *,
    out_dir: Path,
    run_id: str,
) -> dict[str, Any]:
    refresh = refresh_drawings_for_run(out_dir, run_id=run_id, prefix="l1_v3as")
    l1._bootstrap_import_paths()
    scratch = out_dir / "_scratch"
    scratch.mkdir(exist_ok=True)
    prev_out = l1.OUT_DIR
    prev_path = l1.DRAWINGS_PATH
    l1.OUT_DIR = scratch
    l1.DRAWINGS_PATH = refresh.canonical_path
    try:
        payload = refresh.payload
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
        raise RuntimeError("build_pilot_context failed on live drawings")

    mod = sys.modules[l1.__name__]
    v3ak_pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(v3ak_pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(v3ak_pending, ctx["upper_line"], mod, independent=True)
    enrich_d2_channel_filter_fields(v3ak_pending, ctx["upper_line"], mod, independent=True)

    return {
        "ctx": ctx,
        "candles": ctx["candles"],
        "lines": ctx["lines"],
        "lines_by_id": ctx["lines_by_id"],
        "upper_line": ctx["upper_line"],
        "auto": auto,
        "data_end": data_end,
        "drawings_sha256": refresh.sha256,
        "canonical_drawings_path": str(CANONICAL_DRAWINGS_PATH),
        "drawings_snapshot_path": str(refresh.snapshot_path),
        "v3ak_pending_post_d2": v3ak_pending,
        "refresh_warnings": refresh.warnings,
        "window_start_utc": fmt_utc(auto.window_start_ts),
        "window_end_utc": fmt_utc(auto.window_end_ts),
        "simulation_end_utc": fmt_utc(data_end + BAR_SEC_5M),
        "n_candles": len(ctx["candles"]),
        "causality_mode": "RETROSPECTIVE_EXPLORATORY_NOT_CAUSALLY_VALIDATED",
    }
