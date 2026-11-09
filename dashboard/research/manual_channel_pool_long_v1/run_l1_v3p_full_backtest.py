#!/usr/bin/env python3
"""L1 V3P — fresh INDEPENDENT_SUPPORT full backtest report (no strategy changes)."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
DRAWINGS_PATH = PROBE_DIR.parents[1] / "research_charts" / "user_data" / "drawings.json"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))
_TOUCH = PROBE_DIR.parent / "manual_line_touch_probe_v1"
if str(_TOUCH) not in sys.path:
    sys.path.insert(0, str(_TOUCH))

import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
from config import L1_V3E_VARIANT_A, L1_V3E_VARIANT_B, L1_V3E_VARIANT_C
from drawings_auto_window import (
    DRAWINGS_AUTO_WINDOW,
    STATUS_DATA_END,
    STATUS_LINES_EXHAUSTED,
    STATUS_NO_ELIGIBLE,
    compute_auto_window,
    fetch_last_available_bar_open,
    is_candidate_support_trend,
)
from line_geometry import BAR_SEC_5M, fmt_utc, utc_ts
from pool_d2_metrics import analyze_d2_landscape
from support_line_selection import LINE_SELECTION_INDEPENDENT

import run_l1_v3l_independent_support as v3l

VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}
OUT = PROBE_DIR


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _candle_gaps(candles: list[dict]) -> int:
    gaps = 0
    for i in range(1, len(candles)):
        if int(candles[i]["time"]) - int(candles[i - 1]["time"]) != 300:
            gaps += 1
    return gaps


def _tp_audit_row(tr: dict) -> dict[str, Any]:
    entry = float(tr["entry_price"])
    tp = float(tr["tp_price"]) if tr.get("tp_price") else None
    planned = (tp - entry) / entry * 100.0 if tp else None
    nearest = ""
    nearest_dist = ""
    skipped = ""
    if tr.get("confirm_utc") and tp:
        from pool_scan.clock import bar_close

        dc = bar_close(_parse_utc(tr["confirm_utc"]), "5m")
        members, _ = l1.load_pool_snapshot(dc)
        if members:
            _, _, qualified = analyze_d2_landscape(members, dc, entry, apply_provisional_research_filter=False)
            if qualified:
                ql = sorted(qualified, key=lambda x: float(x["lower_bound"]))
                n = ql[0]
                nearest = f"{float(n['lower_bound']):.10f}"
                nearest_dist = f"{(float(n['lower_bound']) - entry) / entry * 100.0:.4f}"
                sel = max(qualified, key=lambda x: float(x["lower_bound"]))
                skipped = str(
                    sum(1 for q in ql if float(q["lower_bound"]) < float(sel["lower_bound"]) - 1e-15)
                )
    return {
        "planned_tp_dist_pct": f"{planned:.4f}" if planned is not None else "",
        "nearest_d2_bottom": nearest,
        "nearest_d2_dist_pct": nearest_dist,
        "nearer_d2_skipped": skipped,
        "upper_channel_drawing_id": tr.get("tp_channel_line_id", "") or "",
    }


def _mfe(tr: dict, candles: list[dict]) -> str:
    if not tr.get("entry_utc"):
        return ""
    entry = float(tr["entry_price"])
    sl = float(tr["sl_price"])
    tp = float(tr["tp_price"]) if tr.get("tp_price") else None
    entry_ts = int(_parse_utc(tr["entry_utc"]).timestamp())
    start = next(i for i, c in enumerate(candles) if int(c["time"]) == entry_ts)
    fav = 0.0
    for j in range(start, len(candles)):
        c = candles[j]
        fav = max(fav, (float(c["high"]) - entry) / entry * 100.0)
        hit_sl = float(c["low"]) <= sl
        hit_tp = tp is not None and float(c["high"]) >= tp
        if hit_sl or hit_tp:
            break
    return f"{fav:.4f}"


def _trade_dict(tr: dict, candles: list[dict], *, kind: str) -> dict[str, str]:
    base = {
        "entry_utc": tr.get("entry_utc", ""),
        "entry_price": tr.get("entry_price", ""),
        "tp_price": tr.get("tp_price", ""),
        "sl_price": tr.get("sl_price", ""),
        "exit_utc": tr.get("exit_utc", ""),
        "exit_price": tr.get("exit_price", ""),
        "exit_type": tr.get("exit_kind", ""),
        "pnl_pct": tr.get("gross_pnl_pct", ""),
        "drawing_id": tr.get("touch_drawing_id", ""),
        "tp_mode": tr.get("tp_mode", ""),
        "drawing_causality": tr.get("drawing_causality", ""),
        "touch_utc": tr.get("touch_utc", ""),
        "confirm_utc": tr.get("confirm_utc", ""),
    }
    base.update(_tp_audit_row(tr))
    if kind == "loss":
        base["mfe_pct"] = _mfe(tr, candles)
    return base


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)


def _resolve_end_status(auto, support_lines, data_end: int) -> str:
    if auto.status == STATUS_NO_ELIGIBLE:
        return STATUS_NO_ELIGIBLE
    if any(ln.extend_right for ln in support_lines):
        return STATUS_DATA_END
    ends = [int(ln.t_hi) for ln in support_lines if ln.t_hi is not None]
    if ends and max(ends) < data_end:
        return STATUS_LINES_EXHAUSTED
    return STATUS_DATA_END


def main() -> int:
    l1._bootstrap_import_paths()
    from pool_scan.clock import bar_close  # noqa: F401 — after bootstrap
    text = DRAWINGS_PATH.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    payload = json.loads(text)
    snap = OUT / f"l1_v3p_drawings_snapshot_{sha[:16]}.json"
    if not snap.is_file():
        snap.write_text(text, encoding="utf-8")

    result = v3l._run(LINE_SELECTION_INDEPENDENT)
    ctx = result["ctx"]
    pending = result["pending"]
    trades = result["trades"]
    candles = ctx["candles"]
    auto = result["auto"]

    lines = list(ctx["lines_by_id"].values())
    support = [
        ln
        for ln in lines
        if is_candidate_support_trend(ln, upper_main_id=l1.UPPER_MAIN_CHANNEL_LINE_ID)
    ]
    end_status = _resolve_end_status(auto, support, result["data_end"])

    winners: dict[str, list[dict]] = {c: [] for c in "ABC"}
    losses: dict[str, list[dict]] = {c: [] for c in "ABC"}
    for code in "ABC":
        for tr in v3g._executed(trades[code]):
            ek = tr.get("exit_kind", "")
            row = _trade_dict(tr, candles, kind="loss" if ek == "SL" else "win")
            row["variant"] = code
            if ek in ("TP", "CHANNEL_UPPER"):
                winners[code].append(row)
            elif ek == "SL":
                losses[code].append(row)

    for code in "ABC":
        _write_csv(OUT / f"l1_v3p_variant_{code.lower()}_winners.csv", winners[code])
        _write_csv(OUT / f"l1_v3p_variant_{code.lower()}_stop_losses.csv", losses[code])

    chart: dict[tuple, dict] = {}
    for code in "ABC":
        for tr in v3g._executed(trades[code]):
            key = (
                tr.get("entry_utc", ""),
                tr.get("exit_utc", ""),
                tr.get("exit_kind", ""),
                tr.get("touch_drawing_id", ""),
            )
            if key not in chart:
                aud = _tp_audit_row(tr)
                chart[key] = {
                    "entry_utc": tr.get("entry_utc", ""),
                    "entry_price": tr.get("entry_price", ""),
                    "sl_price": tr.get("sl_price", ""),
                    "tp_price": tr.get("tp_price", ""),
                    "exit_utc": tr.get("exit_utc", ""),
                    "exit_price": tr.get("exit_price", ""),
                    "pnl_pct": tr.get("gross_pnl_pct", ""),
                    "support_drawing_id": tr.get("touch_drawing_id", ""),
                    "upper_channel_drawing_id": aud["upper_channel_drawing_id"],
                    "tp_mode": tr.get("tp_mode", ""),
                    "A": "no",
                    "B": "no",
                    "C": "no",
                }
            chart[key][code] = "yes"

    chart_rows = sorted(chart.values(), key=lambda r: r["entry_utc"])
    _write_csv(OUT / f"l1_v3p_chart_review_all_trades.csv", chart_rows)

    reject = {c: Counter() for c in "ABC"}
    for row in pending:
        if not row.get("entry_utc"):
            continue
        dec = v3g._filter_decisions(row, VARIANTS)
        for code in "ABC":
            if dec[code] != "ADMIT":
                reject[code][dec[code]] += 1

    exec_by_line: dict[str, int] = defaultdict(int)
    for tr in v3g._executed(trades["A"]):
        exec_by_line[tr.get("touch_drawing_id", "")] += 1

    stats: dict[str, dict] = {}
    for code in "ABC":
        ex = v3g._executed(trades[code])
        real = v3g._realized(ex)
        wins = [t for t in real if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
        sls = [t for t in real if t.get("exit_kind") == "SL"]
        open_e = [t for t in ex if t.get("exit_kind") == "OPEN_AT_WINDOW_END"]
        gross = sum(float(t["gross_pnl_pct"]) for t in real if t.get("gross_pnl_pct"))
        stats[code] = {
            "executed": len(ex),
            "wins": len(wins),
            "sl": len(sls),
            "hit_rate": round(len(wins) / len(real) * 100, 2) if real else 0,
            "wins_sum": round(sum(float(t["gross_pnl_pct"]) for t in wins), 4),
            "losses_sum": round(sum(float(t["gross_pnl_pct"]) for t in sls), 4),
            "gross": round(gross, 4),
            "pool_tp_exits": sum(1 for t in real if t.get("exit_kind") == "TP"),
            "channel_exits": sum(1 for t in real if t.get("exit_kind") == "CHANNEL_UPPER"),
            "open": len(open_e),
            "rejects": dict(reject[code]),
        }

    manifest = {
        "run_id": RUN_ID,
        "mode": LINE_SELECTION_INDEPENDENT,
        "window_mode": DRAWINGS_AUTO_WINDOW,
        "drawings_sha256": sha,
        "drawings_count": len(payload.get("drawings") or []),
        "support_line_count": len(support),
        "support_line_ids": sorted(ln.drawing_id for ln in support),
        "snapshot_path": str(snap),
        "window_start_utc": fmt_utc(auto.window_start_ts),
        "window_end_utc_exclusive": fmt_utc(auto.window_end_ts),
        "simulation_end_utc_exclusive": fmt_utc(result["data_end"] + BAR_SEC_5M),
        "candle_count": len(candles),
        "candle_gaps": _candle_gaps(candles),
        "run_end_status": end_status,
        "touch_events_total": ctx["touch_stats"]["touch_events_total"],
        "long_triggers_allowed": ctx["touch_stats"]["long_triggers_allowed"],
        "c1_candidates": sum(1 for r in pending if r.get("entry_utc")),
        "executed_by_support_line_A": dict(exec_by_line),
        "variant_stats": stats,
        "tp_modes_used_A": dict(Counter(t.get("tp_mode", "") for t in v3g._executed(trades["A"]))),
        "upper_channel_ids_used_A": dict(
            Counter(t.get("tp_channel_line_id", "") or "" for t in v3g._executed(trades["A"]))
        ),
    }
    (OUT / "l1_v3p_run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

  # report md
    lines_md = [
        "# L1 V3P — Full INDEPENDENT_SUPPORT Backtest",
        "",
        f"- Run-ID: `{RUN_ID}`",
        f"- SHA256: `{sha}`",
        f"- Support lines: {len(support)}",
        f"- Window: {manifest['window_start_utc']} .. {manifest['simulation_end_utc_exclusive']}",
        f"- Candles: {len(candles)} (gaps: {manifest['candle_gaps']})",
        f"- End status: `{end_status}`",
        "",
    ]
    for code in "ABC":
        s = stats[code]
        lines_md.append(
            f"- **{code}**: exec={s['executed']} wins={s['wins']} sl={s['sl']} gross={s['gross']}%"
        )
    (OUT / "l1_v3p_full_report.md").write_text("\n".join(lines_md) + "\n", encoding="utf-8")
    print(json.dumps({"run_id": RUN_ID, "stats": stats, "manifest_keys": list(manifest.keys())}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
