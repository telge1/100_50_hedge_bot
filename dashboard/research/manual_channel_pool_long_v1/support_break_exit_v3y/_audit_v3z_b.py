#!/usr/bin/env python3
"""One-off read-only audit for L1 V3Z Variant B (does not modify V3Y results)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

V3Y = Path(__file__).resolve().parent
PROBE = V3Y.parent
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))
if str(V3Y) not in sys.path:
    sys.path.insert(0, str(V3Y))

import run_l1_pilot as l1
from context import build_frozen_context
from support_sim import line_price_at_close
from v3y_config import CONFIRM_DELTA

OUT_MD = V3Y / "L1_V3Z_VARIANT_B_CHART_AUDIT.md"
OUT_CSV = V3Y / "l1_v3z_variant_b_chart_review.csv"


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s.replace(" UTC", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def fmt_utc_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def candle_utc(c: dict) -> str:
    return l1.fmt_utc(int(c["time"]))


def mfe_mae(candles: list, entry_idx: int, exit_idx: int, entry: float) -> tuple[float, float]:
    mfe = mae = 0.0
    for j in range(entry_idx, exit_idx + 1):
        c = candles[j]
        hi = (float(c["high"]) - entry) / entry * 100.0
        lo = (float(c["low"]) - entry) / entry * 100.0
        mfe = max(mfe, hi)
        mae = min(mae, lo)
    return round(mfe, 4), round(mae, 4)


def hold_duration(entry_utc: str, exit_utc: str) -> str:
    if not exit_utc:
        return ""
    d = parse_utc(exit_utc) - parse_utc(entry_utc)
    h = d.total_seconds() / 3600.0
    return f"{h:.2f} h ({int(d.total_seconds() // 60)} min)"


def forensics_support_exit(
    candles: list,
    entry_idx: int,
    exit_idx: int,
    entry: float,
    sl: float,
    pool_tp: float | None,
    channel_line,
    support_line,
) -> dict | None:
    """Replay FSM; return break/confirm details for SUPPORT_BREAK_EXIT."""
    if support_line is None:
        return None
    d = CONFIRM_DELTA
    state = "MONITOR"
    break_idx = -1
    break_close = 0.0
    schedule_at = -1
    confirm_idx = -1

    for j in range(entry_idx, exit_idx + 1):
        c = candles[j]
        channel_px = None
        if channel_line is not None and channel_line.active_for_candle(float(c["time"])):
            channel_px = channel_line.price_at(float(c["time"]))
        hit_sl = float(c["low"]) <= sl
        hit_tp = pool_tp is not None and float(c["high"]) >= pool_tp
        hit_ch = channel_px is not None and float(c["high"]) >= channel_px
        if hit_sl or hit_tp or hit_ch:
            return {"error": "prior_intrabar_exit", "bar": candle_utc(c), "j": j}

        if schedule_at == j:
            return _pack(candles, break_idx, confirm_idx, j, support_line)

        if not support_line.active_for_candle(float(c["time"])):
            state = "MONITOR"
            break_idx = -1
            continue
        lp = line_price_at_close(support_line, c)
        if lp is None:
            continue
        if state == "MONITOR":
            if float(c["close"]) < lp:
                state = "BREAK_PENDING"
                break_close = float(c["close"])
                break_idx = j
        elif state == "BREAK_PENDING" and j > break_idx:
            nc = float(c["close"])
            if nc <= break_close * (1.0 - d):
                confirm_idx = j
                schedule_at = j + 1
            elif nc >= break_close * (1.0 + d):
                state = "MONITOR"
                break_idx = -1
            elif nc >= lp:
                state = "MONITOR"
                break_idx = -1
            else:
                break_close = nc
                break_idx = j

    return None


def _pack(candles, break_idx, confirm_idx, fill_idx, support_line) -> dict:
    bc = candles[break_idx]
    cc = candles[confirm_idx]
    fc = candles[fill_idx]
    b_lp = line_price_at_close(support_line, bc)
    b_close = float(bc["close"])
    c_close = float(cc["close"])
    delta_pct = (c_close - b_close) / b_close * 100.0
    return {
        "break_utc": candle_utc(bc),
        "break_ohlc": (float(bc["open"]), float(bc["high"]), float(bc["low"]), b_close),
        "line_at_break": b_lp,
        "close_vs_line_break": b_close - b_lp if b_lp else None,
        "confirm_utc": candle_utc(cc),
        "confirm_ohlc": (float(cc["open"]), float(cc["high"]), float(cc["low"]), c_close),
        "close_delta_pct": round(delta_pct, 6),
        "confirm_rule": c_close <= b_close * (1.0 - CONFIRM_DELTA),
        "fill_utc": candle_utc(fc),
        "fill_open": float(fc["open"]),
        "rolled_breaks": break_idx,
    }


def load_csv_trades(path: Path, variant: str) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("variant") == variant]


def main() -> None:
    l1._bootstrap_import_paths()
    frozen = build_frozen_context()
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["upper_line"]
    pending = frozen["pending"]
    mod = sys.modules[l1.__name__]

    csv_v3y = load_csv_trades(V3Y / "l1_v3y_test_b_trades.csv", "V3E-B")
    csv_v3u = load_csv_trades(V3Y / "l1_v3y_test_b_baseline_trades.csv", "V3E-B")

    v3u_by_entry = {r["entry_utc"]: r for r in csv_v3u}
    pending_by_entry: dict[str, dict] = {}
    pending_by_eid: dict[str, dict] = {}
    for p in pending:
        if not p.get("_entry_idx"):
            continue
        pending_by_entry[p["entry_utc"]] = p
        pending_by_eid[p["touch_event_id"]] = p

    new_eids = {"te-d-1f70c9798935-0022", "te-d-9b6e62dc884c-0003", "te-d-e1c6e07765fe-0003"}

    rows: list[dict] = []
    support_details: list[dict] = []

    for i, tr in enumerate(sorted(csv_v3y, key=lambda x: x["entry_utc"]), 1):
        entry_utc = tr["entry_utc"]
        p = pending_by_entry.get(entry_utc, {})
        eid = p.get("touch_event_id", "")
        entry_idx = int(p["_entry_idx"]) if p.get("_entry_idx") else None
        entry = float(tr["entry_price"])
        exit_utc = tr.get("exit_utc", "")
        exit_idx = None
        if exit_utc:
            ets = int(parse_utc(exit_utc).timestamp())
            for j, c in enumerate(candles):
                if int(c["time"]) == ets:
                    exit_idx = j
                    break
        mfe = mae = None
        if entry_idx is not None and exit_idx is not None:
            mfe, mae = mfe_mae(candles, entry_idx, exit_idx, entry)

        row = {
            "nr": i,
            "touch_event_id": eid,
            "is_new_v3y_entry": eid in new_eids,
            "entry_utc": entry_utc,
            "entry_price": tr["entry_price"],
            "tp_price": tr.get("tp_price", ""),
            "sl_price": tr.get("sl_price", ""),
            "exit_utc": exit_utc,
            "exit_price": tr.get("exit_price", ""),
            "exit_reason": tr.get("exit_reason", tr.get("exit_kind", "")),
            "pnl_pct": tr.get("pnl_pct", tr.get("gross_pnl_pct", "")),
            "support_drawing_id": tr.get("support_drawing_id") or p.get("touch_drawing_id", ""),
            "upper_channel_id": p.get("tp_channel_line_id", ""),
            "main_upper_at_entry": p.get("_main_upper_at_entry", ""),
            "tp_mode": p.get("tp_mode", tr.get("tp_mode", "")),
            "retrospective": (tr.get("retrospective") == "yes"),
            "hold": hold_duration(entry_utc, exit_utc),
            "mfe_pct": mfe,
            "mae_pct": mae,
        }
        rows.append(row)

        if row["exit_reason"] == "SUPPORT_BREAK_EXIT" and entry_idx is not None and exit_idx is not None:
            support = lines_by_id.get(row["support_drawing_id"])
            fore = forensics_support_exit(
                candles,
                entry_idx,
                exit_idx,
                entry,
                float(tr.get("sl_price") or p["_sl"]),
                float(tr["tp_price"]) if tr.get("tp_price") else None,
                p.get("_channel_line"),
                support,
            )
            support_details.append({**row, "forensics": fore})

    # stats
    pnls = [float(r["pnl_pct"]) for r in rows]
    gross = round(sum(pnls), 4)
    wins = [r for r in rows if float(r["pnl_pct"]) > 0]
    losses = [r for r in rows if float(r["pnl_pct"]) < 0]
    zero = [r for r in rows if float(r["pnl_pct"]) == 0]
    sup_pnls = [float(r["pnl_pct"]) for r in rows if r["exit_reason"] == "SUPPORT_BREAK_EXIT"]

    streak = best = 0
    for r in rows:
        if float(r["pnl_pct"]) < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0

    # Write CSV
    with OUT_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["nr"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Build markdown (abbreviated generation - full tables in file)
    md_lines = [
        "# L1 V3Z — Variant B Chart Audit (V3Y)",
        "",
        "Read-only Auswertung der bestehenden V3Y-Artefakte + Kerzen-Rekonstruktion der Supportbruch-FSM.",
        "Keine Strategieänderung, kein neuer Backtest-Lauf.",
        "",
        "## Gesamtstatistik Variante B (V3Y chrono)",
        "",
        f"- Trades: **{len(rows)}**",
        f"- Gewinntrades: **{len(wins)}**",
        f"- Verlusttrades: **{len(losses)}**",
        f"- SUPPORT_BREAK_EXIT: **{sum(1 for r in rows if r['exit_reason'] == 'SUPPORT_BREAK_EXIT')}**",
        f"- SL-Exits (V3Y): **{sum(1 for r in rows if r['exit_reason'] == 'SL')}**",
        f"- Gesamtbrutto Σ: **{gross:+.4f} %** (additiv)",
        f"- Summe Gewinne: **{sum(float(r['pnl_pct']) for r in wins):+.4f} %**",
        f"- Summe Verluste: **{sum(float(r['pnl_pct']) for r in losses):+.4f} %**",
        f"- Ø pro Trade: **{gross/len(rows):+.4f} %**",
        f"- Längste Verlustserie: **{best}**",
        f"- Σ nur Supportbruch-Exits: **{sum(sup_pnls):+.4f} %**",
        f"- Neue Entries vs. V3U: **{sum(1 for r in rows if r['is_new_v3y_entry'])}** (`{', '.join(sorted(new_eids))}`)",
        "",
        "Alle Trades: **RETROSPECTIVE_DRAWING** = ja.",
        "",
    ]
    OUT_MD.write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    print(json.dumps({"rows": len(rows), "gross": gross, "support_details": len(support_details)}, indent=2))
    # dump full data for assistant to format
    (V3Y / "_audit_v3z_b_payload.json").write_text(
        json.dumps({"rows": rows, "support_details": support_details, "v3u_by_entry": csv_v3u}, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
