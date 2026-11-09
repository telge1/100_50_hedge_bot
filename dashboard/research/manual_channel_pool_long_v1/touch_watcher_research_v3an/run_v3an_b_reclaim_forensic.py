#!/usr/bin/env python3
"""B_RECLAIM forensic on 2026-08-30 — full transition export (V3AN isolated)."""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V3AN = Path(__file__).resolve().parent
V3AB = ROOT / "support_break_exit_v3ab"
V3AK = ROOT / "substantial_entry_gate_v3ak"
for p in (ROOT, V3AB, V3AK, V3AN, ROOT.parent):
    sys.path.insert(0, str(p))

import run_l1_pilot as l1
from v3ak_context import build_context_from_snapshot, DEFAULT_SNAPSHOT, FROZEN_SHA256
from run_l1_pilot import fmt_utc
from watcher_config import TouchWatcherConfig
from watcher_fsm import replay_watcher_from_config

LINE_ID = "d-ea081434c5d3"
OUT = V3AN


def idx_at(candles, utc: str) -> int:
    t = int(datetime.strptime(utc, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == t:
            return i
    return -1


def candle_summary(candles, line, i: int, fmt_utc) -> dict:
    c = candles[i]
    lp = line.price_at(float(c["time"]))
    return {
        "bar_idx": i,
        "utc": fmt_utc(int(c["time"])),
        "open": c["open"],
        "high": c["high"],
        "low": c["low"],
        "close": c["close"],
        "line_px": lp,
        "close_vs_line": (float(c["close"]) - float(lp)) if lp else "",
        "bullish": float(c["close"]) > float(c["open"]),
    }


def main() -> int:
    l1._bootstrap_import_paths()
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    candles = frozen["candles"]
    line = frozen["lines_by_id"][LINE_ID]
    i0 = idx_at(candles, "2026-08-30 09:15:00 UTC")
    i1 = idx_at(candles, "2026-08-30 12:00:00 UTC")

    targets: list[tuple[float, str]] = [(0.03, "NEAR_003"), (0.0, "PHYSICAL_ONLY")]
    windows = [6, 12, 24]
    configs: list[TouchWatcherConfig] = []
    for tol, _ in targets:
        for w in windows:
            configs.append(
                TouchWatcherConfig(
                    touch_tolerance_pct=tol,
                    confirmation_mode="B_RECLAIM",
                    confirmation_min_gain_pct=0.20,
                    watch_window_bars=w,
                    preset_id=f"{'NEAR_003' if tol == 0.03 else 'PHYSICAL_ONLY'}+B_RECLAIM+W{w}",
                )
            )

    summary_rows: list[dict] = []
    all_trans: list[dict] = []

    for cfg in configs:
        r = replay_watcher_from_config(candles, line, cfg, i0, i1, fmt_utc)
        first_break = next((t for t in r.transitions if t.reason == "break_close_below"), None)
        reclaims = [t for t in r.transitions if "reclaim_close" in t.reason or "reclaim_failed" in t.reason]
        summary_rows.append(
            {
                "preset_id": cfg.preset_id,
                "touch_tolerance_pct": cfg.touch_tolerance_pct,
                "watch_window_bars": cfg.watch_window_bars,
                "touch_utc": r.entries[0].touch_utc if r.entries else (r.transitions[0].utc if r.transitions else ""),
                "terminal": r.terminal_reason,
                "first_break_utc": first_break.utc if first_break else "",
                "first_break_close": first_break.c if first_break else "",
                "first_break_line_px": first_break.line_px if first_break else "",
                "n_reclaim_events": len(reclaims),
                "entry_utc": r.entries[0].entry_utc if r.entries else "",
            }
        )
        for t in r.transitions:
            all_trans.append(
                {
                    "preset_id": cfg.preset_id,
                    "watch_window_bars": cfg.watch_window_bars,
                    "bar_idx": t.bar_idx,
                    "utc": t.utc,
                    "from_state": t.from_state,
                    "to_state": t.to_state,
                    "reason": t.reason,
                    "open": t.o,
                    "high": t.h,
                    "low": t.l,
                    "close": t.c,
                    "line_px": t.line_px,
                }
            )

    # A1/A2 step trace NEAR 0.03 W12
    a_cfg = TouchWatcherConfig(
        touch_tolerance_pct=0.03,
        confirmation_mode="A1",
        confirmation_min_gain_pct=0.20,
        watch_window_bars=12,
        preset_id="NEAR_003+A1+W12",
    )
    a2_cfg = TouchWatcherConfig(
        touch_tolerance_pct=0.03,
        confirmation_mode="A2",
        confirmation_min_gain_pct=0.20,
        watch_window_bars=12,
        preset_id="NEAR_003+A2+W12",
    )
    for cfg in (a_cfg, a2_cfg):
        r = replay_watcher_from_config(candles, line, cfg, i0, i1, fmt_utc)
        path = OUT / f"l1_v3an_a_forensic_{cfg.preset_id.replace('+', '_')}.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            wcsv = csv.DictWriter(f, fieldnames=list(all_trans[0].keys()) if all_trans else [])
            if all_trans:
                wcsv.writeheader()
            for t in r.transitions:
                wcsv.writerow(
                    {
                        "preset_id": cfg.preset_id,
                        "watch_window_bars": cfg.watch_window_bars,
                        "bar_idx": t.bar_idx,
                        "utc": t.utc,
                        "from_state": t.from_state,
                        "to_state": t.to_state,
                        "reason": t.reason,
                        "open": t.o,
                        "high": t.h,
                        "low": t.l,
                        "close": t.c,
                        "line_px": t.line_px,
                    }
                )

    touch_bar = next((t for t in replay_watcher_from_config(candles, line, a_cfg, i0, i1, fmt_utc).transitions if t.to_state == "WATCHING_ABOVE"), None)
    candle_ctx: list[dict] = []
    if touch_bar:
        for j in range(touch_bar.bar_idx, min(touch_bar.bar_idx + 6, i1 + 1)):
            candle_ctx.append(candle_summary(candles, line, j, fmt_utc))

    sum_path = OUT / "l1_v3an_b_reclaim_forensic_summary.csv"
    with sum_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary_rows[0].keys()))
        w.writeheader()
        w.writerows(summary_rows)

    trans_path = OUT / "l1_v3an_b_reclaim_transitions_full.csv"
    with trans_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(all_trans[0].keys()))
        w.writeheader()
        w.writerows(all_trans)

    ctx_path = OUT / "l1_v3an_a1_touch_window_candles.csv"
    if candle_ctx:
        with ctx_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(candle_ctx[0].keys()))
            w.writeheader()
            w.writerows(candle_ctx)

    print(sum_path)
    print(trans_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
