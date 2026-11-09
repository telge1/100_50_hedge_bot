#!/usr/bin/env python3
"""V3AP — 30.08.2026 long trade forensic (V3AK vs Watcher A1/A2/A3)."""

from __future__ import annotations

import copy
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROBE = Path(__file__).resolve().parents[1]
V3AP = Path(__file__).resolve().parent
for p in (PROBE, V3AP, PROBE / "touch_watcher_research_v3an"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
from watcher_config import TouchWatcherConfig
from watcher_integration_v3ao.v3ap_funnel import run_full_funnel
from watcher_integration_v3ao.v3ap_shared_context import build_watcher_pending_for_cfg, load_shared_probe_context

OUT = V3AP
LINE_ID = "d-ea081434c5d3"
DAY = "2026-08-30"


def _stage_row(label: str, row: dict | None, trade: dict | None, pending_ready: dict | None) -> dict:
    row = row or {}
    trade = trade or {}
    pr = pending_ready or {}
    return {
        "variant": label,
        "touch_drawing_id": row.get("touch_drawing_id", LINE_ID),
        "touch_utc": row.get("touch_utc", ""),
        "confirm_utc": row.get("confirm_utc", ""),
        "entry_utc": row.get("entry_utc", ""),
        "entry_price": row.get("entry_price", ""),
        "long_trigger_allowed": row.get("long_trigger_allowed", ""),
        "d2_gate_ok": row.get("_d2_gate_ok", ""),
        "d2_gate_reason": row.get("d2_gate_reason", ""),
        "cap6_pick_ok": pr.get("_pick_ok", row.get("_pick_ok", "")),
        "cap6_reject": pr.get("reject_reason", row.get("reject_reason", "")),
        "cap6_tp_price": pr.get("tp_price", row.get("tp_price", "")),
        "e3_pass": pr.get("entry_gate_pass", ""),
        "e3_reason": pr.get("entry_gate_reason", ""),
        "executed": "yes" if trade.get("exit_kind") not in ("NO_TRADE", "", None) else "no",
        "exec_block_reason": trade.get("reject_reason", ""),
        "sl_price": trade.get("sl_price", row.get("sl_price", "")),
        "tp_price": trade.get("tp_price", row.get("tp_price", "")),
        "exit_utc": trade.get("exit_utc", ""),
        "exit_kind": trade.get("exit_kind", ""),
        "gross_pnl_pct": trade.get("gross_pnl_pct", ""),
    }


def _pick_day_rows(pending: list[dict], trades: list[dict], pid_prefix: str | None = None) -> tuple[dict | None, dict | None, dict | None]:
    rows = [
        p
        for p in pending
        if p.get("touch_drawing_id") == LINE_ID
        and (p.get("touch_utc") or "").startswith(DAY)
    ]
    if not rows and pid_prefix:
        rows = [p for p in pending if p.get("touch_event_id", "").startswith(f"te-{LINE_ID}")]
    rows.sort(key=lambda r: r.get("entry_utc") or r.get("touch_utc"))
    target = None
    for r in rows:
        if r.get("_entry_idx") and (r.get("entry_utc") or "").startswith(DAY):
            target = r
            break
    if target is None and rows:
        target = rows[0]
    trade = None
    if target:
        trade = next((t for t in trades if t.get("touch_event_id") == target.get("touch_event_id")), None)
        if trade is None:
            for t in trades:
                eu = t.get("entry_utc") or ""
                if eu.startswith(DAY) and t.get("touch_drawing_id") == LINE_ID:
                    trade = t
                    break
    ready = target
    return target, trade, ready


def main() -> int:
    l1._bootstrap_import_paths()
    shared = load_shared_probe_context()
    lines = shared["lines"]
    forensic: list[dict] = []

    v3ak_f = run_full_funnel(
        shared["v3ak_pending_post_d2"],
        lines=lines,
        candles=shared["candles"],
        upper_line=shared["upper_line"],
        lines_by_id=shared["lines_by_id"],
        entry_stats={
            "touch_episodes": len(shared["ctx"]["all_events"]),
            "entry_signals": sum(1 for p in shared["v3ak_pending_post_d2"] if p.get("_entry_idx")),
        },
    )
    prmap = {r["touch_event_id"]: r for r in v3ak_f["pending_ready"]}
    row, tr, _ = _pick_day_rows(
        shared["v3ak_pending_post_d2"],
        v3ak_f["trades"],
        None,
    )
    # Also explicit production event
    prod = next(
        (p for p in shared["v3ak_pending_post_d2"] if p.get("touch_event_id") == "te-d-ea081434c5d3-0003"),
        row,
    )
    prod_trade = next((t for t in v3ak_f["trades"] if t.get("touch_event_id") == "te-d-ea081434c5d3-0003"), tr)
    forensic.append(
        _stage_row(
            "V3AK_te-0003",
            prod,
            prod_trade,
            prmap.get(prod["touch_event_id"]) if prod else None,
        )
    )

    watcher_cfgs = [
        TouchWatcherConfig(
            touch_tolerance_pct=0.03,
            confirmation_mode=m,
            confirmation_min_gain_pct=0.20,
            watch_window_bars=12,
            preset_id=f"FORENSIC_{m}_W12",
        )
        for m in ("A1", "A2", "A3")
    ]
    for cfg in watcher_cfgs:
        pending, es = build_watcher_pending_for_cfg(shared, cfg)
        funnel = run_full_funnel(
            pending,
            lines=lines,
            candles=shared["candles"],
            upper_line=shared["upper_line"],
            lines_by_id=shared["lines_by_id"],
            entry_stats=es,
        )
        prmap = {r["touch_event_id"]: r for r in funnel["pending_ready"]}
        # earliest Aug30 entry on line
        cands = [
            p
            for p in pending
            if p.get("touch_drawing_id") == LINE_ID
            and (p.get("entry_utc") or "").startswith(DAY)
            and p.get("_entry_idx")
        ]
        cands.sort(key=lambda r: r["entry_utc"])
        target = cands[0] if cands else None
        tr = None
        if target:
            tr = next((t for t in funnel["trades"] if t.get("touch_event_id") == target["touch_event_id"]), None)
        forensic.append(
            _stage_row(
                cfg.preset_id,
                target,
                tr,
                prmap.get(target["touch_event_id"]) if target else None,
            )
        )

    path = OUT / "l1_v3ap_aug30_forensic.csv"
    if forensic:
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(forensic[0].keys()))
            w.writeheader()
            w.writerows(forensic)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
