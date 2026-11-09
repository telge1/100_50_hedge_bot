#!/usr/bin/env python3
"""L1 V3M — READ-ONLY TP audit for V3L trades (no strategy changes)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
from config import MIN_POOL_DIST_PCT
from pool_d2_metrics import analyze_d2_landscape
from pd1_exact_d2_adapter import upper_d2_bands_at_close, qualify_upper_d2_bands

OUT = PROBE_DIR
SEP_LINE = "d-1f70c9798935"
FOCUS_ENTRIES = {
    "2026-09-16 18:05:00 UTC",
    "2026-09-20 12:10:00 UTC",
    "2026-09-24 09:40:00 UTC",
}


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _executed_rows(path: Path) -> list[dict]:
    return [r for r in csv.DictReader(path.open(encoding="utf-8")) if r.get("exit_kind") not in ("NO_TRADE", "")]


def _tp_selection_audit(entry: float, members: list[dict], dc: datetime) -> dict[str, Any]:
    m, bands, qualified = analyze_d2_landscape(members, dc, entry, apply_provisional_research_filter=False)
    ql = sorted(qualified, key=lambda x: float(x["lower_bound"]))
    by_max = max(qualified, key=lambda x: float(x["lower_bound"])) if qualified else None
    nearest = ql[0] if ql else None
    top3 = []
    for q in ql[:3]:
        dist = (float(q["lower_bound"]) - entry) / entry * 100.0
        top3.append(
            {
                "lower_bound": float(q["lower_bound"]),
                "upper_bound": float(q.get("upper_bound") or q["lower_bound"]),
                "depth": int(q.get("d2_depth") or 0),
                "pool_id": q.get("pool_id", ""),
                "dist_pct": round(dist, 6),
                "tp_qualified": dist >= MIN_POOL_DIST_PCT - 1e-12,
                "selected_by_code": bool(by_max and q["pool_id"] == by_max["pool_id"]),
                "nearest": bool(nearest and q["pool_id"] == nearest["pool_id"]),
            }
        )
    return {
        "d2_gate_pass": m.gate_pass,
        "qualified_count": len(qualified),
        "selected_tp": float(by_max["lower_bound"]) if by_max else None,
        "selected_pool_id": by_max["pool_id"] if by_max else "",
        "selection_rule": "max(lower_bound) among D2-qualified upper bands",
        "nearest_tp": float(nearest["lower_bound"]) if nearest else None,
        "nearest_dist_pct": (
            round((float(nearest["lower_bound"]) - entry) / entry * 100.0, 6) if nearest else None
        ),
        "furthest_selected_dist_pct": (
            round((float(by_max["lower_bound"]) - entry) / entry * 100.0, 6) if by_max else None
        ),
        "skipped_nearer_count": sum(
            1
            for q in ql
            if by_max and float(q["lower_bound"]) < float(by_max["lower_bound"]) - 1e-15
        ),
        "top3_qualified": top3,
        "all_bands_d2": [
            {
                "bottom": b.bottom,
                "top": b.top,
                "depth": b.depth,
                "dist_pct": round((b.bottom - entry) / entry * 100.0, 6) if b.bottom > entry else None,
            }
            for b in bands
            if b.bottom > entry
        ],
    }


def _trade_ohlc_audit(
    entry_utc: str,
    entry_price: float,
    sl: float,
    tp: float | None,
    candles: list[dict],
) -> dict[str, Any]:
    entry_ts = int(_parse_utc(entry_utc).timestamp())
    start_idx = next(i for i, c in enumerate(candles) if int(c["time"]) == entry_ts)
    max_fav = 0.0
    max_adv = 0.0
    exit_kind, exit_ts, exit_px = "", None, None
    ambiguous = False
    for j in range(start_idx, len(candles)):
        c = candles[j]
        hi, lo = float(c["high"]), float(c["low"])
        max_fav = max(max_fav, (hi - entry_price) / entry_price * 100.0)
        max_adv = min(max_adv, (lo - entry_price) / entry_price * 100.0)
        hit_sl = lo <= sl
        hit_tp = tp is not None and hi >= tp
        if hit_sl and hit_tp:
            ambiguous = True
            exit_kind = "AMBIGUOUS"
            exit_ts = int(c["time"])
            exit_px = sl
            break
        if hit_sl:
            exit_kind = "SL"
            exit_ts = int(c["time"])
            exit_px = sl
            break
        if hit_tp:
            exit_kind = "TP"
            exit_ts = int(c["time"])
            exit_px = tp
            break
    return {
        "max_favorable_pct": round(max_fav, 4),
        "max_adverse_pct": round(max_adv, 4),
        "recomputed_exit_kind": exit_kind,
        "recomputed_exit_utc": l1.fmt_utc(exit_ts) if exit_ts else "",
        "recomputed_exit_price": f"{exit_px:.10f}" if exit_px is not None else "",
        "ambiguous_bar": ambiguous,
        "tp_touched": exit_kind in ("TP", "AMBIGUOUS"),
        "sl_touched": exit_kind in ("SL", "AMBIGUOUS"),
    }


def _find_pending_row(candidates: list[dict], touch_event_id: str) -> dict | None:
    for r in candidates:
        if r.get("touch_event_id") == touch_event_id:
            return r
    return None


def main() -> int:
    l1._bootstrap_import_paths()
    manifest = json.loads((OUT / "l1_v3l_run_manifest.json").read_text(encoding="utf-8"))
    candidates = list(csv.DictReader((OUT / "l1_v3l_candidates.csv").open(encoding="utf-8")))
    candles = l1.load_candles(
        start=int(_parse_utc("2026-03-29 16:00:00 UTC").timestamp()),
        end=int(_parse_utc("2026-10-19 13:05:00 UTC").timestamp()),
    )
    l1.CANDLE_LOAD_LIMIT = 100000
    candles = l1.load_candles(
        start=int(_parse_utc("2026-03-29 16:00:00 UTC").timestamp()),
        end=int(_parse_utc("2026-10-19 13:05:00 UTC").timestamp()),
    )

    audits: list[dict] = []
    tp_rows: list[dict] = []
    wins: list[dict] = []
    losses: list[dict] = []
    chart_rows: dict[str, dict] = {}
    stats = {c: {"executed": 0, "wins": 0, "sl": 0, "tp_pool": 0, "ch": 0, "ambig": 0, "tp_select_flag": 0} for c in "ABC"}

    for code in "ABC":
        for tr in _executed_rows(OUT / f"l1_v3l_variant_{code.lower()}_trades.csv"):
            tr = dict(tr)
            tr["variant"] = code
            stats[code]["executed"] += 1
            ek = tr.get("exit_kind", "")
            if ek in ("TP", "CHANNEL_UPPER"):
                stats[code]["wins"] += 1
                if ek == "TP":
                    stats[code]["tp_pool"] += 1
                else:
                    stats[code]["ch"] += 1
            elif ek == "SL":
                stats[code]["sl"] += 1
            elif ek == "AMBIGUOUS":
                stats[code]["ambig"] += 1

            entry = float(tr["entry_price"])
            sl = float(tr["sl_price"])
            tp = float(tr["tp_price"]) if tr.get("tp_price") else None
            pend = _find_pending_row(candidates, tr.get("touch_event_id", ""))
            confirm_utc = tr.get("confirm_utc") or (pend or {}).get("confirm_utc", "")
            members = None
            dc = None
            err = ""
            if confirm_utc:
                from pool_scan.clock import bar_close

                confirm_dt = _parse_utc(confirm_utc)
                dc = bar_close(confirm_dt, "5m")
                members, err = l1.load_pool_snapshot(dc)
            else:
                err = "NO_CONFIRM_UTC"
            audit = _tp_selection_audit(entry, members or [], dc) if members and dc else {"error": err}

            ohlc = _trade_ohlc_audit(tr["entry_utc"], entry, sl, tp, candles)
            planned_dist = (tp - entry) / entry * 100.0 if tp else None
            realized = float(tr["gross_pnl_pct"]) if tr.get("gross_pnl_pct") else None
            row_common = {
                "entry_utc": tr["entry_utc"],
                "entry_price": tr["entry_price"],
                "tp_price": tr.get("tp_price", ""),
                "sl_price": tr.get("sl_price", ""),
                "exit_utc": tr.get("exit_utc", ""),
                "exit_price": tr.get("exit_price", ""),
                "exit_type": ek,
                "pnl_pct": tr.get("gross_pnl_pct", ""),
                "planned_tp_dist_pct": f"{planned_dist:.4f}" if planned_dist is not None else "",
                "drawing_id": tr.get("touch_drawing_id", ""),
                "tp_mode": tr.get("tp_mode", ""),
                "drawing_causality": tr.get("drawing_causality", ""),
                "touch_utc": tr.get("touch_utc", ""),
                "confirm_utc": tr.get("confirm_utc", ""),
                "timeframe": "5m",
                "go_to_utc": tr["entry_utc"],
            }
            if ek in ("TP", "CHANNEL_UPPER"):
                wins.append({**row_common, "variant": code})
            if ek == "SL":
                losses.append({**row_common, "variant": code})

            flag = ""
            if audit.get("skipped_nearer_count", 0) > 0:
                flag = "POSSIBLE_FAR_TP_SELECTION"
                stats[code]["tp_select_flag"] += 1
            audit_status = "OK"
            if ohlc.get("ambiguous_bar"):
                audit_status = "AMBIGUOUS_BAR"
            elif ohlc.get("recomputed_exit_kind") and ohlc["recomputed_exit_kind"] != ek:
                audit_status = f"EXIT_MISMATCH:{ohlc['recomputed_exit_kind']}!={ek}"

            tid = tr.get("touch_event_id", tr["entry_utc"])
            if tid not in chart_rows:
                chart_rows[tid] = {
                    **row_common,
                    "A": "no",
                    "B": "no",
                    "C": "no",
                    "audit_status": audit_status,
                    "tp_select_flag": flag,
                }
            chart_rows[tid][code] = "yes" if ek not in ("NO_TRADE", "") else "no"

            audits.append({**tr, "tp_audit": audit, "ohlc_audit": ohlc, "tp_select_flag": flag})

            for t3 in audit.get("top3_qualified", []):
                tp_rows.append(
                    {
                        "variant": code,
                        "entry_utc": tr["entry_utc"],
                        "drawing_id": tr.get("touch_drawing_id", ""),
                        "tp_mode": tr.get("tp_mode", ""),
                        "selected_tp": audit.get("selected_tp"),
                        **t3,
                    }
                )

    # extra bands rows for focus trades
    focus_detail = []
    for code in ("A",):
        for tr in _executed_rows(OUT / f"l1_v3l_variant_{code.lower()}_trades.csv"):
            if tr.get("entry_utc") not in FOCUS_ENTRIES:
                continue
            entry = float(tr["entry_price"])
            from pool_scan.clock import bar_close

            confirm_dt = _parse_utc(tr["confirm_utc"])
            dc = bar_close(confirm_dt, "5m")
            members, _ = l1.load_pool_snapshot(dc)
            aud = _tp_selection_audit(entry, members or [], dc)
            ohlc = _trade_ohlc_audit(
                tr["entry_utc"], entry, float(tr["sl_price"]), float(tr["tp_price"]), candles
            )
            focus_detail.append({"trade": tr, "audit": aud, "ohlc": ohlc})

    def write_csv(path: Path, rows: list[dict]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
            w.writeheader()
            w.writerows(rows)

    write_csv(OUT / "l1_v3m_all_winning_trades.csv", wins)
    write_csv(OUT / "l1_v3m_all_stop_loss_trades.csv", losses)
    write_csv(OUT / "l1_v3m_chart_review_trades.csv", list(chart_rows.values()))
    write_csv(OUT / "l1_v3m_tp_candidates.csv", tp_rows)

    md = _report(manifest, focus_detail, stats, audits, wins, losses)
    (OUT / "l1_v3m_tp_audit_report.md").write_text(md, encoding="utf-8")
    print("wrote l1_v3m artifacts")
    return 0


def _report(manifest, focus, stats, audits, wins, losses) -> str:
    lines = [
        "# L1 V3M — V3L Take-Profit Audit (READ-ONLY)",
        "",
        "## 1. TP selection implementation",
        "",
        "- Function: `run_l1_pilot.pick_tp()`",
        "- Input qualified list: `analyze_d2_landscape()` → `qualify_upper_d2_bands()` (canonical PD1 ACTIVE Exact Unique-Origin D2, depth ≥ 2)",
        "- Qualification: upper band `bottom > entry` and distance ≥ `MIN_POOL_DIST_PCT` (0.50 %)",
        "- **Selection rule (no channel deckel): `best = max(qualified, key=lower_bound)`**",
        "- Interpretation: **highest lower_bound** = **farthest** qualified target above entry, **not** nearest",
        "- Nearest would be `min(lower_bound)`",
        "- Executed V3L trades use TP from `apply_d2_gate_to_pending()`; label is often `POOL_BOTTOM_NO_CLEAR_CHANNEL` even without channel",
        "",
        "## 2. TP selection verdict",
        "",
        "**Selection matches code, but code picks the farthest qualified D2 bottom, not the nearest.**",
        "This can assign distant TP targets when nearer qualified D2 pools exist.",
        "",
        "## 3. September focus trades",
        "",
    ]
    for item in focus:
        tr = item["trade"]
        aud = item["audit"]
        ohlc = item["ohlc"]
        lines.append(f"### {tr['entry_utc']} `{tr['touch_drawing_id']}` exit={tr['exit_kind']} pnl={tr['gross_pnl_pct']}%")
        lines.append("")
        lines.append(f"- Entry {tr['entry_price']} | TP {tr['tp_price']} | SL {tr['sl_price']}")
        lines.append(f"- Planned TP dist {aud.get('furthest_selected_dist_pct')}% | nearest qualified {aud.get('nearest_tp')} ({aud.get('nearest_dist_pct')}%)")
        lines.append(f"- Skipped nearer pools: {aud.get('skipped_nearer_count')}")
        lines.append(f"- Exit reported {tr['exit_utc']} @ {tr['exit_price']}")
        lines.append(f"- OHLC replay: {ohlc['recomputed_exit_kind']} @ {ohlc['recomputed_exit_utc']} (ambig={ohlc['ambiguous_bar']})")
        lines.append(f"- Max favorable {ohlc['max_favorable_pct']}% | max adverse {ohlc['max_adverse_pct']}%")
        lines.append("")
        lines.append("Top qualified D2 targets:")
        for t in aud.get("top3_qualified", []):
            lines.append(
                f"- bottom={t['lower_bound']:.8f} top={t['upper_bound']:.8f} depth={t['depth']} "
                f"dist={t['dist_pct']}% selected={t['selected_by_code']} nearest={t['nearest']}"
            )
        lines.append("")
    lines.extend(["## 4. Variant stats", ""])
    for c in "ABC":
        s = stats[c]
        gross = sum(float(x["pnl_pct"]) for x in wins if x["variant"] == c) + sum(
            float(x["pnl_pct"]) for x in losses if x["variant"] == c
        )
        wr = s["wins"] / s["executed"] * 100 if s["executed"] else 0
        lines.append(
            f"- **{c}**: executed={s['executed']} wins={s['wins']} sl={s['sl']} "
            f"hit={wr:.1f}% gross={gross:.4f}% pool_tp={s['tp_pool']} channel={s['ch']} "
            f"ambig={s['ambig']} far_tp_flag={s['tp_select_flag']}"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
