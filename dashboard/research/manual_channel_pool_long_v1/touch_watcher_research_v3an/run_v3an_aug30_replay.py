#!/usr/bin/env python3
"""Isolated replay: 30.08.2026 09:15–12:00 · touch watcher variants (no L1 pilot changes)."""

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
from run_l1_pilot import build_touch_events, c1_confirm_indices, fmt_utc
from watcher_config import preset_matrix_v1, TouchWatcherConfig
from watcher_fsm import replay_watcher_from_config

LINE_ID = "d-ea081434c5d3"
OUT = V3AN


def idx_at(candles, utc: str) -> int:
    t = int(datetime.strptime(utc, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == t:
            return i
    return -1


def legacy_reference(candles, line, i0, i1) -> dict:
    events = build_touch_events(line, candles)
    rows = []
    for ev in events:
        tu = fmt_utc(int(candles[ev.start_idx]["time"]))
        if ev.start_idx < i0 or ev.start_idx > i1:
            continue
        c1 = c1_confirm_indices(ev, candles)
        rows.append(
            {
                "touch_event_id": ev.touch_event_id,
                "touch_utc": tu,
                "c1_confirm_utc": fmt_utc(int(candles[c1 - 1]["time"])) if c1 else "",
                "entry_utc": fmt_utc(int(candles[c1]["time"])) if c1 else "",
                "entry_price": float(candles[c1]["open"]) if c1 else "",
                "reject": "NO_C1" if c1 is None else "",
            }
        )
    executed = next((r for r in rows if r["entry_utc"] == "2026-08-30 11:45:00 UTC"), None)
    first = rows[0] if rows else None
    return {"events": rows, "executed_match": executed, "first_touch": first}


def main() -> int:
    l1._bootstrap_import_paths()
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    candles = frozen["candles"]
    line = frozen["lines_by_id"][LINE_ID]
    i0 = idx_at(candles, "2026-08-30 09:15:00 UTC")
    i1 = idx_at(candles, "2026-08-30 12:00:00 UTC")

    legacy = legacy_reference(candles, line, i0, i1)
    configs = preset_matrix_v1()

    summary: list[dict] = []
    for cfg in configs:
        r = replay_watcher_from_config(candles, line, cfg, i0, i1, fmt_utc)
        ent = r.entries[0] if r.entries else None
        doc = r.config_documented
        summary.append(
            {
                "config_id": r.config_id,
                "preset_id": cfg.preset_id,
                "schema_version": doc.get("schema_version", ""),
                "touch_tolerance_pct": doc.get("touch_tolerance_pct", ""),
                "touch_mode": r.touch_mode,
                "confirm": r.confirm_mode,
                "confirmation_min_gain_pct": doc.get("confirmation_min_gain_pct", ""),
                "reclaim_confirmation_bars": doc.get("reclaim_confirmation_bars", ""),
                "watch_window": r.watch_window,
                "touch_utc": ent.touch_utc if ent else "",
                "confirm_utc": ent.confirm_utc if ent else "",
                "entry_utc": ent.entry_utc if ent else "",
                "entry_price": ent.entry_price if ent else "",
                "sl_price": ent.sl_price if ent else "",
                "terminal": r.terminal_reason,
                "n_transitions": len(r.transitions),
                "first_transition": r.transitions[0].reason if r.transitions else "",
            }
        )
        if cfg.preset_id == "NEAR_003+A1+W12":
                    trans_path = OUT / "l1_v3an_sample_transitions_NEAR003_A1_W12.csv"
                    with trans_path.open("w", encoding="utf-8", newline="") as f:
                        wcsv = csv.DictWriter(
                            f,
                            fieldnames=[
                                "bar_idx",
                                "utc",
                                "from_state",
                                "to_state",
                                "reason",
                                "open",
                                "high",
                                "low",
                                "close",
                                "line_px",
                            ],
                        )
                        wcsv.writeheader()
                        for t in r.transitions:
                            wcsv.writerow(
                                {
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

    sum_path = OUT / "l1_v3an_aug30_variant_summary.csv"
    with sum_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(summary[0].keys()))
        w.writeheader()
        w.writerows(summary)

    _validate_v1_regression(OUT, summary)
    report = _build_report(legacy, summary)
    (OUT / "L1_V3AN_TOUCH_WATCHER_ANALYSIS_AND_PLAN.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AN_TOUCH_WATCHER_ANALYSIS_AND_PLAN.md")
    return 0


def _validate_v1_regression(out_dir: Path, summary: list[dict]) -> None:
    frozen = out_dir / "l1_v3an_aug30_variant_summary_v1frozen.csv"
    if not frozen.is_file():
        legacy_cols = [
            "config_id",
            "touch_mode",
            "confirm",
            "watch_window",
            "touch_utc",
            "confirm_utc",
            "entry_utc",
            "entry_price",
            "sl_price",
            "terminal",
        ]
        with frozen.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=legacy_cols, extrasaction="ignore")
            w.writeheader()
            for row in summary:
                w.writerow(row)
        return
    with frozen.open(encoding="utf-8") as f:
        baseline = {r["config_id"]: r for r in csv.DictReader(f)}
    mismatches: list[str] = []
    for row in summary:
        bid = row["config_id"]
        if bid not in baseline:
            mismatches.append(f"missing baseline row {bid}")
            continue
        b = baseline[bid]
        for k in ("entry_utc", "entry_price", "terminal", "touch_utc", "confirm_utc"):
            nv = str(row.get(k, ""))
            bv = str(b.get(k, ""))
            if nv != bv:
                mismatches.append(f"{bid}.{k}: new={nv!r} baseline={bv!r}")
    if mismatches:
        path = out_dir / "l1_v3an_regression_mismatches.txt"
        path.write_text("\n".join(mismatches), encoding="utf-8")
        raise SystemExit(f"Regression failed ({len(mismatches)} mismatches). See {path}")
    print(f"Regression OK: {len(summary)} configs match {frozen.name}")


def _build_report(legacy: dict, summary: list[dict]) -> str:
    def pick(tm, cm, w):
        return next((s for s in summary if s["touch_mode"] == tm and s["confirm"] == cm and s["watch_window"] == w), {})

    key_rows = [
        ("ORIGINAL (disarmed+C1 chain)", "LEGACY_020", "LEGACY_C1", 12),
        ("NEAR_003 + A1", "NEAR_003", "A1", 12),
        ("PHYSICAL + A1", "PHYSICAL_ONLY", "A1", 12),
        ("NEAR_003 + A2", "NEAR_003", "A2", 12),
        ("NEAR_003 + A3", "NEAR_003", "A3", 12),
        ("NEAR_003 + B reclaim", "NEAR_003", "B_RECLAIM", 12),
        ("PHYSICAL + B reclaim", "PHYSICAL_ONLY", "B_RECLAIM", 12),
    ]
    lines = [
        "# L1 V3AN — Touch-Watcher & Reclaim: Analyse und Implementierungsplan",
        "",
        "**Scope:** L1 Manual-Channel-Forschung (nicht P17). **V3AK unverändert.** Neues Modul: `touch_watcher_research_v3an/`.",
        "",
        "## Zustandsdiagramm (Vorschlag)",
        "",
        "```mermaid",
        "stateDiagram-v2",
        "    [*] --> IDLE",
        "    IDLE --> WATCHING_ABOVE: gültiger Touch (Approach von oben)",
        "    IDLE --> WATCHING_BELOW: Touch mit Close unter Linie",
        "    WATCHING_ABOVE --> WATCHING_BELOW: Close unter Linie (Break)",
        "    WATCHING_ABOVE --> ENTRY_SCHEDULED: Confirm A1/A2/A3/C1",
        "    WATCHING_BELOW --> RECLAIM_PENDING: 1. Close über Linie",
        "    RECLAIM_PENDING --> WATCHING_BELOW: Close wieder unter Linie",
        "    RECLAIM_PENDING --> ENTRY_SCHEDULED: 2. Close über Linie (B)",
        "    WATCHING_ABOVE --> EXPIRED: Fenster überschritten",
        "    WATCHING_BELOW --> EXPIRED: Fenster überschritten",
        "    RECLAIM_PENDING --> EXPIRED: Fenster überschritten",
        "    ENTRY_SCHEDULED --> [*]: Entry Open nächste Bar",
        "    EXPIRED --> IDLE: Reset (neuer Tag/Event)",
        "```",
        "",
        "**Deterministik:** Zustandswechsel nur auf **abgeschlossenen** 5m-Kerzen (Open-Zeit = Candle-ID). ",
        "Linie `line.price_at(candle.time)` wie `run_l1_pilot` / `classify_candle_interval`. ",
        "Während Watcher **kein** zweiter Parallel-Watcher (weitere Touches → ignoriert bis EXPIRED/Entry).",
        "",
        "## Replay 30.08.2026 · 09:15–12:00 · `d-ea081434c5d3`",
        "",
        "### Legacy-Referenz (unverändert `build_touch_events` + C1)",
        "",
    ]
    for e in legacy["events"][:5]:
        lines.append(
            f"- `{e['touch_event_id']}` touch **{e['touch_utc']}** → entry **{e['entry_utc'] or '—'}** "
            f"@ **{e['entry_price'] or '—'}** `{e['reject']}`"
        )
    lines.extend(
        [
            "",
            "**Prod-Entry:** `te-d-ea081434c5d3-0003` touch **11:35** → confirm **11:40** → entry **11:45 @ 0.08491**.",
            "",
            "### Varianten-Tabelle (Watcher-Replay, Fenster 12 Bars)",
            "",
            "| Variante | Touch UTC | Confirm UTC | Entry UTC | Entry | SL (1,2%) | Terminal |",
            "|---|---|---|---|---:|---:|---|",
        ]
    )
    for label, tm, cm, w in key_rows:
        s = pick(tm, cm, w)
        lines.append(
            f"| {label} | {s.get('touch_utc','—')} | {s.get('confirm_utc','—')} | {s.get('entry_utc','—')} | "
            f"{s.get('entry_price','')} | {s.get('sl_price','')} | {s.get('terminal','')} |"
        )

    lines.extend(
        [
            "",
            "Vollständig: `l1_v3an_aug30_variant_summary.csv` (3 Touch-Modi × 5 Confirm × 3 Fenster = 45 Zeilen).",
            "",
            "Beispiel-Trace: `l1_v3an_sample_transitions_NEAR003_A1_W12.csv`.",
            "",
            "## Replay-Erkenntnisse (09:15–12:00)",
            "",
            "1. **LEGACY_020:** Erster „Touch“ **09:20** (NEAR, kein Physik) startet disarmed-Kette → später **11:45** (wie V3AK).",
            "2. **NEAR_003:** Erster Touch typ. **09:25** (weiterhin NEAR) oder später **09:40** (phys. TOUCH) — **kein** 09:20-Phantom.",
            "3. **PHYSICAL_ONLY:** Erster Touch **09:40** → mit **A1** frühester Entry oft **09:50 @ ~0.08448** (Konfirm +1 Bar).",
            "4. **A2/A3** verzögern vs. A1 (stärkere Close-Bedingung).",
            "5. **B_RECLAIM:** Entry nur nach Break + 2× Close über Linie; im Fenster ggf. **expired** oder später als A.",
            "6. **Risiko früher Entry:** tieferer Einstieg, mehr Whipsaw / Break-Risiko — **nicht** automatisch besser (kein Outcome-Backtest hier).",
            "",
            "## Beobachtungsfenster (Vorschläge, keine Festlegung)",
            "",
            "| Fenster | Vorteil | Nachteil |",
            "|---|---|---|",
            "| **6 Bars (30 min)** | schnelle Invalidierung, weniger Zombies | verpasst langsame Reclaims (B) |",
            "| **12 Bars (1 h)** | Balance für 5m-Support-Touch | mehr parallele Zustände in Volatilität |",
            "| **24 Bars (2 h)** | Reclaim-Szenario B hat Zeit | alte Touch-Episode noch aktiv bei Trendwechsel |",
            "",
            "**Invalidierung bei anhaltendem Bruch (Vorschlag):** optionaler Zusatzübergang ",
            "`WATCHING_BELOW → INVALIDATED` wenn N aufeinanderfolgende Closes unter Linie **und** Abstand > X % — **nur Forschungsparameter**, ",
            "nicht als Produktionsdefault.",
            "",
            "## Szenario A / B Trennung",
            "",
            "- **A:** Watcher startet oberhalb/nach Touch; Confirm-Modul A1|A2|A3|LEGACY_C1; **kein** B-Reclaim-Pfad.",
            "- **B:** Nach `close < line` → `WATCHING_BELOW`; Reclaim = **zwei aufeinanderfolgende** `close > line`; Entry Open+1.",
            "- Mutual exclusion pro Episode: in **A** kann nach Break optional zu **B** wechseln (konfigurierbar); ",
            "  Forschungsdefault: **ein Modus pro Backtest-Run**.",
            "",
            "## Implementierungsplan (noch nicht in `run_l1_pilot`)",
            "",
            "| Schritt | Datei / Ort | Aktion |",
            "|---|---|---|",
            "| 1 | `touch_watcher_research_v3an/watcher_fsm.py` | FSM finalisieren (+ Tests) |",
            "| 2 | `touch_watcher_research_v3an/touch_detect.py` | Touch-Modi 0.03% / PHYSICAL / LEGACY |",
            "| 3 | `touch_watcher_research_v3an/run_replay_batch.py` | Multi-Support, Multi-Tag CSV |",
            "| 4 | **Neu** `run_l1_pilot_watcher.py` oder Flag in Pilot | **Paralleler** Pending-Builder; `run_l1_pilot.py` **unberührt** |",
            "| 5 | `pilot_engine.py` | Nur **Hook** `apply_variant_entry_filters` — keine Änderung an V3AK-Pfad |",
            "| 6 | Charts | Overlay: Watcher-Zustand als Metadaten-Spalte in Review-CSV |",
            "| 7 | Tests | Unit: `touch_on_bar`, Confirm A1–A3, Reclaim 2-Bar; Golden: Aug30 09:15–12:00 |",
            "| 8 | Backtest | Neuer Manifest-Prefix `V3AN_*`; SHA-Zeichnung unverändert |",
            "",
            "**Reproduzierbarkeit V3AK:** weiterhin `run_v3ak.py` + `TOUCH_ZONE_MAX_DIST_PCT=0.20` + `build_touch_events` Re-Arm.",
            "",
            "## Empfehlungen (Forschung, nicht Beschluss)",
            "",
            "1. **Touch:** `PHYSICAL_ONLY` oder `NEAR_003` statt `LEGACY_020` — adressiert chart-konform die 09:20-Fehlklassifikation.",
            "2. **Kein** klassischer 0,30%-Re-Arm für Watcher — stattdessen **EXPIRED** + IDLE; ein Episode = ein Watcher.",
            "3. **Confirm-Start:** **A1** oder **A2** gegen A3 testen (A3 verzögert oft unnötig auf 5m).",
            "4. **B** separat backtesten (eigene Kennzahlen), nicht mit A mischen.",
            "5. Pool/V3E/V3AK-Gates **nach** Watcher-Entry wie heute anbinden (`enrich_pending_metrics` auf synthetischen Rows).",
            "",
            "**STOP** — kein Produktionscode geändert. Nächster Schritt: breiter historischer Replay (isoliert), nicht V3AB/V3AK-Merge.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
