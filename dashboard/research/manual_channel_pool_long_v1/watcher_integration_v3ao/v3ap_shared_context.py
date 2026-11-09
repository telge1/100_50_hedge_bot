"""Load frozen snapshot + pilot candles once (V3AP batch)."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
V3AB = PROBE / "support_break_exit_v3ab"
for p in (PROBE, V3AB):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
from drawings_auto_window import compute_auto_window, fetch_last_available_bar_open
from line_geometry import BAR_SEC_5M
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_INDEPENDENT
from v3ak_context import DEFAULT_SNAPSHOT, FROZEN_SHA256
import pilot_engine as pe


def load_shared_probe_context(
    snapshot_path: Path = DEFAULT_SNAPSHOT,
    *,
    expected_sha256: str = FROZEN_SHA256,
) -> dict[str, Any]:
    snap = snapshot_path.resolve()
    text = snap.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    if sha != expected_sha256:
        raise ValueError(f"drawings sha mismatch: {sha}")

    l1._bootstrap_import_paths()
    scratch = V3AB / "_scratch_v3ap"
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
    v3ak_pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(v3ak_pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(v3ak_pending, ctx["upper_line"], mod, independent=True)
    enrich_d2_channel_filter_fields(v3ak_pending, ctx["upper_line"], mod, independent=True)

    inventory = [
        {
            "drawing_id": d.get("drawing_id"),
            "drawing_type": d.get("drawing_type"),
            "start_ts": d.get("start_timestamp"),
            "end_ts": d.get("end_timestamp"),
        }
        for d in payload.get("drawings") or []
    ]

    return {
        "ctx": ctx,
        "candles": ctx["candles"],
        "lines": ctx["lines"],
        "lines_by_id": ctx["lines_by_id"],
        "upper_line": ctx["upper_line"],
        "auto": auto,
        "data_end": data_end,
        "drawings_sha256": sha,
        "drawings_snapshot": str(snap),
        "drawing_inventory": inventory,
        "v3ak_pending_post_d2": v3ak_pending,
        "window_start_utc": l1.fmt_utc(auto.window_start_ts),
        "window_end_utc": l1.fmt_utc(auto.window_end_ts),
        "simulation_end_utc": l1.fmt_utc(data_end + BAR_SEC_5M),
    }


def build_watcher_pending_for_cfg(shared: dict[str, Any], cfg) -> tuple[list[dict], dict]:
    from watcher_integration_v3ao.build_watcher_pending import build_watcher_pending_with_stats

    mod = sys.modules[l1.__name__]
    pending, stats = build_watcher_pending_with_stats(
        l1=mod,
        candles=shared["candles"],
        lines=shared["lines"],
        lines_by_id=shared["lines_by_id"],
        upper_line=shared["upper_line"],
        cfg=cfg,
        line_selection_mode=LINE_SELECTION_INDEPENDENT,
    )
    apply_d2_gate_to_pending(pending, shared["lines"], l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, shared["upper_line"], mod, independent=True)
    enrich_d2_channel_filter_fields(pending, shared["upper_line"], mod, independent=True)
    es = {
        "touch_episodes": stats.touch_episodes,
        "entry_signals": stats.entry_signals,
        "structure_rejected": stats.structure_rejected,
    }
    return pending, es
