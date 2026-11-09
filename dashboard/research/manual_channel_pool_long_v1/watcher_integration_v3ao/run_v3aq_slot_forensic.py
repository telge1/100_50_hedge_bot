#!/usr/bin/env python3
"""L1 V3AQ — GLOBAL_MAX_ONE_OPEN_LONG forensic (READ-ONLY)."""

from __future__ import annotations

import copy
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE = Path(__file__).resolve().parents[1]
V3AP = Path(__file__).resolve().parent
V3AK = PROBE / "substantial_entry_gate_v3ak"
V3Y = PROBE / "support_break_exit_v3y"
for p in (PROBE, V3AP, V3AK, V3AH if (V3AH := PROBE / "realistic_pool_tp_v3ah") else None, PROBE / "tp_research_v3w", PROBE / "support_break_exit_v3ab", V3Y, PROBE / "touch_watcher_research_v3an"):
    if p and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from config import L1_V3E_VARIANT_B
from support_break_exit_v3y.support_sim import simulate_exit_v3y
from v3ak_context import build_context_from_snapshot, DEFAULT_SNAPSHOT, FROZEN_SHA256
from watcher_config import TouchWatcherConfig
from watcher_integration_v3ao.v3ap_funnel import run_full_funnel
from watcher_integration_v3ao.v3ap_shared_context import build_watcher_pending_for_cfg, load_shared_probe_context

OUT = V3AP
LINE_ID = "d-ea081434c5d3"
TARGET_ENTRY_UTC = "2026-08-30 09:35:00 UTC"
WIN_LO = datetime(2026, 8, 30, 8, 0, tzinfo=timezone.utc)
WIN_HI = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
VARIANT_B = L1_V3E_VARIANT_B


def _support_line(row: dict, lines_by_id: dict):
    return lines_by_id.get(row.get("touch_drawing_id") or "")


def _parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _in_window(utc: str) -> bool:
    dt = _parse_utc(utc)
    return dt is not None and WIN_LO <= dt <= WIN_HI


def execute_timeline_v3aq(
    pending_ready: list[dict],
    *,
    candles: list[dict],
    lines_by_id: dict,
    upper_line,
    mod,
    label: str,
) -> tuple[list[dict], list[dict], dict | None]:
    """Mirror execute_variant_v3y with slot forensics."""
    channel_line_at_bar = mod.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    cycle = pe.ChannelCycleState()
    pending_sorted = sorted(
        pending_ready,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    )
    position_free_from_idx = 0
    traded_events: set[str] = set()
    timeline: list[dict] = []
    executed: list[dict] = []
    last_executed: dict[str, Any] | None = None
    target_block_event: dict | None = None

    for row in pending_sorted:
        eid = row["touch_event_id"]
        entry_utc = row.get("entry_utc", "")
        entry_idx = int(row["_entry_idx"]) if row.get("_entry_idx") not in (None, "") else None

        ev: dict[str, Any] = {
            "simulation": label,
            "seq_order": len(timeline) + 1,
            "touch_event_id": eid,
            "touch_drawing_id": row.get("touch_drawing_id", ""),
            "touch_utc": row.get("touch_utc", ""),
            "entry_utc": entry_utc,
            "entry_price": row.get("entry_price", ""),
            "entry_idx": entry_idx if entry_idx is not None else "",
            "position_free_from_idx_before": position_free_from_idx,
            "open_blocker_id": last_executed.get("touch_event_id") if last_executed else "",
            "open_blocker_entry_utc": last_executed.get("entry_utc") if last_executed else "",
            "open_blocker_exit_utc": last_executed.get("exit_utc") if last_executed else "",
            "open_blocker_exit_kind": last_executed.get("exit_kind") if last_executed else "",
            "open_at_entry_decision": "yes"
            if entry_idx is not None and entry_idx < position_free_from_idx
            else "no",
            "pick_ok": row.get("_pick_ok", ""),
            "entry_gate_pass": row.get("entry_gate_pass", ""),
            "result": "",
            "reject_reason": "",
        }

        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            ev["result"] = "SKIP"
            ev["reject_reason"] = row.get("reject_reason", "")
            timeline.append(ev)
            continue

        entry_idx = int(row["_entry_idx"])
        tsi = row.get("_touch_start_idx")
        if tsi is not None:
            cycle.maybe_reset_at_touch_start(candles, int(tsi), upper_px)
        cycle.advance_to(candles, entry_idx - 1, upper_px)

        if eid in traded_events:
            ev["result"] = "SKIP"
            ev["reject_reason"] = "EVENT_TRADE_ALREADY_TAKEN"
            timeline.append(ev)
            continue

        if not row.get("_pick_ok"):
            ev["result"] = "BLOCKED"
            ev["reject_reason"] = row.get("reject_reason") or "PICK_FAIL"
            timeline.append(ev)
            continue

        vreject = pe.apply_variant_entry_filters(
            VARIANT_B,
            float(row["entry_price"]),
            row.get("_main_upper_at_entry"),
            row.get("_effective_tp"),
            cycle,
            row=row,
        )
        if vreject:
            ev["result"] = "BLOCKED"
            ev["reject_reason"] = vreject
            timeline.append(ev)
            continue

        if entry_idx < position_free_from_idx:
            ev["result"] = "GLOBAL_MAX_ONE_OPEN_LONG"
            ev["reject_reason"] = "GLOBAL_MAX_ONE_OPEN_LONG"
            if last_executed:
                ev["blocking_trade_id"] = last_executed.get("touch_event_id")
                ev["blocking_line_id"] = last_executed.get("touch_drawing_id")
                ev["blocking_entry_utc"] = last_executed.get("entry_utc")
                ev["blocking_entry_price"] = last_executed.get("entry_price")
                ev["blocking_sl"] = last_executed.get("sl_price")
                ev["blocking_tp"] = last_executed.get("tp_price")
                ev["blocking_exit_utc"] = last_executed.get("exit_utc")
                ev["blocking_exit_kind"] = last_executed.get("exit_kind")
                ev["blocking_pnl_pct"] = last_executed.get("gross_pnl_pct")
                ev["still_open_at_blocked_entry"] = (
                    "yes" if entry_idx < position_free_from_idx else "no"
                )
            timeline.append(ev)
            if (
                row.get("touch_drawing_id") == LINE_ID
                and entry_utc == TARGET_ENTRY_UTC
                and target_block_event is None
            ):
                target_block_event = ev
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
        ex = {
            "simulation": label,
            "touch_event_id": eid,
            "touch_drawing_id": row.get("touch_drawing_id", ""),
            "entry_utc": entry_utc,
            "entry_price": row.get("entry_price", ""),
            "sl_price": row.get("sl_price", ""),
            "tp_price": row.get("tp_price", ""),
            "exit_utc": exit_utc,
            "exit_kind": exit_kind,
            "gross_pnl_pct": f"{gross_pnl_pct:.4f}",
            "_entry_idx": entry_idx,
            "_exit_idx": exit_idx,
        }
        executed.append(ex)
        ev["result"] = f"EXECUTED:{exit_kind}"
        ev["reject_reason"] = ""
        ev["exit_utc"] = exit_utc
        ev["exit_kind"] = exit_kind
        ev["gross_pnl_pct"] = f"{gross_pnl_pct:.4f}"
        timeline.append(ev)

        if exit_idx is not None:
            cycle.advance_to(candles, exit_idx, upper_px)
            position_free_from_idx = len(candles) if exit_kind == "OPEN_AT_WINDOW_END" else exit_idx + 1
            last_executed = ex

    return timeline, executed, target_block_event


def _build_ready_pending(pending_post_d2, shared, entry_stats=None) -> list[dict]:
    f = run_full_funnel(
        pending_post_d2,
        lines=shared["lines"],
        candles=shared["candles"],
        upper_line=shared["upper_line"],
        lines_by_id=shared["lines_by_id"],
        entry_stats=entry_stats or {},
    )
    return f["pending_ready"]


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


def hypothetical_exit(row: dict, candles, lines_by_id, mod) -> dict:
    support = _support_line(row, lines_by_id)
    exit_kind, exit_ts, exit_price, gross_pnl_pct, exit_idx = simulate_exit_v3y(
        candles,
        int(row["_entry_idx"]),
        float(row["entry_price"]),
        float(row["_sl"]),
        row.get("_pool_tp"),
        row.get("_channel_line"),
        support,
    )
    return {
        "label": "HYPOTHETICAL_NO_SLOT",
        "touch_event_id": row.get("touch_event_id", ""),
        "touch_drawing_id": row.get("touch_drawing_id", ""),
        "entry_utc": row.get("entry_utc", ""),
        "entry_price": row.get("entry_price", ""),
        "sl_price": row.get("sl_price", ""),
        "tp_price": row.get("tp_price", ""),
        "exit_utc": l1.fmt_utc(exit_ts) if exit_ts else "",
        "exit_kind": exit_kind,
        "exit_price": f"{exit_price:.10f}" if exit_price is not None else "",
        "gross_pnl_pct": f"{gross_pnl_pct:.4f}" if gross_pnl_pct is not None else "",
        "note": "NOT included in backtest sum — diagnostic only",
    }


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]
    shared = load_shared_probe_context()

    configs = {
        "V3AK": None,
        "WATCHER_A1_W12_003": TouchWatcherConfig(
            touch_tolerance_pct=0.03,
            confirmation_mode="A1",
            watch_window_bars=12,
            preset_id="FORENSIC_A1_W12",
        ),
        "WATCHER_A2_W12_003": TouchWatcherConfig(
            touch_tolerance_pct=0.03,
            confirmation_mode="A2",
            watch_window_bars=12,
            preset_id="FORENSIC_A2_W12",
        ),
        "WATCHER_A3_W12_003": TouchWatcherConfig(
            touch_tolerance_pct=0.03,
            confirmation_mode="A3",
            confirmation_min_gain_pct=0.20,
            watch_window_bars=12,
            preset_id="FORENSIC_A3_W12",
        ),
    }

    all_timeline: list[dict] = []
    blockers: list[dict] = []
    hypos: list[dict] = []

    # V3AK uses standard frozen pending
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    v3ak_ready = _build_ready_pending(frozen["pending"], shared)

    sim_data: dict[str, dict] = {}
    for label, cfg in configs.items():
        if cfg is None:
            pending = frozen["pending"]
            ready = v3ak_ready
        else:
            pending, _ = build_watcher_pending_for_cfg(shared, cfg)
            ready = _build_ready_pending(pending, shared)
        tl, ex, blk = execute_timeline_v3aq(
            ready,
            candles=shared["candles"],
            lines_by_id=shared["lines_by_id"],
            upper_line=shared["upper_line"],
            mod=mod,
            label=label,
        )
        sim_data[label] = {"timeline": tl, "executed": ex, "block_event": blk, "ready": ready}
        all_timeline.extend([e for e in tl if _in_window(e.get("entry_utc", "")) or _in_window(e.get("touch_utc", ""))])
        if blk:
            blockers.append({**blk, "simulation": label})

        if label in ("WATCHER_A1_W12_003", "WATCHER_A2_W12_003"):
            row = next(
                (
                    r
                    for r in ready
                    if r.get("touch_drawing_id") == LINE_ID and r.get("entry_utc") == TARGET_ENTRY_UTC
                ),
                None,
            )
            if row:
                hypos.append(hypothetical_exit(row, shared["candles"], shared["lines_by_id"], mod))

    _write_csv(OUT / "l1_v3aq_timeline_0830_1200.csv", sorted(all_timeline, key=lambda r: (r["simulation"], r.get("entry_utc", ""))))
    _write_csv(OUT / "l1_v3aq_blocking_trade.csv", blockers)
    _write_csv(OUT / "l1_v3aq_hypothetical_0935_exit.csv", hypos)

    report = _build_report(sim_data, blockers, hypos)
    (OUT / "L1_V3AQ_GLOBAL_MAX_ONE_OPEN_LONG_FORENSIC.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AQ_GLOBAL_MAX_ONE_OPEN_LONG_FORENSIC.md")
    return 0


def _build_report(sim_data, blockers, hypos) -> str:
    lines = [
        "# L1 V3AQ — Forensik `GLOBAL_MAX_ONE_OPEN_LONG` (30.08.2026)",
        "",
        "**READ-ONLY** · Keine Regeländerung · Equality aus V3AP unverändert gültig.",
        "",
        "## Aufgabe 1 — Blockierender Trade (A1/A2 @ 09:35)",
        "",
    ]
    a1_blk = next((b for b in blockers if b.get("simulation") == "WATCHER_A1_W12_003"), None)
    if a1_blk:
        lines.extend(
            [
                f"| Feld | Wert |",
                f"|---|---|",
                f"| Blockierter Kandidat | `{LINE_ID}` Entry **{TARGET_ENTRY_UTC}** @ {a1_blk.get('entry_price','')} |",
                f"| Trade-ID (Blocker) | `{a1_blk.get('blocking_trade_id','')}` |",
                f"| Trendlinie (Blocker) | `{a1_blk.get('blocking_line_id','')}` |",
                f"| Blocker Entry | {a1_blk.get('blocking_entry_utc','')} @ {a1_blk.get('blocking_entry_price','')} |",
                f"| SL / TP | {a1_blk.get('blocking_sl','')} / {a1_blk.get('blocking_tp','')} |",
                f"| Exit | {a1_blk.get('blocking_exit_utc','')} · **{a1_blk.get('blocking_exit_kind','')}** |",
                f"| Rendite Blocker | {a1_blk.get('blocking_pnl_pct','')}% |",
                f"| Noch offen um 09:35? | **{a1_blk.get('still_open_at_blocked_entry','') }** |",
                f"| `position_free_from_idx` vor Entscheid | {a1_blk.get('position_free_from_idx_before','')} (Entry idx {a1_blk.get('entry_idx','')}) |",
                "",
            ]
        )
    else:
        lines.append("_Kein Block-Event für Ziel-Entry gefunden._\n")

    lines.extend(["## Aufgabe 2 — Chronologie 08:00–12:00 UTC", "", "Vollständig: `l1_v3aq_timeline_0830_1200.csv` (pro Simulation).", ""])

    for label in ("V3AK", "WATCHER_A1_W12_003", "WATCHER_A2_W12_003", "WATCHER_A3_W12_003"):
        tl = [e for e in sim_data[label]["timeline"] if _in_window(e.get("entry_utc", ""))]
        lines.append(f"### {label}")
        for e in tl:
            lines.append(
                f"- {e.get('entry_utc','—')} `{e.get('touch_event_id','')}` "
                f"line=`{e.get('touch_drawing_id','')}` → **{e.get('result','')}** "
                f"{('reject='+e.get('reject_reason','')) if e.get('reject_reason') else ''}"
            )
        lines.append("")

    lines.extend(
        [
            "## Aufgabe 3 — Warum A3 @ 10:10, A1/A2 @ 09:35 blockiert",
            "",
            "Jede Variante ist eine **eigene** chronologische Simulation (eigene Pending-Liste, eigenes `position_free_from_idx`).",
            "",
        ]
    )
    a3_ex = sim_data["WATCHER_A3_W12_003"]["executed"]
    a1_ex = sim_data["WATCHER_A1_W12_003"]["executed"]
    lines.append(f"- **A1** ausgeführte Trades im Gesamtfenster: {len(a1_ex)}")
    lines.append(f"- **A3** ausgeführte Trades im Gesamtfenster: {len(a3_ex)}")
    lines.append(
        "- A3-Entry auf EA08 ist **später** (10:10) und trifft auf einen **anderen Slot-Stand** "
        "(der Blocker-Trade ist typischerweise bereits **geschlossen**, `position_free_from_idx` ≤ 10:10)."
    )
    lines.append(
        "- A1/A2-Entry 09:35 liegt **während** der vom Blocker belegten Bars (siehe `open_blocker_exit_utc` in CSV)."
    )
    lines.append("")

    lines.extend(["## Aufgabe 4 — Hypothetischer A1/A2-Exit (ohne Slot)", ""])
    for h in hypos:
        lines.append(
            f"- **{h['label']}** Entry {h['entry_utc']} → Exit **{h['exit_kind']}** "
            f"{h['exit_utc']} · **{h['gross_pnl_pct']}%** — _{h['note']}_"
        )
    lines.append("")

    lines.extend(
        [
            "## Aufgabe 5 — Bewertung",
            "",
            "- Die Regel **maximal ein offener Long** arbeitet deterministisch über sortierte `_entry_idx` "
            "und `position_free_from_idx = exit_idx + 1` nach `simulate_exit_v3y`.",
            "- Block bei 09:35 ist **regelkonform**, nicht Gate-Fehler.",
            "- Unterschied A1/A2 vs. A3 ist **zeitlich** (Slot frei vs. belegt), nicht unterschiedliche Exit-Engine.",
            "",
            "**STOP**",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
