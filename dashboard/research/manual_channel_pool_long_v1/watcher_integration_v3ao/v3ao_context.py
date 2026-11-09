"""V3AO context — frozen snapshot + watcher pending + original D2/V3E enrichments."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
V3AB = PROBE / "support_break_exit_v3ab"
V3AN = PROBE / "touch_watcher_research_v3an"
V3Y = PROBE / "support_break_exit_v3y"
for p in (PROBE, V3AB, V3AN, V3Y):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from drawings_auto_window import compute_auto_window, fetch_last_available_bar_open
from line_geometry import BAR_SEC_5M
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_INDEPENDENT
from v3ak_context import DEFAULT_SNAPSHOT, FROZEN_SHA256
from watcher_config import TouchWatcherConfig

from .build_watcher_pending import build_watcher_pending

DEFAULT_WATCHER_CONFIG = TouchWatcherConfig(
    touch_tolerance_pct=0.03,
    confirmation_mode="A2",
    confirmation_min_gain_pct=0.20,
    watch_window_bars=12,
    reclaim_confirmation_bars=2,
    preset_id="V3AO_DEFAULT_A2_W12",
)


def build_v3ao_context_from_snapshot(
    snapshot_path: Path,
    watcher_cfg: TouchWatcherConfig,
    *,
    expected_sha256: str | None = FROZEN_SHA256,
) -> dict[str, Any]:
    snap = snapshot_path.resolve()
    text = snap.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    if expected_sha256 and sha != expected_sha256:
        raise ValueError(f"drawings sha mismatch: {sha} != {expected_sha256}")

    l1._bootstrap_import_paths()
    scratch = V3AB / "_scratch_v3ao"
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
        raise RuntimeError("build_pilot_context failed (candles/lines)")

    mod = sys.modules[l1.__name__]
    pending = build_watcher_pending(
        l1=mod,
        candles=ctx["candles"],
        lines=ctx["lines"],
        lines_by_id=ctx["lines_by_id"],
        upper_line=ctx["upper_line"],
        cfg=watcher_cfg,
        line_selection_mode=LINE_SELECTION_INDEPENDENT,
    )
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod, independent=True)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod, independent=True)

    return {
        "ctx": ctx,
        "pending": pending,
        "pending_v3ak_reference": copy.deepcopy(ctx["pending"]),
        "lines_by_id": ctx["lines_by_id"],
        "candles": ctx["candles"],
        "upper_line": ctx["upper_line"],
        "auto": auto,
        "data_end": data_end,
        "drawings_sha256": sha,
        "drawings_snapshot": str(snap),
        "watcher_config": watcher_cfg.to_documented_dict(),
        "watcher_preset_id": watcher_cfg.preset_id or watcher_cfg.config_id(),
    }
