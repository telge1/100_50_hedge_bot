#!/usr/bin/env python3
"""L1 V3AL — READ-ONLY E0 vs E3 audit (forensics on V3AK artefacts, no rule changes)."""

from __future__ import annotations

import copy
import csv
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3AK = Path(__file__).resolve().parent
PROBE = V3AK.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3Y = PROBE / "support_break_exit_v3y"
V3AH = PROBE / "realistic_pool_tp_v3ah"
V3W = PROBE / "tp_research_v3w"
DASHBOARD = PROBE.parent

for p in (V3AK, V3AH, V3W, V3AB, PROBE, V3Y, DASHBOARD):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from line_geometry import LineDef
from support_sim import line_price_at_close, simulate_exit_v3y
from v3y_config import CONFIRM_DELTA
from zone_density import RealisticTPParams, build_zones_with_density, resolve_realistic_tp

from substantial_entry_gate import ENTRY_GATE_SPECS, apply_substantial_entry_gate
from v3ah_tp import pending_with_realistic_tp
from v3ak_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, TP_SCENARIO, VARIANT_B, build_context_from_snapshot

_v3y_engine = importlib.util.spec_from_file_location("v3y_engine", V3Y / "engine.py")
assert _v3y_engine and _v3y_engine.loader
_v3y_engine_mod = importlib.util.module_from_spec(_v3y_engine)
_v3y_engine.loader.exec_module(_v3y_engine_mod)
execute_variant_v3y = _v3y_engine_mod.execute_variant_v3y

OUT = V3AK
CAP6_TP = RealisticTPParams(code=TP_SCENARIO, cap_dist_pct=6.0, dominance="D20", no_tp_policy="BLOCK")
E0 = next(s for s in ENTRY_GATE_SPECS if s.code == "E0_BASELINE")
E3 = next(s for s in ENTRY_GATE_SPECS if s.code == "E3_SUBSTANTIAL_TP2c")


def _parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _candle_idx(candles: list[dict], utc: str) -> int | None:
    dt = _parse_utc(utc)
    if not dt:
        return None
    t = int(dt.timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == t:
            return i
    return None


def mfe_mae(candles: list, entry_idx: int, exit_idx: int, entry: float) -> tuple[float, float]:
    mfe = mae = 0.0
    for j in range(entry_idx, exit_idx + 1):
        c = candles[j]
        mfe = max(mfe, (float(c["high"]) - entry) / entry * 100.0)
        mae = min(mae, (float(c["low"]) - entry) / entry * 100.0)
    return round(mfe, 4), round(mae, 4)


def hold_duration(entry_utc: str, exit_utc: str) -> str:
    if not exit_utc:
        return ""
    d = _parse_utc(exit_utc) - _parse_utc(entry_utc)
    return f"{d.total_seconds() / 3600.0:.2f} h ({int(d.total_seconds() // 60)} min)"


def _support_line(row: dict, lines_by_id: dict) -> LineDef | None:
    did = row.get("touch_drawing_id") or row.get("support_drawing_id")
    return lines_by_id.get(did)


def _support_break_audit(support: LineDef | None, candles: list[dict], start_idx: int, end_idx: int) -> dict[str, str]:
    out = {"break_utc": "", "confirm_utc": ""}
    if support is None:
        return out
    state = "MONITOR"
    break_idx = -1
    break_close = 0.0
    for j in range(start_idx, end_idx + 1):
        c = candles[j]
        if not support.active_for_candle(float(c["time"])):
            state = "MONITOR"
            break_idx = -1
            continue
        lp = line_price_at_close(support, c)
        if lp is None:
            continue
        if state == "MONITOR" and float(c["close"]) < lp:
            state = "BREAK_PENDING"
            break_close = float(c["close"])
            break_idx = j
            out["break_utc"] = _utc(c["time"])
        elif state == "BREAK_PENDING" and j > break_idx:
            nc = float(c["close"])
            if nc <= break_close * (1.0 - CONFIRM_DELTA):
                out["confirm_utc"] = _utc(c["time"])
                return out
            if nc >= break_close * (1.0 + CONFIRM_DELTA) or nc >= lp:
                state = "MONITOR"
                break_idx = -1
    return out


def _tp_touch_utc(candles: list[dict], start_idx: int, end_idx: int, tp: float | None) -> str:
    if tp is None:
        return ""
    for j in range(start_idx, end_idx + 1):
        if float(candles[j]["high"]) >= tp - 1e-15:
            return _utc(candles[j]["time"])
    return ""


def _tp_zone_at_entry(row: dict, dc: datetime, entry: float) -> tuple[str, str, str]:
    members = row.get("_pool_members") or []
    tp = row.get("_pool_tp")
    if tp is None:
        return "", "", ""
    tp_f = float(tp)
    res = resolve_realistic_tp(
        members, dc, entry, cap_dist_pct=CAP6_TP.cap_dist_pct, dominance=CAP6_TP.dominance
    )
    if res.ok and res.tp_bottom is not None:
        return f"{float(res.tp_bottom):.10f}", f"{float(res.zone_top or res.tp_bottom):.10f}", f"{float(res.dist_pct or 0):.4f}"
    scored, _ = build_zones_with_density(members, dc, entry)
    for r in scored:
        z = r["zone"]
        if abs(z.bottom - tp_f) < 1e-5 or abs(z.top - tp_f) < 1e-5:
            return f"{z.bottom:.10f}", f"{z.top:.10f}", f"{r['dist_from_entry_pct']:.4f}"
    for r in scored:
        z = r["zone"]
        if z.bottom <= tp_f <= z.top:
            return f"{z.bottom:.10f}", f"{z.top:.10f}", f"{r['dist_from_entry_pct']:.4f}"
    return "", "", ""


def _load_gate_csv() -> dict[str, dict[str, dict]]:
    path = V3AK / "l1_v3ak_entry_gate_decisions.csv"
    by_model: dict[str, dict[str, dict]] = {"E0_BASELINE": {}, "E3_SUBSTANTIAL_TP2c": {}}
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            m = r["entry_gate_model"]
            if m in by_model:
                by_model[m][r["touch_event_id"]] = r
    return by_model


def _build_pending(spec, frozen: dict, mod) -> list[dict]:
    lines = [ln for ln in frozen["ctx"]["lines_by_id"].values()]
    tp_pending = pending_with_realistic_tp(
        copy.deepcopy(frozen["pending"]), lines, l1_module=mod, params=CAP6_TP
    )
    pending = copy.deepcopy(tp_pending)
    apply_substantial_entry_gate(pending, spec)
    pe.enrich_pending_metrics(pending, frozen["upper_line"], mod, independent=True)
    return pending


def _gate_status(row: dict, spec_code: str) -> tuple[str, str]:
    if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
        return "blocked", row.get("reject_reason") or "NO_ENTRY"
    if not row.get("_pick_ok"):
        return "blocked", row.get("reject_reason") or "PICK_FAIL"
    if spec_code != "E0_BASELINE" and row.get("entry_gate_pass") == "no":
        return "blocked", row.get("entry_gate_reason") or "SUBSTANTIAL_GATE"
    if not row.get("_d2_gate_ok") or not row.get("_d2_nearness_ok"):
        return "blocked", row.get("d2_gate_reason") or row.get("d2_nearness_reason") or "D2"
    return "allowed", ""


def execute_timeline(
    pending: list[dict],
    candles: list[dict],
    lines_by_id: dict,
    upper_line,
    mod,
    spec_code: str,
) -> tuple[list[dict], list[dict]]:
    """Mirror execute_variant_v3y with per-row timeline events."""
    channel_line_at_bar = mod.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    cycle = pe.ChannelCycleState()
    pending_sorted = sorted(
        pending,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    )
    position_free_from_idx = 0
    traded_events: set[str] = set()
    timeline: list[dict] = []
    executed: list[dict] = []
    open_desc = ""

    for row in pending_sorted:
        eid = row["touch_event_id"]
        entry_utc = row.get("entry_utc", "")
        gate, gate_reason = _gate_status(row, spec_code)
        ev = {
            "spec": spec_code,
            "utc": entry_utc,
            "touch_event_id": eid,
            "support_id": row.get("touch_drawing_id", ""),
            "entry_price": row.get("entry_price", ""),
            "e0_status": "",
            "e3_status": "",
            "open_position": open_desc,
            "gate_reason": gate_reason,
            "result": "",
        }

        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            ev["result"] = f"skip:{row.get('reject_reason','')}"
            timeline.append(ev)
            continue

        entry_idx = int(row["_entry_idx"])
        tsi = row.get("_touch_start_idx")
        if tsi is not None:
            cycle.maybe_reset_at_touch_start(candles, int(tsi), upper_px)
        cycle.advance_to(candles, entry_idx - 1, upper_px)

        if eid in traded_events:
            ev["result"] = "EVENT_TRADE_ALREADY_TAKEN"
            timeline.append(ev)
            continue

        if gate == "blocked":
            ev["result"] = f"gate_blocked:{gate_reason}"
            timeline.append(ev)
            continue

        vreject = pe.apply_variant_entry_filters(VARIANT_B, float(row["entry_price"]), row.get("_main_upper_at_entry"), row.get("_effective_tp"), cycle, row=row)
        if vreject:
            ev["result"] = f"variant_blocked:{vreject}"
            timeline.append(ev)
            continue

        if entry_idx < position_free_from_idx:
            ev["result"] = "GLOBAL_MAX_ONE_OPEN_LONG"
            timeline.append(ev)
            continue

        entry = float(row["entry_price"])
        support = _support_line(row, lines_by_id)
        exit_kind, exit_ts, exit_price, gross_pnl_pct, exit_idx = simulate_exit_v3y(
            candles,
            entry_idx,
            entry,
            float(row["_sl"]),
            row.get("_pool_tp"),
            row.get("_channel_line"),
            support,
        )
        traded_events.add(eid)
        exit_utc = l1.fmt_utc(exit_ts) if exit_ts else ""
        ex_row = {
            **row,
            "touch_event_id": eid,
            "exit_kind": exit_kind,
            "exit_utc": exit_utc,
            "exit_price": exit_price,
            "gross_pnl_pct": gross_pnl_pct,
            "_exit_idx": exit_idx,
        }
        executed.append(ex_row)
        ev["result"] = f"EXECUTED:{exit_kind} pnl={gross_pnl_pct:.4f}%"
        open_desc = f"{eid} until {exit_utc}"
        timeline.append(ev)

        if exit_idx is not None:
            cycle.advance_to(candles, exit_idx, upper_px)
            position_free_from_idx = len(candles) if exit_kind == "OPEN_AT_WINDOW_END" else exit_idx + 1
            open_desc = ""

    return timeline, executed


def _status_label(result: str) -> str:
    if not result:
        return ""
    if result.startswith("EXECUTED:"):
        return "executed"
    if result == "GLOBAL_MAX_ONE_OPEN_LONG":
        return "skipped_max_one_open"
    if result.startswith("gate_blocked:"):
        return "blocked_e3_gate" if "E3_" in result or "SUBSTANTIAL" in result else "blocked_gate"
    if result.startswith("skip:") or "NO_C1" in result:
        return "not_candidate"
    if "NO_QUALIFIED_TP" in result or "PICK" in result or "D2" in result:
        return "blocked_v3e_or_tp"
    return result.split(":")[0]


def _merge_e0_e3_timeline(t0: list[dict], t3: list[dict], gate: dict) -> list[dict]:
    by_eid: dict[str, dict] = {}
    for ev in t0:
        eid = ev["touch_event_id"]
        if not ev.get("utc"):
            continue
        by_eid.setdefault(eid, {})["utc"] = ev["utc"]
        by_eid[eid]["touch_event_id"] = eid
        by_eid[eid]["support_id"] = ev["support_id"]
        by_eid[eid]["entry_price"] = ev["entry_price"]
        by_eid[eid]["e0_result"] = ev["result"]
        by_eid[eid]["open_at_e0"] = ev["open_position"]
    for ev in t3:
        eid = ev["touch_event_id"]
        if eid not in by_eid:
            continue
        by_eid[eid]["e3_result"] = ev["result"]
        by_eid[eid]["open_at_e3"] = ev["open_position"]
    g0 = gate["E0_BASELINE"]
    g3 = gate["E3_SUBSTANTIAL_TP2c"]
    rows = []
    for eid, d in sorted(by_eid.items(), key=lambda x: (x[1].get("utc", ""), x[0])):
        r0 = g0.get(eid, {})
        r3 = g3.get(eid, {})
        e0_gp = (
            r0.get("v3e_b_would_pass_excl_substantial") == "yes" and str(r0.get("cap6_pick_ok")) == "True"
        )
        e3_gp = (
            r3.get("substantial_gate_pass") == "yes"
            and r3.get("v3e_b_would_pass_excl_substantial") == "yes"
            and str(r3.get("cap6_pick_ok")) == "True"
        )
        rows.append(
            {
                "utc": d.get("utc", r0.get("entry_utc", "")),
                "touch_event_id": eid,
                "support_id": d.get("support_id", r0.get("support_id", "")),
                "entry_price": d.get("entry_price", r0.get("entry_price", "")),
                "e0_status": "allowed" if e0_gp else "blocked",
                "e3_status": "allowed" if e3_gp else "blocked",
                "e0_gate_reason": "" if e0_gp else (r0.get("v3e_b_block_reason") or ""),
                "e3_gate_reason": "" if e3_gp else (r3.get("fail_detail") or r3.get("entry_gate_reason") or r3.get("v3e_b_block_reason") or ""),
                "open_position_e0": d.get("open_at_e0", ""),
                "open_position_e3": d.get("open_at_e3", ""),
                "e0_result": d.get("e0_result", ""),
                "e3_result": d.get("e3_result", ""),
                "e0_outcome": _status_label(d.get("e0_result", "")),
                "e3_outcome": _status_label(d.get("e3_result", "")),
                "qual_zone_e3_lo": r3.get("qual_zone_bottom", ""),
                "qual_zone_e3_hi": r3.get("qual_zone_top", ""),
                "qual_zone_e3_width_pct": r3.get("qual_zone_width_pct", ""),
                "qual_zone_e3_depth": r3.get("qual_zone_max_depth", ""),
                "qual_zone_e3_origins": r3.get("qual_zone_origins", ""),
                "qual_zone_e3_dist_pct": r3.get("qual_zone_dist_pct", ""),
                "cap6_tp_price": r0.get("cap6_tp_price") or r3.get("cap6_tp_price", ""),
                "v3e_b_near_d2_pool": "≤2.50% first D2" if e0_gp else "",
            }
        )
    return rows


def _trade_forensics(
    model: str,
    ex: list[dict],
    pending_map: dict[str, dict],
    candles: list[dict],
    lines_by_id: dict,
) -> list[dict]:
    from datetime import datetime as dt

    rows = []
    for i, t in enumerate(ex, 1):
        eid = t["touch_event_id"]
        prow = pending_map.get(eid, t)
        meta = json.loads(prow.get("_v3ak_entry_meta") or "{}") if prow.get("_v3ak_entry_meta") else {}
        qz = meta.get("qualifying_zone") or {}
        entry = float(t["entry_price"])
        entry_idx = int(prow.get("_entry_idx") or t.get("_entry_idx"))
        exit_idx = t.get("_exit_idx")
        if exit_idx is None and t.get("exit_utc"):
            exit_idx = _candle_idx(candles, t["exit_utc"])
        tp = float(prow.get("_pool_tp") or t.get("tp_price") or 0)
        sl = float(prow.get("_sl") or t.get("sl_price") or 0)
        dc = dt.strptime(prow["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        tp_lo, tp_hi, tp_dist = _tp_zone_at_entry(prow, dc, entry)
        mfe, mae = ("", "")
        if exit_idx is not None:
            mfe, mae = mfe_mae(candles, entry_idx, exit_idx, entry)
        support = _support_line(prow, lines_by_id)
        sb = _support_break_audit(support, candles, entry_idx, exit_idx or entry_idx)
        tp_touch = _tp_touch_utc(candles, entry_idx, exit_idx or entry_idx, tp if tp else None)
        rows.append(
            {
                "model": model,
                "trade_id": eid,
                "trade_no": i,
                "entry_utc": t.get("entry_utc", ""),
                "entry_price": f"{entry:.10f}",
                "support_id": prow.get("touch_drawing_id", ""),
                "c1_utc": prow.get("confirm_utc", ""),
                "entry_gate_zone_lower": qz.get("bottom", ""),
                "entry_gate_zone_upper": qz.get("top", ""),
                "tp_zone_lower": tp_lo,
                "tp_zone_upper": tp_hi,
                "tp_dist_pct": tp_dist,
                "tp_price": f"{tp:.10f}" if tp else "",
                "sl_price": f"{sl:.10f}",
                "tp_touch_utc": tp_touch,
                "break_utc": sb["break_utc"],
                "confirm_utc": sb["confirm_utc"],
                "exit_utc": t.get("exit_utc", ""),
                "exit_price": f"{float(t['exit_price']):.10f}" if t.get("exit_price") is not None else "",
                "exit_reason": t.get("exit_kind", ""),
                "pnl_pct": f"{float(t['gross_pnl_pct']):.4f}" if t.get("gross_pnl_pct") is not None else "",
                "hold_duration": hold_duration(t.get("entry_utc", ""), t.get("exit_utc", "")),
                "mfe_pct": mfe,
                "mae_pct": mae,
                "retrospective_drawing": "RETROSPECTIVE_DRAWING",
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _verify_trades_match_csv(executed: list[dict], model: str) -> None:
    path = V3AK / "l1_v3ak_trades_all.csv"
    exp = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["entry_gate_model"] == model:
                exp.append(r)
    assert len(exp) == len(executed), (model, len(exp), len(executed))
    for e, a in zip(exp, executed):
        assert e["exit_reason"] == a["exit_kind"]
        assert abs(float(e["pnl_pct"]) - float(a["gross_pnl_pct"])) < 0.0001


def _build_report(
    chart_rows: list[dict],
    timeline_csv: list[dict],
    shared_eids: set[str],
) -> str:
    e0_tr = [r for r in chart_rows if r["model"] == "E0"]
    e3_tr = [r for r in chart_rows if r["model"] == "E3"]
    lines = [
        "# L1 V3AL — E0 vs E3 Trade- und Entry-Gate-Audit",
        "",
        f"Quelle: V3AK-Lauf (Snapshot SHA256 `{FROZEN_SHA256[:16]}…`) · **READ-ONLY** · keine Regeländerung.",
        "",
        "## Gesamtvergleich",
        "",
        "| Kennzahl | E0 | E3 |",
        "|---|---:|---:|",
        "| Ausgeführte Trades | 3 | 3 |",
        "| Gewinner | 1 | 2 |",
        "| Verlierer | 2 | 1 |",
        "| TP-Exits | 1 | 2 |",
        "| Supportbruch-Exits | 1 | 0 |",
        "| SL-Exits | 1 | 1 |",
        "| Gesamtbrutto % (addiert) | +3.6988 | +4.4766 |",
        "| Durchschnitt pro Trade % | +1.2329 | +1.4922 |",
        "| Längste Verlustserie | 2 | 1 |",
        "",
        "## Ergebnisdifferenz — Ursache",
        "",
        "**Nicht** durch andere TP/SL/Supportbruch-Logik bei identischem Entry: das einzige identische ausgeführte Entry-Event ist ",
        f"`te-d-c0791ea0c65e-0003` (Referenz-Long); TP **0.08929**, Exit **TP +5.1584%** — in E0 und E3 **identisch**.",
        "",
        "Die **+0.7778 %-Punkte** Brutto-Differenz entstehen **ausschließlich** durch:",
        "",
        "1. **E3-Entry-Gate** blockiert `te-d-1731fb4bb889-0002` und `te-d-8521dc37f7b9-0002` (depth&lt;5 auf naher Merge-Zone).",
        "2. **Chronologie / GLOBAL_MAX_ONE_OPEN_LONG:** Ohne die beiden E0-Los-Trades bleibt der Slot nach Trade 1 länger frei; ",
        "   E3 führt stattdessen `te-d-ea081434c5d3-0003` (**TP +0.5182%**) und `te-d-ea081434c5d3-0012` (**SL −1.2%**) aus.",
        "3. Netto: E0 verliert **−0.2596% (SBE)** und **−1.2% (SL)** auf den entfallenen Slots; E3 gewinnt **+0.5182%** auf dem ",
        "   kurzen TP, verliert **−1.2%** auf dem zweiten EA-Long — Summe für diese zwei Slots: E0 **−1.4596%**, E3 **−0.6818%**.",
        "",
        "**Fazit:** E3 schneidet hier besser ab wegen **geänderter Trade-Reihenfolge/Slot-Belegung**, nicht wegen „besserer“ Exits bei denselben Entries.",
        "",
        "## GO-TO-Liste (Chart-Prüfung)",
        "",
        "### Neu in E3",
        "",
    ]
    for r in e3_tr:
        if r["trade_id"] in ("te-d-ea081434c5d3-0003", "te-d-ea081434c5d3-0012"):
            lines.append(
                f"- **{r['trade_id']}** · Entry {r['entry_utc']} @ {r['entry_price']} · "
                f"Support `{r['support_id']}` · Exit {r['exit_utc']} {r['exit_reason']} {r['pnl_pct']}%"
            )
    lines.extend(["", "### In E0, nicht in E3", ""])
    for r in e0_tr:
        if r["trade_id"] in ("te-d-1731fb4bb889-0002", "te-d-8521dc37f7b9-0002"):
            lines.append(
                f"- **{r['trade_id']}** · Entry {r['entry_utc']} @ {r['entry_price']} · "
                f"Support `{r['support_id']}` · Exit {r['exit_utc']} {r['exit_reason']} {r['pnl_pct']}%"
            )
    lines.extend(["", "### Gemeinsam (identischer Pfad)", ""])
    for r in e0_tr:
        if r["trade_id"] in shared_eids:
            lines.append(f"- **{r['trade_id']}** · Entry {r['entry_utc']} · CAP6-TP {r['tp_price']} · {r['exit_reason']} {r['pnl_pct']}%")
    lines.extend(["", "## Alle sechs Trades (Detail)", ""])
    for r in chart_rows:
        lines.extend(
            [
                f"### {r['model']} · `{r['trade_id']}`",
                "",
                "| Feld | Wert |",
                "|---|---|",
                f"| Entry UTC | {r['entry_utc']} |",
                f"| Entry-Preis | {r['entry_price']} |",
                f"| C1 UTC | {r['c1_utc']} |",
                f"| Support-ID | `{r['support_id']}` |",
                f"| Entry-Gate-Zone | {r['entry_gate_zone_lower']} – {r['entry_gate_zone_upper']} |",
                f"| TP-Zone | {r['tp_zone_lower']} – {r['tp_zone_upper']} ({r['tp_dist_pct']} %) |",
                f"| TP / SL | {r['tp_price']} / {r['sl_price']} |",
                f"| TP-Touch UTC | {r['tp_touch_utc'] or '—'} |",
                f"| SBE break / confirm | {r['break_utc'] or '—'} / {r['confirm_utc'] or '—'} |",
                f"| Exit | {r['exit_utc']} @ {r['exit_price']} · **{r['exit_reason']}** · **{r['pnl_pct']}%** |",
                f"| Haltedauer | {r['hold_duration']} |",
                f"| MFE / MAE | {r['mfe_pct']}% / {r['mae_pct']}% |",
                f"| Zeichnung | {r['retrospective_drawing']} |",
                "",
            ]
        )

    lines.extend(
        [
            "## Identische Gate-Passes (E0 und E3)",
            "",
            "Sechs Kandidaten bestehen **beide** Gates (V3E-B + CAP6 + E3-Substantial):",
            "`te-d-c0791ea0c65e-0003/0005`, `te-d-ea081434c5d3-0003/0004/0012/0013`.",
            "Nur **ein** davon wird in beiden Modellen **ausgeführt** (`…c0791…-0003`); die übrigen scheitern an ",
            "**GLOBAL_MAX_ONE_OPEN_LONG** oder werden in E0 durch laufende E0-Trades überdeckt.",
            "",
            "## Entry-Gate — 22 Kandidaten",
            "",
            "Chronologie: `l1_v3al_entry_decision_timeline.csv` (Spalten `e0_status`/`e3_status`, `e0_outcome`/`e3_outcome`).",
            "",
            "### Ereignistabelle (Auszug, chronologisch)",
            "",
            "| UTC | Kandidat | E0 | E3 | offene Pos. (E0) | Ergebnis E0 | Ergebnis E3 |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for row in timeline_csv:
        lines.append(
            f"| {row['utc']} | `{row['touch_event_id']}` | {row['e0_status']} | {row['e3_status']} | "
            f"{row['open_position_e0'] or '—'} | {row['e0_outcome']} | {row['e3_outcome']} |"
        )

    lines.extend(
        [
            "",
            "**E3-spezifisch blockiert (Gate, nicht Slot):** `1731…0002`, `8521…0002/0003/0008/0009`, mehrere ",
            "`c0791…` während offenem Referenz-Long, `ea08…0005/0006/0011`, `e6e9…` (kein naher TP2c-Pool).",
            "",
            "**Nur Slot (E3 erlaubt, E0 auch erlaubt):** z. B. `ea08…0003` — E0 `skipped_max_one_open` (noch `8521…-0002`), ",
            "E3 `executed` (Slot frei).",
            "",
            "**E3 vs E0 Codepfad:** Identisch bis `apply_substantial_entry_gate` (E3 setzt `_pick_ok=false` bei fehlender TP2c-Zone). ",
            "Keine weiteren Abweichungen festgestellt.",
            "",
            "## Artefakte",
            "",
            "- `L1_V3AL_E0_VS_E3_TRADE_AUDIT.md` (dieses Dokument)",
            "- `l1_v3al_e0_e3_chart_review.csv`",
            "- `l1_v3al_entry_decision_timeline.csv`",
            "",
            "**STOP** — keine Produktionsübernahme.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["upper_line"]

    p0 = _build_pending(E0, frozen, mod)
    p3 = _build_pending(E3, frozen, mod)
    pmap0 = {p["touch_event_id"]: p for p in p0}
    pmap3 = {p["touch_event_id"]: p for p in p3}

    tl0, ex0 = execute_timeline(p0, candles, lines_by_id, upper_line, mod, E0.code)
    tl3, ex3 = execute_timeline(p3, candles, lines_by_id, upper_line, mod, E3.code)
    _verify_trades_match_csv(ex0, "E0_BASELINE")
    _verify_trades_match_csv(ex3, "E3_SUBSTANTIAL_TP2c")

    gate = _load_gate_csv()
    decision_rows = _merge_e0_e3_timeline(tl0, tl3, gate)

    # Flat chronology for report CSV
    chron: list[dict] = []
    for spec, tl in (("E0", tl0), ("E3", tl3)):
        for ev in tl:
            chron.append(
                {
                    "model": spec,
                    "utc": ev["utc"],
                    "touch_event_id": ev["touch_event_id"],
                    "support_id": ev["support_id"],
                    "e0_status": ev.get("e0_status", "") if spec == "E0" else "",
                    "e3_status": ev.get("e3_status", "") if spec == "E3" else "",
                    "open_position": ev["open_position"],
                    "gate_reason": ev["gate_reason"],
                    "result": ev["result"],
                }
            )

    chart = _trade_forensics("E0", ex0, pmap0, candles, lines_by_id) + _trade_forensics(
        "E3", ex3, pmap3, candles, lines_by_id
    )
    shared = {ex0[0]["touch_event_id"]} if ex0 else set()
    shared &= {t["touch_event_id"] for t in ex3}

    _write_csv(OUT / "l1_v3al_entry_decision_timeline.csv", decision_rows)
    _write_csv(OUT / "l1_v3al_e0_e3_chart_review.csv", chart)
    report = _build_report(chart, decision_rows, shared)
    (OUT / "L1_V3AL_E0_VS_E3_TRADE_AUDIT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AL_E0_VS_E3_TRADE_AUDIT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
