#!/usr/bin/env python3
"""L1 V3S — canonical drawing refresh + INDEPENDENT_SUPPORT full backtest (no strategy changes)."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
PREFIX = "l1_v3s"
OUT = PROBE_DIR
V3P_MANIFEST = OUT / "l1_v3p_run_manifest.json"
V3P_SHA = "76f70d9d43b85c273052e8f7b87cbbb5fab3930d43336dbecaa86c9866a21239"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))
_TOUCH = PROBE_DIR.parent / "manual_line_touch_probe_v1"
if str(_TOUCH) not in sys.path:
    sys.path.insert(0, str(_TOUCH))

import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
import run_l1_v3l_independent_support as v3l
from config import L1_V3E_VARIANT_A, L1_V3E_VARIANT_B, L1_V3E_VARIANT_C
from drawing_refresh import CANONICAL_DRAWINGS_PATH, refresh_drawings_for_run
from drawings_auto_window import (
    DRAWINGS_AUTO_WINDOW,
    STATUS_DATA_END,
    STATUS_LINES_EXHAUSTED,
    STATUS_NO_ELIGIBLE,
    is_candidate_support_trend,
)
from line_geometry import BAR_SEC_5M, fmt_utc
from support_line_selection import LINE_SELECTION_INDEPENDENT

# Reuse V3P helpers (read-only parity).
from run_l1_v3p_full_backtest import (  # noqa: E402
    _candle_gaps,
    _resolve_end_status,
    _trade_dict,
    _tp_audit_row,
    _write_csv,
)

VARIANTS = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}


def _diff_ids(previous: list[str], current: list[str]) -> dict[str, list[str]]:
    prev, cur = set(previous), set(current)
    return {
        "removed": sorted(prev - cur),
        "added": sorted(cur - prev),
        "unchanged": sorted(prev & cur),
    }


def _load_previous_manifest_ids() -> tuple[str, list[str]]:
    if not V3P_MANIFEST.is_file():
        return "", []
    data = json.loads(V3P_MANIFEST.read_text(encoding="utf-8"))
    prev_sha = str(data.get("drawings_sha256") or V3P_SHA)
    prev_ids = sorted(
        set(data.get("support_line_ids") or []) | {l1.UPPER_MAIN_CHANNEL_LINE_ID}
    )
    # support_line_ids exclude upper; add all drawing ids from v3p snapshot if present
    snap = data.get("snapshot_path")
    if snap and Path(snap).is_file():
        drawings = json.loads(Path(snap).read_text(encoding="utf-8")).get("drawings") or []
        prev_ids = sorted(d.get("drawing_id") for d in drawings if d.get("drawing_id"))
    return prev_sha, prev_ids


def _row_for_output(tr: dict, candles: list[dict], code: str, *, kind: str) -> dict[str, str]:
    row = _trade_dict(tr, candles, kind=kind)
    aud = _tp_audit_row(tr)
    row["variant"] = code
    row["support_id"] = row.pop("drawing_id", "")
    row["upper_id"] = aud["upper_channel_drawing_id"]
    row["retrospective"] = "yes" if tr.get("drawing_causality") == "RETROSPECTIVE_DRAWING" else "no"
    return row


def main() -> int:
    l1._bootstrap_import_paths()
    from pool_scan.clock import bar_close  # noqa: F401

    refresh = refresh_drawings_for_run(OUT, run_id=RUN_ID, prefix=PREFIX)
    payload = refresh.payload
    sha = refresh.sha256
    prev_sha, prev_ids = _load_previous_manifest_ids()
    id_diff = _diff_ids(prev_ids, refresh.drawing_ids)

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
            kind = "loss" if ek == "SL" else "win"
            row = _row_for_output(tr, candles, code, kind=kind)
            if ek in ("TP", "CHANNEL_UPPER"):
                winners[code].append(row)
            elif ek == "SL":
                losses[code].append(row)

    for code in "ABC":
        _write_csv(OUT / f"{PREFIX}_variant_{code.lower()}_winners.csv", winners[code])
        _write_csv(OUT / f"{PREFIX}_variant_{code.lower()}_stop_losses.csv", losses[code])

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
                    "tp_price": tr.get("tp_price", ""),
                    "sl_price": tr.get("sl_price", ""),
                    "exit_utc": tr.get("exit_utc", ""),
                    "exit_price": tr.get("exit_price", ""),
                    "pnl_pct": tr.get("gross_pnl_pct", ""),
                    "exit_type": tr.get("exit_kind", ""),
                    "support_drawing_id": tr.get("touch_drawing_id", ""),
                    "upper_channel_drawing_id": aud["upper_channel_drawing_id"],
                    "tp_mode": tr.get("tp_mode", ""),
                    "retrospective": "yes"
                    if tr.get("drawing_causality") == "RETROSPECTIVE_DRAWING"
                    else "no",
                    "A": "no",
                    "B": "no",
                    "C": "no",
                }
            chart[key][code] = "yes"

    chart_rows = sorted(chart.values(), key=lambda r: r["entry_utc"])
    _write_csv(OUT / f"{PREFIX}_chart_review_all_trades.csv", chart_rows)

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
            "support_lines_used": len(set(t.get("touch_drawing_id") for t in ex if t.get("touch_drawing_id"))),
            "rejects": dict(reject[code]),
        }

    manifest = {
        "run_id": RUN_ID,
        "drawing_refresh_protocol": "V3S_CANONICAL_ONLY",
        "canonical_drawings_path": str(CANONICAL_DRAWINGS_PATH),
        "drawings_backup_path": str(refresh.backup_path),
        "mode": LINE_SELECTION_INDEPENDENT,
        "window_mode": DRAWINGS_AUTO_WINDOW,
        "drawings_sha256": sha,
        "drawings_count": len(payload.get("drawings") or []),
        "drawing_ids_all": refresh.drawing_ids,
        "support_line_count": len(support),
        "support_line_ids": sorted(ln.drawing_id for ln in support),
        "snapshot_path": str(refresh.snapshot_path),
        "refresh_warnings": refresh.warnings,
        "previous_reference_run": "l1_v3p",
        "previous_drawings_sha256": prev_sha,
        "drawing_id_diff_vs_v3p_snapshot": id_diff,
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
    (OUT / f"{PREFIX}_run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    lines_md = [
        "# L1 V3S — Drawing refresh + INDEPENDENT_SUPPORT backtest",
        "",
        f"- Run-ID: `{RUN_ID}`",
        f"- Canonical: `{CANONICAL_DRAWINGS_PATH}`",
        f"- SHA256: `{sha}`",
        f"- Drawings: {len(refresh.drawing_ids)} (support candidates: {len(support)})",
        f"- V3P SHA was `{prev_sha[:16]}…` → diff removed={id_diff['removed']} added={id_diff['added']}",
        f"- Window: {manifest['window_start_utc']} .. {manifest['simulation_end_utc_exclusive']}",
        f"- End status: `{end_status}`",
        "",
        "## Stats (gross %, not compounded)",
        "",
    ]
    for code in "ABC":
        s = stats[code]
        lines_md.append(
            f"- **{code}**: trades={s['executed']} wins={s['wins']} sl={s['sl']} "
            f"gross={s['gross']}% support_lines={s['support_lines_used']}"
        )
    (OUT / f"{PREFIX}_full_report.md").write_text("\n".join(lines_md) + "\n", encoding="utf-8")

    print(json.dumps({"run_id": RUN_ID, "sha256": sha, "stats": stats, "diff": id_diff}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
