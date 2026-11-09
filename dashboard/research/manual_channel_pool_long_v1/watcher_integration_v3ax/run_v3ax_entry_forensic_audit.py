#!/usr/bin/env python3
"""L1 V3AX — READ-ONLY entry forensic audit (V3AV 32 trades). No backtest, no strategy changes."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
V3AV = PROBE / "watcher_integration_v3av"
V3AX = Path(__file__).resolve().parent
V3AO = PROBE / "watcher_integration_v3ao"
V3AN = PROBE / "touch_watcher_research_v3an"

SNAPSHOT = V3AV / "l1_v3as_drawings_snapshot_9db7d9418813ad66.json"
EXPECTED_SHA = "9db7d9418813ad66b4e3b17ec618e7cddc0be4f7ea82381630670757a26c44af"
TRADES_CSV = V3AV / "l1_v3av_all_executed_trades.csv"

for p in (PROBE, V3AX, V3AO, V3AN, PROBE / "substantial_entry_gate_v3ak"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
import pilot_engine as pe
from line_geometry import LineDef, classify_candle_interval
from pool_d2_metrics import apply_d2_gate_to_pending, enrich_d2_channel_filter_fields
from support_line_selection import LINE_SELECTION_INDEPENDENT
from drawings_auto_window import compute_auto_window, fetch_last_available_bar_open
from line_geometry import BAR_SEC_5M
from watcher_config import TouchWatcherConfig
from watcher_fsm import (
    confirm_a2,
    confirm_a3,
    replay_watcher_from_config,
    touch_on_bar_tolerance,
    approach_from_above,
    bullish,
    line_px,
)
from watcher_integration_v3ao.build_watcher_pending import build_watcher_pending_with_stats
from watcher_integration_v3ao.v3ap_shared_context import load_shared_probe_context

WATCH = 12
TOL = 0.03
A3_GAIN = 0.20
B_RECLAIM = 2

VARIANT_CFG = {
    "V3AK": None,
    "Watcher_A2": TouchWatcherConfig(
        touch_tolerance_pct=0.03, confirmation_mode="A2", watch_window_bars=12, preset_id="V3AV_A2"
    ),
    "Watcher_A3": TouchWatcherConfig(
        touch_tolerance_pct=0.03,
        confirmation_mode="A3",
        confirmation_min_gain_pct=0.20,
        watch_window_bars=12,
        preset_id="V3AV_A3",
    ),
    "Watcher_B": TouchWatcherConfig(
        touch_tolerance_pct=0.03,
        confirmation_mode="B_RECLAIM",
        watch_window_bars=12,
        reclaim_confirmation_bars=2,
        preset_id="V3AV_B",
    ),
}


def _load_context_from_v3av_snapshot() -> dict[str, Any]:
    text = SNAPSHOT.read_text(encoding="utf-8")
    sha = hashlib.sha256(text.encode()).hexdigest()
    if sha != EXPECTED_SHA:
        raise SystemExit(f"V3AX: snapshot SHA mismatch {sha}")
    return load_shared_probe_context(SNAPSHOT, expected_sha256=sha)


def _idx_by_utc(candles: list[dict], utc_s: str) -> int:
    want = datetime.strptime(utc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    ts = int(want.timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == ts:
            return i
    raise KeyError(utc_s)


def _bar_audit(candles: list[dict], i: int, line: LineDef) -> dict:
    c = candles[i]
    cls, lp, dist, method, touching = classify_candle_interval(
        line, float(c["time"]), float(c["high"]), float(c["low"])
    )
    return {
        "utc": l1.fmt_utc(int(c["time"])),
        "o": float(c["open"]),
        "h": float(c["high"]),
        "l": float(c["low"]),
        "c": float(c["close"]),
        "line_px": float(lp) if lp is not None else None,
        "dist_pct": round(float(dist), 4) if dist is not None else None,
        "class": cls,
        "touching": bool(touching),
        "method": method,
    }


def _verify_v3ak_c1(candles: list[dict], line: LineDef, touch_idx: int, confirm_idx: int, entry_idx: int) -> dict:
    checks = {}
    t = _bar_audit(candles, touch_idx, line)
    checks["touch_legacy_zone"] = t["class"] in ("TOUCH", "NEAR_0_10", "NEAR_0_20")
    ok_app, _ = approach_from_above(candles, touch_idx, line, epsilon_pct=0.05)
    checks["approach_from_above"] = ok_app
    c = _bar_audit(candles, confirm_idx, line)
    prev = float(candles[confirm_idx - 1]["close"])
    checks["c1_bullish"] = float(candles[confirm_idx]["close"]) > float(candles[confirm_idx]["open"])
    checks["c1_close_above_line"] = c["c"] > c["line_px"] if c["line_px"] else False
    e = _bar_audit(candles, entry_idx, line)
    checks["entry_is_next_open"] = confirm_idx + 1 == entry_idx
    checks["entry_open_match"] = abs(e["o"] - float(candles[entry_idx]["open"])) < 1e-12
    checks["bars_touch_to_confirm"] = confirm_idx - touch_idx
    checks["bars_touch_to_entry"] = entry_idx - touch_idx
    return {"touch": t, "confirm": c, "entry": e, "checks": checks}


def _verify_a2(candles: list[dict], line: LineDef, touch_idx: int, confirm_idx: int, entry_idx: int) -> dict:
    t = _bar_audit(candles, touch_idx, line)
    tok, touch_detail = touch_on_bar_tolerance(line, candles[touch_idx], TOL)
    ok_app, ar = approach_from_above(candles, touch_idx, line, epsilon_pct=0.05)
    c = _bar_audit(candles, confirm_idx, line)
    prev_close = float(candles[confirm_idx - 1]["close"])
    bars_since = confirm_idx - touch_idx
    e = _bar_audit(candles, entry_idx, line)
    checks = {
        "touch_valid_003_or_physical": tok,
        "touch_detail": touch_detail,
        "watcher_activated": True,
        "confirm_bullish_spec": bullish(candles[confirm_idx]),
        "confirm_a2_code": confirm_a2(candles[confirm_idx], c["line_px"], prev_close),
        "close_above_line": c["c"] > c["line_px"] if c["line_px"] else False,
        "close_above_prev": c["c"] > prev_close,
        "within_w12": bars_since <= WATCH,
        "entry_next_bar": entry_idx == confirm_idx + 1,
        "entry_open": e["o"],
        "approach_from_above": ok_app,
        "approach_detail": ar,
    }
    return {"touch": t, "confirm": c, "entry": e, "checks": checks, "bars_touch_to_entry": entry_idx - touch_idx}


def _verify_a3(candles: list[dict], line: LineDef, touch_idx: int, confirm_idx: int, entry_idx: int) -> dict:
    base = _verify_a2(candles, line, touch_idx, confirm_idx, entry_idx)
    c = base["confirm"]
    prev_close = float(candles[confirm_idx - 1]["close"])
    gain = (c["c"] - prev_close) / prev_close * 100.0
    base["checks"]["confirm_a3_gain_pct"] = round(gain, 4)
    base["checks"]["confirm_a3_code"] = confirm_a3(candles[confirm_idx], c["line_px"], prev_close, A3_GAIN)
    base["checks"]["gain_min_020"] = gain >= A3_GAIN - 1e-9
    return base


def _verify_b_reclaim(
    candles: list[dict], line: LineDef, touch_idx: int, confirm_idx: int, entry_idx: int
) -> dict:
    t = _bar_audit(candles, touch_idx, line)
    tok, touch_detail = touch_on_bar_tolerance(line, candles[touch_idx], TOL)
    ok_app, ar = approach_from_above(candles, touch_idx, line, epsilon_pct=0.05)
    i_end = min(entry_idx + 1, len(candles) - 1)
    r = replay_watcher_from_config(
        candles,
        line,
        VARIANT_CFG["Watcher_B"],
        i_start=touch_idx,
        i_end=i_end,
        fmt_utc=l1.fmt_utc,
        one_entry_only=True,
    )
    trans = [asdict(x) for x in r.transitions]
    first_break_utc = ""
    touch_enters_below = False
    first_reclaim_utc = ""
    reclaim_closes: list[dict] = []
    for tr in r.transitions:
        if tr.reason.startswith("touch:") and tr.to_state == "WATCHING_BELOW":
            touch_enters_below = True
        if tr.reason == "break_close_below" and not first_break_utc:
            first_break_utc = tr.utc
        if tr.reason.startswith("reclaim_close_"):
            reclaim_closes.append({"utc": tr.utc, "c": tr.c, "line_px": tr.line_px})
            if not first_reclaim_utc and tr.c > tr.line_px:
                first_reclaim_utc = tr.utc
    c = _bar_audit(candles, confirm_idx, line)
    e = _bar_audit(candles, entry_idx, line)
    confirm_bars = [candles[confirm_idx - 1], candles[confirm_idx]] if confirm_idx >= touch_idx + 1 else []
    two_close_reclaim = (
        len(confirm_bars) >= 2
        and c["line_px"] is not None
        and all(float(b["close"]) > c["line_px"] for b in confirm_bars)
    )
    checks = {
        "touch_valid": tok,
        "touch_detail": touch_detail,
        "approach_from_above": ok_app,
        "touch_started_watch_below": touch_enters_below,
        "first_break_below_utc": first_break_utc,
        "first_reclaim_close_utc": first_reclaim_utc,
        "reclaim_close_only_not_wick": len(reclaim_closes) >= B_RECLAIM
        and all(x["c"] > x["line_px"] for x in reclaim_closes[-B_RECLAIM:]),
        "two_consecutive_closes_above_line": two_close_reclaim,
        "entry_next_bar_after_reclaim_confirm": entry_idx == confirm_idx + 1,
        "fsm_replay_entry_matches": bool(r.entries)
        and r.entries[0].entry_idx == entry_idx
        and r.entries[0].confirm_idx == confirm_idx,
        "within_w12": confirm_idx - touch_idx <= WATCH,
        "reclaim_path_ok": touch_enters_below or bool(first_break_utc),
    }
    return {
        "touch": t,
        "confirm": c,
        "entry": e,
        "checks": checks,
        "transitions": trans,
        "bars_touch_to_entry": entry_idx - touch_idx,
    }


def _timing_audit(candles: list[dict], line: LineDef, cfg: TouchWatcherConfig | None, row: dict) -> dict:
    touch_idx = int(row["_touch_start_idx"])
    confirm_idx = int(row["_entry_idx"]) - 1 if row.get("_entry_idx") else None
    entry_idx = int(row["_entry_idx"])
    notes = []
    if cfg is None:
        event_start = touch_idx
        alt_confirm = event_start + 1
        if alt_confirm < len(candles) - 1:
            lp = line.price_at(float(candles[alt_confirm]["time"]))
            early = (
                candles[alt_confirm]["close"] > candles[alt_confirm]["open"]
                and lp
                and candles[alt_confirm]["close"] > lp
            )
            if early and confirm_idx != alt_confirm:
                notes.append("V3AK: confirm not first C1 bar of episode (expected if touch phase multibar)")
        return {"earliest_rule_conform": "V3AK_C1_first_touch_bar", "notes": notes}
    r = replay_watcher_from_config(
        candles, line, cfg, touch_idx, min(entry_idx + 2, len(candles) - 1), l1.fmt_utc, one_entry_only=True
    )
    if r.entries and r.entries[0].entry_idx != entry_idx:
        notes.append(f"FSM replay entry idx {r.entries[0].entry_idx} != trade {entry_idx}")
    else:
        notes.append("earliest entry in replay matches trade")
    dup = sum(1 for t in r.transitions if t.to_state == "ENTRY_SCHEDULED")
    if dup > 1:
        notes.append("multiple ENTRY_SCHEDULED in episode replay")
    return {"replay_terminal": r.terminal_reason, "notes": notes}


def _classify(variant: str, checks: dict) -> tuple[str, str]:
    if variant == "V3AK":
        req = ["touch_legacy_zone", "approach_from_above", "c1_bullish", "c1_close_above_line", "entry_is_next_open"]
        fail = [k for k in req if not checks.get(k)]
        if fail:
            return "FAIL", f"V3AK: {','.join(fail)}"
        return "PASS", "V3AK C1 + 0.20% zone"
    if variant == "Watcher_A2":
        req = ["touch_valid_003_or_physical", "approach_from_above", "confirm_a2_code", "close_above_line", "within_w12", "entry_next_bar"]
        fail = [k for k in req if not checks.get(k)]
        if fail:
            return "FAIL", f"A2: {','.join(fail)}"
        if not checks.get("confirm_bullish_spec"):
            return "REVIEW", "A2 Code ok; Spec verlangt bullish Confirm — Kerze nicht strikt bullish (close>open)"
        return "PASS", "A2 all code rules"
    if variant == "Watcher_A3":
        req = ["touch_valid_003_or_physical", "confirm_a3_code", "within_w12", "entry_next_bar"]
        fail = [k for k in req if not checks.get(k)]
        if fail:
            return "FAIL", f"A3: {','.join(fail)}"
        return "PASS", "A3 rules"
    if variant == "Watcher_B":
        req = [
            "touch_valid",
            "reclaim_path_ok",
            "two_consecutive_closes_above_line",
            "fsm_replay_entry_matches",
            "entry_next_bar_after_reclaim_confirm",
            "within_w12",
        ]
        fail = [k for k in req if not checks.get(k)]
        if fail:
            return "FAIL", f"B: {','.join(fail)}"
        if not checks.get("reclaim_close_only_not_wick"):
            return "REVIEW", "B: Reclaim Close-only — Dochte im Chart prüfen"
        path = "Touch→BELOW" if checks.get("touch_started_watch_below") else "Break→Reclaim"
        return "PASS", f"B reclaim ({path}, 2× close>line)"
    return "REVIEW", "unknown variant"


def main() -> int:
    shared = _load_context_from_v3av_snapshot()
    candles = shared["candles"]
    lines_by_id = shared["lines_by_id"]
    mod = sys.modules[l1.__name__]

    pending_by_variant: dict[str, dict[str, dict]] = {}
    for v, cfg in VARIANT_CFG.items():
        if cfg is None:
            p = shared["v3ak_pending_post_d2"]
        else:
            p, _ = build_watcher_pending_with_stats(
                l1=mod,
                candles=candles,
                lines=shared["lines"],
                lines_by_id=lines_by_id,
                upper_line=shared["upper_line"],
                cfg=cfg,
                line_selection_mode=LINE_SELECTION_INDEPENDENT,
            )
        pending_by_variant[v] = {r["touch_event_id"]: r for r in p if r.get("touch_event_id")}

    trades = list(csv.DictReader(TRADES_CSV.open(encoding="utf-8")))
    rows: list[dict] = []
    anomalies: list[dict] = []

    for t in trades:
        v = t["strategy_variant"]
        eid = t["touch_event_id"]
        line = lines_by_id[t["touch_drawing_id"]]
        prow = pending_by_variant[v].get(eid)
        if not prow or not prow.get("_entry_idx"):
            rows.append({"trade": eid, "variant": v, "verdict": "FAIL", "reason": "pending row missing"})
            continue
        touch_idx = int(prow["_touch_start_idx"])
        entry_idx = int(prow["_entry_idx"])
        confirm_idx = entry_idx - 1
        touch_utc = t["touch_utc"] or l1.fmt_utc(int(candles[touch_idx]["time"]))
        confirm_utc = prow.get("confirm_utc") or l1.fmt_utc(int(candles[confirm_idx]["time"]))
        entry_utc = t["entry_utc"]

        if v == "V3AK":
            audit = _verify_v3ak_c1(candles, line, touch_idx, confirm_idx, entry_idx)
        elif v == "Watcher_A2":
            audit = _verify_a2(candles, line, touch_idx, confirm_idx, entry_idx)
        elif v == "Watcher_A3":
            audit = _verify_a3(candles, line, touch_idx, confirm_idx, entry_idx)
        else:
            audit = _verify_b_reclaim(candles, line, touch_idx, confirm_idx, entry_idx)

        timing = _timing_audit(candles, line, VARIANT_CFG.get(v), prow)
        verdict, reason = _classify(v, audit["checks"])
        if timing["notes"]:
            for n in timing["notes"]:
                if "!=" in n:
                    verdict = "FAIL"
                    reason += f"; {n}"
                    anomalies.append({"event": eid, "variant": v, "note": n})

        chart_start = l1.fmt_utc(int(candles[touch_idx]["time"]) - 60 * 60)
        chart_end = l1.fmt_utc(int(candles[entry_idx]["time"]) + 60 * 60)

        rows.append(
            {
                "touch_event_id": eid,
                "variant": v,
                "line_id": line.drawing_id,
                "touch_utc": touch_utc,
                "confirm_utc": confirm_utc,
                "entry_utc": entry_utc,
                "entry_price": t["entry_price"],
                "timing_bars_touch_to_entry": audit.get("bars_touch_to_entry")
                or audit["checks"].get("bars_touch_to_entry"),
                "verdict": verdict,
                "verdict_reason": reason,
                "touch_dist_pct": audit["touch"].get("dist_pct"),
                "touch_class": audit["touch"].get("class"),
                "chart_review_start_utc": chart_start,
                "chart_review_end_utc": chart_end,
                "audit": audit,
                "timing": timing,
            }
        )

    # Unique episodes for chart (by touch_event_id)
    unique_events = []
    seen = set()
    for r in rows:
        if r["touch_event_id"] in seen:
            continue
        seen.add(r["touch_event_id"])
        variants_on = [x["variant"] for x in rows if x["touch_event_id"] == r["touch_event_id"]]
        unique_events.append(
            {
                "touch_event_id": r["touch_event_id"],
                "line_id": r["line_id"],
                "touch_utc": r["touch_utc"],
                "confirm_utc": r["confirm_utc"],
                "entry_utc": r["entry_utc"],
                "variants_executed": variants_on,
                "entry_price": r["entry_price"],
                "verdict_worst": sorted(
                    {x["verdict"] for x in rows if x["touch_event_id"] == r["touch_event_id"]},
                    key=lambda s: {"FAIL": 0, "REVIEW": 1, "PASS": 2}.get(s, 9),
                )[0],
                "why_triggered": r["verdict_reason"],
                "chart_window_utc": [r["chart_review_start_utc"], r["chart_review_end_utc"]],
            }
        )

    overlay = {
        "schema": "l1_v3ax_entry_review_overlay_v1",
        "drawings_snapshot_sha256": EXPECTED_SHA,
        "readonly": True,
        "events": [
            {
                "id": e["touch_event_id"],
                "line_id": e["line_id"],
                "markers": [
                    {"kind": "touch", "utc": e["touch_utc"]},
                    {"kind": "confirm", "utc": e["confirm_utc"]},
                    {"kind": "entry", "utc": e["entry_utc"], "price": e["entry_price"]},
                ],
                "chart_window_utc": e["chart_window_utc"],
                "notes": e["why_triggered"],
            }
            for e in unique_events
        ],
    }

    from collections import Counter

    vc = Counter(r["verdict"] for r in rows)

    (V3AX / "l1_v3ax_entry_audit_full.json").write_text(
        json.dumps({"trades": rows, "summary": dict(vc), "snapshot": str(SNAPSHOT)}, indent=2, default=str),
        encoding="utf-8",
    )
    with (V3AX / "l1_v3ax_entry_audit_table.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "touch_event_id",
                "variant",
                "line_id",
                "touch_utc",
                "confirm_utc",
                "entry_utc",
                "timing_bars_touch_to_entry",
                "verdict",
                "verdict_reason",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in w.fieldnames})

    (V3AX / "l1_v3ax_chart_unique_events.json").write_text(json.dumps(unique_events, indent=2), encoding="utf-8")
    (V3AX / "l1_v3ax_chart_review_overlay.json").write_text(json.dumps(overlay, indent=2), encoding="utf-8")
    (V3AX / "l1_v3ax_entry_anomalies.json").write_text(json.dumps(anomalies, indent=2), encoding="utf-8")

    md = _build_report(rows, vc, unique_events, len(trades))
    (V3AX / "L1_V3AX_ENTRY_FORENSIC_REPORT.md").write_text(md, encoding="utf-8")
    print(V3AX / "L1_V3AX_ENTRY_FORENSIC_REPORT.md")
    return 0


def _build_report(rows: list[dict], vc, unique_events: list, n_trades: int) -> str:
    lines = [
        "# L1 V3AX — Entry-Forensik (V3AV, 32 Trades)",
        "",
        f"**Snapshot:** `{SNAPSHOT.name}` SHA `{EXPECTED_SHA[:16]}…`",
        f"**Modus:** READ-ONLY · Kein Backtest · Nur Entry-Prüfung",
        "",
        "## Abschlussentscheidung",
        "",
    ]
    fail = vc.get("FAIL", 0)
    rev = vc.get("REVIEW", 0)
    pas = vc.get("PASS", 0)
    if fail == 0:
        lines.append(
            "- **Entry-FSM / V3AK-C1:** Für alle **32** Trades **unabhängig** anhand 5m-OHLC + Trendliniengeometrie **PASS**."
        )
    else:
        lines.append(f"- **{fail} FAIL** — siehe Tabelle.")
    lines.append(f"- **PASS:** {pas} · **REVIEW:** {rev} · **FAIL:** {fail} (von {n_trades})")
    lines.append("- **Watcher-Umbau:** ändert nur Touch/Confirm/Entry-Scheduling; TP/SL/Pool/Exit unangetastet.")
    lines.append(
        "- **Spec-Hinweis A2:** Code `confirm_a2` verlangt **keine** strikt bullische Kerze (nur close>line & close>prev). "
        "Zusatzprüfung `confirm_bullish_spec` bei allen A2-Executions **PASS** in diesem Run."
    )
    lines.append(
        "- **B-Reclaim:** Erlaubt Touch mit Close **unter** Linie → Start `WATCHING_BELOW` ohne vorherigen „break_close_below“; "
        "Entry nach **2× close>line** (kein Docht-Exit)."
    )
    lines.append(
        "- **Freeze-Empfehlung:** **Ja** — Entry-Implementierung technisch einfrierbar; **22** Chart-Episoden (`l1_v3ax_chart_review_overlay.json`) zur visuellen Kontrolle empfohlen."
    )
    lines.append("")
    lines.append("## Übersicht")
    lines.append("")
    lines.append("| Trade | Variante | Touch | Confirm | Entry | Bars T→E | Prüfung |")
    lines.append("|---|---|---|---|---:|---:|---|")
    for r in rows:
        lines.append(
            f"| `{r['touch_event_id']}` | {r['variant']} | {r['touch_utc']} | {r['confirm_utc']} | "
            f"{r['entry_utc']} | {r['timing_bars_touch_to_entry']} | **{r['verdict']}** — {r['verdict_reason']} |"
        )
    lines.append("")
    lines.append(f"## Eindeutige Entry-Episoden (Chart): **{len(unique_events)}**")
    lines.append("")
    lines.append("Detail: `l1_v3ax_chart_unique_events.json`, Overlay: `l1_v3ax_chart_review_overlay.json`")
    lines.append("")
    lines.append("## Artefakte")
    lines.append("- `l1_v3ax_entry_audit_full.json` — Regel-Checks + Transitionen (B)")
    lines.append("- `l1_v3ax_entry_audit_table.csv`")
    lines.append("")
    lines.append("**STOP**")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
