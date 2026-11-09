#!/usr/bin/env python3
"""L1 V3G — V3F filters on window 2026-08-25 .. 2026-09-20 (excl.)."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
ROOT = PROBE_DIR.parents[1]
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"
V3F_SHA_PREFIX = "3e7041de2814607b"
BAR_SEC = 300

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
from config import (
    L1_V3E_VARIANT_A,
    L1_V3E_VARIANT_B,
    L1_V3E_VARIANT_C,
    MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT,
    MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER,
    PILOT_ALLOWED_LOWER_TREND_IDS,
    UPPER_MAIN_CHANNEL_LINE_ID,
)
from line_geometry import utc_ts
from pd1_exact_d2_adapter import D2_COMPUTE_VERSION, upper_d2_bands_at_close

OUT_DIR = PROBE_DIR

V3G_WINDOW_START = utc_ts(datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc))
V3G_WINDOW_END = utc_ts(datetime(2026, 9, 20, 0, 0, 0, tzinfo=timezone.utc))
EXPECTED_BARS = 26 * 24 * 12  # 7488

SEG_Z1_END = utc_ts(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc))
SEG_Z2_END = utc_ts(datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc))

V3F_REGRESSION = {
    "A": {
        "gross_realized": 7.7204,
        "trades": {
            "2026-08-25 21:15:00 UTC": ("SL", -1.2000),
            "2026-08-26 15:10:00 UTC": ("CHANNEL_UPPER", 6.5449),
            "2026-08-29 07:20:00 UTC": ("CHANNEL_UPPER", 2.3755),
        },
    },
    "B": {
        "gross_realized": 8.9204,
        "blocked": ["2026-08-25 21:15:00 UTC"],
        "trades": {
            "2026-08-26 15:10:00 UTC": ("CHANNEL_UPPER", 6.5449),
            "2026-08-29 07:20:00 UTC": ("CHANNEL_UPPER", 2.3755),
        },
    },
    "C": {"gross_realized": 8.9204},
}


def _parse_utc(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())


def _drawings_manifest() -> dict[str, Any]:
    text = DRAWINGS_PATH.read_text(encoding="utf-8") if DRAWINGS_PATH.is_file() else ""
    payload = json.loads(text) if text else {}
    sha = hashlib.sha256(text.encode()).hexdigest() if text else ""
    ids = {str(d.get("drawing_id") or d.get("id") or "") for d in payload.get("drawings") or []}
    return {
        "sha256": sha,
        "sha_matches_v3f": sha.startswith(V3F_SHA_PREFIX),
        "drawing_count": len(payload.get("drawings") or []),
        "allowlist_present": sorted(i for i in PILOT_ALLOWED_LOWER_TREND_IDS if i in ids),
        "allowlist_missing": sorted(i for i in PILOT_ALLOWED_LOWER_TREND_IDS if i not in ids),
    }


def _git_head() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _audit_candles(candles: list[dict], start: int, end: int) -> dict[str, Any]:
    by_ts = {int(c["time"]) for c in candles}
    gaps = [l1.fmt_utc(t) for t in range(start, end, BAR_SEC) if t not in by_ts]
    return {
        "loaded": len(candles),
        "expected": (end - start) // BAR_SEC,
        "complete": len(candles) == (end - start) // BAR_SEC and not gaps,
        "gap_count": len(gaps),
        "first_gaps": gaps[:20],
    }


def _audit_line_geometry(lines_by_id: dict, start: int, end: int) -> dict[str, Any]:
    checks = [start, (start + end) // 2, end - BAR_SEC]
    out: dict[str, Any] = {"upper_main": UPPER_MAIN_CHANNEL_LINE_ID, "lines": []}
    keys = sorted(PILOT_ALLOWED_LOWER_TREND_IDS | {UPPER_MAIN_CHANNEL_LINE_ID})
    for did in keys:
        ln = lines_by_id.get(did)
        if not ln:
            out["lines"].append({"drawing_id": did, "missing": True})
            continue
        snap = []
        for t in checks:
            st = ln.anchor_status(float(t)) if ln.drawing_type == "trend" else "HLINE"
            snap.append(
                {
                    "bar_utc": l1.fmt_utc(t),
                    "active": ln.active_for_candle(float(t)),
                    "anchor_status": st,
                    "price": ln.price_at(float(t)),
                    "extend_right": ln.extend_right,
                }
            )
        out["lines"].append({"drawing_id": did, "missing": False, "samples": snap})
    return out


def _audit_pool_and_d2(
    pending: list[dict],
    *,
    window_start: int,
    window_end: int,
) -> dict[str, Any]:
    from pool_scan.clock import bar_close

    decision_closes: set[str] = set()
    for row in pending:
        if row.get("_pool_decision_close"):
            decision_closes.add(str(row["_pool_decision_close"]))
    # tägliche Stichprobe 00:00 UTC → decision close der 5m-Kerze
    daily: list[str] = []
    t = window_start
    while t < window_end:
        dc = bar_close(datetime.fromtimestamp(t, tz=timezone.utc), "5m")
        daily.append(l1.fmt_utc(int(dc.timestamp())))
        t += 86400

    all_dc = sorted(decision_closes | set(daily))
    pool_fail: list[dict] = []
    d2_fail: list[dict] = []
    ok = 0
    for dc_s in all_dc:
        dc = datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        members, err = l1.load_pool_snapshot(dc)
        if err or members is None:
            pool_fail.append({"decision_close_utc": dc_s, "error": err or "NO_MEMBERS"})
            continue
        ok += 1
        entry_proxy = 0.09
        bands, berr = upper_d2_bands_at_close(members, dc, entry_proxy)
        if berr:
            d2_fail.append({"decision_close_utc": dc_s, "error": berr})

    first_fail = pool_fail[0]["decision_close_utc"] if pool_fail else None
    last_ok = None
    if pool_fail:
        # letzter OK vor erster Lücke (grobe Grenze)
        for dc_s in reversed(all_dc):
            if dc_s not in {f["decision_close_utc"] for f in pool_fail}:
                last_ok = dc_s
                break

    return {
        "unique_decision_closes_from_candidates": len(decision_closes),
        "probes_total": len(all_dc),
        "pool_snapshots_ok": ok,
        "pool_failures": pool_fail,
        "d2_compute_failures": d2_fail,
        "pool_coverage_complete_for_window": len(pool_fail) == 0,
        "approx_last_ok_before_gap": last_ok,
        "first_pool_failure_utc": first_fail,
    }


def _executed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "")]


def _realized(trades: list[dict]) -> list[dict]:
    return [t for t in _executed(trades) if t.get("exit_kind") != "OPEN_AT_WINDOW_END"]


def _write_trades_csv(path: Path, trades: list[dict]) -> None:
    rows = [{k: v for k, v in t.items() if not str(k).startswith("_")} for t in trades]
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)


def _summary_metrics(trades: list[dict], touch_stats: dict) -> dict[str, Any]:
    realized = _realized(trades)
    open_tr = [t for t in _executed(trades) if t.get("exit_kind") == "OPEN_AT_WINDOW_END"]
    wins = [float(t["gross_pnl_pct"]) for t in realized if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
    losses = [float(t["gross_pnl_pct"]) for t in realized if t.get("exit_kind") == "SL"]
    sm = pe.summarize_variant(trades, touch_stats)
    sm["entry_candidates"] = sum(1 for t in trades if t.get("entry_utc"))
    sm["win_count"] = len([t for t in realized if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")])
    sm["loss_count"] = len([t for t in realized if t.get("exit_kind") == "SL"])
    sm["wins_sum"] = sum(wins)
    sm["losses_sum"] = sum(losses)
    sm["gross_realized_sum"] = sum(wins) + sum(losses)
    sm["open_count"] = len(open_tr)
    sm["open_mtm_sum"] = sum(float(t["gross_pnl_pct"]) for t in open_tr) if open_tr else 0.0
    sm["reject_first_d2"] = sm["rejects"].get("REJECTED_FIRST_D2_TOO_FAR", 0)
    sm["reject_stagger"] = sm["rejects"].get("REJECTED_NO_EARLY_D2_STAGGERING", 0)
    return sm


def _admitted_candidates(variant, pending, candles, *, upper_line, l1_module) -> int:
    channel_line_at_bar = l1_module.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    cycle = pe.ChannelCycleState()
    n = 0
    for row in sorted(
        pending,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    ):
        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            continue
        entry_idx = int(row["_entry_idx"])
        tsi = row.get("_touch_start_idx")
        if tsi is not None:
            cycle.maybe_reset_at_touch_start(candles, int(tsi), upper_px)
        cycle.advance_to(candles, entry_idx - 1, upper_px)
        entry = float(row["entry_price"])
        reason = pe.apply_variant_entry_filters(
            variant, entry, row.get("_main_upper_at_entry"), row.get("_effective_tp"), cycle, row=row
        )
        if reason:
            continue
        if row.get("_pick_ok"):
            n += 1
    return n


def _filter_decisions(row: dict, variants: dict) -> dict[str, str]:
    from channel_cycle import ChannelCycleState

    out: dict[str, str] = {}
    for code, var in variants.items():
        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            out[code] = row.get("reject_reason") or "NOT_LONG_TRIGGER"
            continue
        if not row.get("_d2_gate_ok"):
            out[code] = row.get("d2_gate_reason") or "D2_GATE_FAIL"
            continue
        cycle = ChannelCycleState()
        reason = pe.apply_variant_entry_filters(
            var,
            float(row["entry_price"]),
            row.get("_main_upper_at_entry"),
            row.get("_effective_tp"),
            cycle,
            row=row,
        )
        if reason:
            out[code] = reason
        elif not row.get("_pick_ok"):
            out[code] = row.get("reject_reason") or "PICK_FAIL"
        else:
            out[code] = "ADMIT"
    return out


def _exit_ts(trade: dict) -> int | None:
    return _parse_utc(str(trade.get("exit_utc") or "")) if trade.get("exit_utc") else None


def _segment_for_exit(exit_ts: int | None) -> str:
    if exit_ts is None:
        return "unknown"
    if exit_ts < SEG_Z1_END:
        return "Z1_25Aug_01Sep"
    if exit_ts < SEG_Z2_END:
        return "Z2_01Sep_10Sep"
    if exit_ts < V3G_WINDOW_END:
        return "Z3_10Sep_20Sep"
    return "after_window"


def _segment_pnl(trades: list[dict]) -> dict[str, dict[str, float]]:
    """Zuordnung nach **Exit-UTC** (OPEN → letzter Bar-Close im Fenster)."""
    segs = {
        "Z1_25Aug_01Sep": {"exec": 0, "wins": 0, "sl": 0, "w_sum": 0.0, "l_sum": 0.0},
        "Z2_01Sep_10Sep": {"exec": 0, "wins": 0, "sl": 0, "w_sum": 0.0, "l_sum": 0.0},
        "Z3_10Sep_20Sep": {"exec": 0, "wins": 0, "sl": 0, "w_sum": 0.0, "l_sum": 0.0},
    }
    for t in _realized(trades):
        ex = _exit_ts(t)
        seg = _segment_for_exit(ex)
        if seg not in segs:
            continue
        segs[seg]["exec"] += 1
        pnl = float(t["gross_pnl_pct"])
        if t.get("exit_kind") == "SL":
            segs[seg]["sl"] += 1
            segs[seg]["l_sum"] += pnl
        elif t.get("exit_kind") in ("TP", "CHANNEL_UPPER"):
            segs[seg]["wins"] += 1
            segs[seg]["w_sum"] += pnl
    for s in segs.values():
        s["gross"] = s["w_sum"] + s["l_sum"]
    return segs


def _z1_realized_gross(trades: list[dict]) -> float:
    total = 0.0
    for t in _realized(trades):
        ex = _exit_ts(t)
        if ex is not None and ex < SEG_Z1_END:
            total += float(t["gross_pnl_pct"])
    return total


def _by_eid(trades: list[dict]) -> dict[str, dict]:
    return {t["touch_event_id"]: t for t in trades}


def _row_for_eid(pending: list[dict], eid: str) -> dict:
    return next((r for r in pending if r["touch_event_id"] == eid), {})


def _filter_diff_rows(
    tr_a: list[dict],
    tr_b: list[dict],
    tr_c: list[dict],
    pending: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    ba, bb, bc = _by_eid(tr_a), _by_eid(tr_b), _by_eid(tr_c)
    a_exe = {eid for eid, t in ba.items() if t.get("exit_kind") not in ("NO_TRADE", "")}
    b_exe = {eid for eid, t in bb.items() if t.get("exit_kind") not in ("NO_TRADE", "")}
    c_exe = {eid for eid, t in bc.items() if t.get("exit_kind") not in ("NO_TRADE", "")}

    def pack(eid: str, ta, tb) -> dict:
        p = _row_for_eid(pending, eid)
        return {
            "touch_event_id": eid,
            "entry_utc": ta.get("entry_utc") or tb.get("entry_utc") or p.get("entry_utc"),
            "entry_price": ta.get("entry_price") or tb.get("entry_price"),
            "drawing_id": ta.get("touch_drawing_id") or tb.get("touch_drawing_id"),
            "nearest_d2_pct": p.get("d2_first_in_channel_pct", ""),
            "d2_quarter_bands": p.get("d2_quarter_bands", ""),
            "filter_B_or_C": tb.get("reject_reason", ""),
            "A_exit": ta.get("exit_kind", ""),
            "A_pnl": ta.get("gross_pnl_pct", ""),
            "B/C_exit": tb.get("exit_kind", ""),
            "B/C_pnl": tb.get("gross_pnl_pct", ""),
        }

    list_ab = [pack(eid, ba[eid], bb[eid]) for eid in sorted(a_exe - b_exe)]
    list_bc = [pack(eid, bb[eid], bc[eid]) for eid in sorted(b_exe - c_exe)]

    # Slot freed: in B executed, not in A, same entry not blocked by global on A... simplified: B exe, A NO_TRADE filter
    slot_rows: list[dict] = []
    for eid in sorted(b_exe - a_exe):
        ta, tb = ba.get(eid, {}), bb[eid]
        if ta.get("exit_kind") in ("NO_TRADE", ""):
            slot_rows.append(
                {
                    **pack(eid, ta, tb),
                    "note": "B_executed_A_did_not_same_event_or_slot",
                }
            )
    return list_ab, list_bc, slot_rows


def _trade_sections(trades: list[dict], label: str) -> list[str]:
    realized = sorted(_realized(trades), key=lambda t: t.get("entry_utc", ""))
    wins = [t for t in realized if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
    losses = [t for t in realized if t.get("exit_kind") == "SL"]
    open_tr = sorted(
        [t for t in _executed(trades) if t.get("exit_kind") == "OPEN_AT_WINDOW_END"],
        key=lambda t: t.get("entry_utc", ""),
    )
    cross = []
    for t in realized:
        ent = _parse_utc(t["entry_utc"]) if t.get("entry_utc") else None
        ex = _exit_ts(t)
        if ent and ex and _segment_for_exit(ent) != _segment_for_exit(ex):
            cross.append(t)

    out = [f"### {label}", ""]
    for title, rows, cols in (
        (
            "Gewinn-Trades",
            wins,
            "| Entry UTC | Entry | SL | TP@Entry | Exit UTC | Exit | Typ | drawing_id | PnL % |",
        ),
        (
            "Stop-Loss-Trades",
            losses,
            "| Entry UTC | Entry | SL | TP@Entry | SL UTC | Exit | drawing_id | PnL % |",
        ),
    ):
        out.append(f"#### {title}")
        if rows:
            out.append(cols)
            out.append("| --- |" + " --- |" * (cols.count("|") - 1))
            for t in rows:
                if title.startswith("Gewinn"):
                    out.append(
                        f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                        f"| {t['exit_utc']} | {t['exit_price']} | {t['exit_kind']} | {t['touch_drawing_id']} "
                        f"| {t['gross_pnl_pct']} |"
                    )
                else:
                    out.append(
                        f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                        f"| {t['exit_utc']} | {t['exit_price']} | {t['touch_drawing_id']} | {t['gross_pnl_pct']} |"
                    )
        else:
            out.append("_Keine._")
        out.append("")

    out.append("#### Offene Trades am Fensterende")
    if open_tr:
        out.append("| Entry UTC | Entry | SL | TP | Mark UTC | Mark | drawing_id | unrealized % |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
        for t in open_tr:
            out.append(
                f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                f"| {t['exit_utc']} | {t['exit_price']} | {t['touch_drawing_id']} | {t['gross_pnl_pct']} |"
            )
    else:
        out.append("_Keine._")
    if cross:
        out.append("")
        out.append("#### Abschnittsübergreifend (Entry-Segment ≠ Exit-Segment)")
        for t in cross:
            out.append(
                f"- {t['entry_utc']} → exit {t['exit_utc']} ({t['exit_kind']}) "
                f"entry_seg≠exit_seg PnL={t['gross_pnl_pct']}"
            )
    out.append("")
    return out


def main() -> int:
    l1._bootstrap_import_paths()
    manifest = _drawings_manifest()
    comparable_v3f = manifest["sha_matches_v3f"]

    saved_start, saved_end, saved_limit = l1.WINDOW_START, l1.WINDOW_END, l1.CANDLE_LOAD_LIMIT
    l1.WINDOW_START = V3G_WINDOW_START
    l1.WINDOW_END = V3G_WINDOW_END
    l1.CANDLE_LOAD_LIMIT = 10000
    coverage: dict[str, Any] = {
        "window_start_utc": l1.fmt_utc(V3G_WINDOW_START),
        "window_end_utc_exclusive": l1.fmt_utc(V3G_WINDOW_END),
        "expected_bars": EXPECTED_BARS,
        "git_head": _git_head(),
        "drawings": manifest,
        "v3f_comparable": comparable_v3f,
    }
    try:
        ctx = l1.build_pilot_context()
    finally:
        l1.WINDOW_START, l1.WINDOW_END, l1.CANDLE_LOAD_LIMIT = saved_start, saved_end, saved_limit

    if ctx is None:
        coverage["error"] = "build_pilot_context_failed"
        (OUT_DIR / "l1_v3g_data_coverage_report.json").write_text(
            json.dumps(coverage, indent=2) + "\n", encoding="utf-8"
        )
        return 2

    cand_audit = _audit_candles(ctx["candles"], V3G_WINDOW_START, V3G_WINDOW_END)
    coverage["candles"] = cand_audit
    coverage["line_geometry"] = _audit_line_geometry(ctx["lines_by_id"], V3G_WINDOW_START, V3G_WINDOW_END)

    mod = sys.modules[l1.__name__]
    lines = list(ctx["lines_by_id"].values())
    pending = copy.deepcopy(ctx["pending"])
    from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields

    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod)

    coverage["pool_d2"] = _audit_pool_and_d2(pending, window_start=V3G_WINDOW_START, window_end=V3G_WINDOW_END)

    candles_ok = cand_audit["complete"]
    pool_ok = coverage["pool_d2"]["pool_coverage_complete_for_window"]
    coverage["backtest_claim"] = (
        "full_26d" if candles_ok and pool_ok else "partial_or_gapped — see candles/pool_d2"
    )

    if not candles_ok:
        print(
            f"WARN: candle coverage incomplete loaded={cand_audit['loaded']} expected={cand_audit['expected']}",
            file=sys.stderr,
        )

    variants = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}
    tr: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    for code, var in variants.items():
        tr[code] = pe.execute_variant(
            var, copy.deepcopy(pending), ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        sm = _summary_metrics(tr[code], ctx["touch_stats"])
        sm["admitted"] = _admitted_candidates(
            var, pending, ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        summary[code] = sm
        _write_trades_csv(OUT_DIR / f"l1_v3g_variant_{code.lower()}_trades.csv", tr[code])

    cand_rows = []
    for row in pending:
        if not row.get("entry_utc"):
            continue
        base = {k: v for k, v in row.items() if not str(k).startswith("_")}
        dec = _filter_decisions(row, variants)
        base["filter_A"], base["filter_B"], base["filter_C"] = dec["A"], dec["B"], dec["C"]
        cand_rows.append(base)
    with (OUT_DIR / "l1_v3g_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if cand_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in cand_rows for k in r}))
            w.writeheader()
            w.writerows(cand_rows)

    list_ab, list_bc, slot_rows = _filter_diff_rows(tr["A"], tr["B"], tr["C"], pending)
    coverage["regression_z1"] = {}
    for code in ("A", "B", "C"):
        gross = _z1_realized_gross(tr[code])
        ref = V3F_REGRESSION.get(code, {}).get("gross_realized")
        coverage["regression_z1"][code] = {
            "realized_gross_z1": round(gross, 4),
            "v3f_ref": ref,
            "match": ref is not None and abs(gross - ref) < 0.02,
        }

    (OUT_DIR / "l1_v3g_data_coverage_report.json").write_text(
        json.dumps(coverage, indent=2, default=str) + "\n", encoding="utf-8"
    )

    md = [
        "# L1 V3G — Backtest 25.08.–20.09.2026",
        "",
        f"- **Daten vollständig (Kerzen):** {'ja' if candles_ok else 'NEIN'} "
        f"({cand_audit['loaded']}/{cand_audit['expected']}, Lücken={cand_audit['gap_count']})",
        f"- **Pool-SSOT vollständig (Proben):** {'ja' if pool_ok else 'NEIN'} "
        f"(Fehler={len(coverage['pool_d2']['pool_failures'])})",
        f"- **Backtest-Anspruch:** {coverage['backtest_claim']}",
        f"- drawings SHA256: `{manifest['sha256']}` | V3F-vergleichbar: **{comparable_v3f}**",
        f"- D2: `{D2_COMPUTE_VERSION}` | Filter: Nähe≤{MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT}% Q1≥{MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER}",
        "",
        "_Linien mit `extend_right` bleiben über Anker hinaus aktiv (`ON_EXTENDED_RAY`) — retrospektiv._",
        "",
        "## Gesamtergebnis",
        "",
        "| Kennzahl | A | B | C |",
        "| --- | ---: | ---: | ---: |",
        f"| Touch-Events | {summary['A']['touch_events']} | = | = |",
        f"| Entry-Kandidaten | {summary['A']['entry_candidates']} | = | = |",
        f"| Zugelassene Kandidaten | {summary['A']['admitted']} | {summary['B']['admitted']} | {summary['C']['admitted']} |",
        f"| Ausgeführte Trades | {summary['A']['executed']} | {summary['B']['executed']} | {summary['C']['executed']} |",
        f"| Gewinner | {summary['A']['win_count']} | {summary['B']['win_count']} | {summary['C']['win_count']} |",
        f"| Stop-Loss | {summary['A']['loss_count']} | {summary['B']['loss_count']} | {summary['C']['loss_count']} |",
        f"| Kanal-TP | {summary['A']['tp_channel']} | {summary['B']['tp_channel']} | {summary['C']['tp_channel']} |",
        f"| Pool-TP | {summary['A']['tp_pool']} | {summary['B']['tp_pool']} | {summary['C']['tp_pool']} |",
        f"| Offen am Ende | {summary['A']['open_count']} | {summary['B']['open_count']} | {summary['C']['open_count']} |",
        f"| Summe Gewinne % | {summary['A']['wins_sum']:.4f} | {summary['B']['wins_sum']:.4f} | {summary['C']['wins_sum']:.4f} |",
        f"| Summe Verluste % | {summary['A']['losses_sum']:.4f} | {summary['B']['losses_sum']:.4f} | {summary['C']['losses_sum']:.4f} |",
        f"| **Gesamtbrutto %** | **{summary['A']['gross_realized_sum']:.4f}** | **{summary['B']['gross_realized_sum']:.4f}** | **{summary['C']['gross_realized_sum']:.4f}** |",
        f"| Ablehnungen D2-Nähe | 0 | {summary['B']['reject_first_d2']} | {summary['C']['reject_first_d2']} |",
        f"| Ablehnungen Staffelung | 0 | 0 | {summary['C']['reject_stagger']} |",
        "",
        "## Zeiträume (Exit-UTC-Zuordnung)",
        "",
    ]
    for code in ("A", "B", "C"):
        segs = _segment_pnl(tr[code])
        md.append(f"### Variante {code}")
        md.append("| Segment | exec | Gewinne | SL | Σ Gew % | Σ Verl % | Brutto % |")
        md.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, label in (
            ("Z1_25Aug_01Sep", "25.08.–01.09."),
            ("Z2_01Sep_10Sep", "01.09.–10.09."),
            ("Z3_10Sep_20Sep", "10.09.–20.09."),
        ):
            s = segs[name]
            md.append(
                f"| {label} | {s['exec']} | {s['wins']} | {s['sl']} | {s['w_sum']:.4f} | "
                f"{s['l_sum']:.4f} | {s['gross']:.4f} |"
            )
        z1g = _z1_realized_gross(tr[code])
        ref = V3F_REGRESSION.get(code, {}).get("gross_realized", "—")
        md.append(f"- Z1 vs V3F-Ref brutto {ref}: **{z1g:.4f}** (match={coverage['regression_z1'][code]['match']})")
        md.append("")

    md.extend(["## Filtervergleich", ""])
    md.append("### A ausgeführt, B blockiert (gleiches touch_event_id)")
    for r in list_ab:
        md.append(
            f"- `{r['entry_utc']}` {r['drawing_id']}: A={r['A_exit']} {r['A_pnl']}% "
            f"→ B {r['B/C_exit']} ({r['filter_B_or_C']}) near={r['nearest_d2_pct']}% Q={r['d2_quarter_bands']}"
        )
    if not list_ab:
        md.append("_Keine._")
    md.append("")
    md.append("### B ausgeführt, C blockiert")
    for r in list_bc:
        md.append(f"- `{r['entry_utc']}` {r['drawing_id']}: B={r['A_exit']} → C {r['B/C_exit']} ({r['filter_B_or_C']})")
    if not list_bc:
        md.append("_Keine unterschiedliche Ausführung B vs C._")
    md.append("")
    md.append("### B ausgeführt, A nicht (gleiches Event / Slot)")
    for r in slot_rows:
        md.append(f"- `{r['entry_utc']}` {r['drawing_id']}: A={r['A_exit']} B/C={r['B/C_exit']} PnL_A={r['A_pnl']}")

    for code, label in (("A", "Variante A"), ("B", "Variante B"), ("C", "Variante C")):
        md.extend(_trade_sections(tr[code], label))

    (OUT_DIR / "l1_v3g_comparison_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(
        f"V3G candles={cand_audit['loaded']}/{cand_audit['expected']} pool_fail={len(coverage['pool_d2']['pool_failures'])}"
    )
    print(
        f"A gross={summary['A']['gross_realized_sum']:.4f} B={summary['B']['gross_realized_sum']:.4f} "
        f"C={summary['C']['gross_realized_sum']:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
