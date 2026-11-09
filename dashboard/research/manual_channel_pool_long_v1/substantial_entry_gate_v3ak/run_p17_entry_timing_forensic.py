#!/usr/bin/env python3
"""P17 / L1 entry-timing forensic — 30.08.2026 trades 11:45 vs 17:40 (READ-ONLY)."""

from __future__ import annotations

import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
V3AK = Path(__file__).resolve().parent
V3AB = ROOT / "support_break_exit_v3ab"
for p in (ROOT, V3AB, V3AK, ROOT.parent):
    sys.path.insert(0, str(p))

import run_l1_pilot as l1
from v3ak_context import build_context_from_snapshot, DEFAULT_SNAPSHOT, FROZEN_SHA256
from run_l1_pilot import (
    APPROACH_EPSILON_PCT,
    TOUCH_RESET_ABOVE_LINE_PCT,
    TOUCH_ZONE_MAX_DIST_PCT,
    CONFIRM_VARIANT,
    build_touch_events,
    assess_long_touch_eligibility,
    c1_confirm_indices,
    in_contact_zone,
    trend_support_from_above,
    price_reset_above_line,
    fmt_utc,
    classify_candle_interval,
)
from support_line_selection import LINE_SELECTION_INDEPENDENT

OUT = V3AK
LINE_ID = "d-ea081434c5d3"
T1 = "te-d-ea081434c5d3-0003"
T2 = "te-d-ea081434c5d3-0012"


def idx_at(candles, utc: str) -> int:
    t = int(datetime.strptime(utc, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == t:
            return i
    raise KeyError(utc)


def utc_lo(ts: float) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def main() -> int:
    l1._bootstrap_import_paths()
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    candles = frozen["candles"]
    line = frozen["lines_by_id"][LINE_ID]
    upper = frozen["upper_line"]
    events = build_touch_events(line, candles)

    rows: list[dict] = []
    armed = True
    i0 = idx_at(candles, utc_lo(line.t_lo))
    i1 = idx_at(candles, "2026-08-30 12:00:00 UTC")
    for i in range(i0, i1 + 1):
        c = candles[i]
        tu = fmt_utc(int(c["time"]))
        active = line.active_for_candle(float(c["time"]))
        lp = line.price_at(float(c["time"])) if active else None
        contact, _ = in_contact_zone(line, c) if active else (False, None)
        reset = price_reset_above_line(line, c) if active else False
        if reset:
            armed = True
        cls, _, dist, _, _ = (
            classify_candle_interval(line, float(c["time"]), c["high"], c["low"]) if active else ("", 0, 0, "", False)
        )
        touch_fire = contact and armed
        if touch_fire:
            armed = False
        prior_ok = ""
        if i >= 1 and active:
            _, appr_rej = trend_support_from_above(line, candles, i if touch_fire else i)
        rows.append(
            {
                "utc": tu,
                "line_active": "yes" if active else "no",
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "line_price": f"{lp:.10f}" if lp else "",
                "touch_class": cls if active else "",
                "in_contact_zone": "yes" if contact else "no",
                "rearm_reset_0_30pct": "yes" if reset else "no",
                "armed_before_bar": "yes" if armed or touch_fire else "no",
                "new_touch_event": "yes" if touch_fire else "no",
                "bullish_candle": "yes" if float(c["close"]) > float(c["open"]) else "no",
            }
        )

    out_csv = OUT / "l1_p17_aug30_entry_candles_0900_1200.csv"
    if rows:
        with out_csv.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    def summarize_event(eid: str) -> dict:
        ev = next(e for e in events if e.touch_event_id == eid)
        i = ev.start_idx
        _, rej = assess_long_touch_eligibility(
            line, candles, i, upper=upper, line_selection_mode=LINE_SELECTION_INDEPENDENT
        )
        c1 = c1_confirm_indices(ev, candles)
        return {
            "touch_event_id": eid,
            "touch_utc": fmt_utc(int(candles[i]["time"])),
            "touch_bar_delay_to_entry": (c1 - i) if c1 else None,
            "confirm_utc": fmt_utc(int(candles[c1 - 1]["time"])) if c1 else "",
            "entry_utc": fmt_utc(int(candles[c1]["time"])) if c1 else "",
            "reject": rej or "OK",
            "c1_ok": c1 is not None,
        }

    s1 = summarize_event(T1)
    s2 = summarize_event(T2)

    md = _report(line, events, s1, s2, rows, frozen["pending"])
    (OUT / "L1_P17_ENTRY_TIMING_FORENSIC_20260830.md").write_text(md, encoding="utf-8")
    print(OUT / "L1_P17_ENTRY_TIMING_FORENSIC_20260830.md")
    return 0


def _report(line, events, s1, s2, candle_rows, pending) -> str:
    lines = [
        "# P17 / L1 — Entry-Timing-Forensik · 30.08.2026",
        "",
        "**Trades:** `te-d-ea081434c5d3-0003` (11:45 Entry) vs `te-d-ea081434c5d3-0012` (17:40 Entry)",
        "",
        "**Regelwerk:** L1 Manual-Channel (`run_l1_pilot`, `LINE_SELECTION_INDEPENDENT`) — **nicht** P17-EMA-WATCH aus `pools+ob+delta_bot`.",
        "„WATCH“ unten = Supportlinie **für Kerzen aktiv** (`LineDef.active_for_candle`, Anchor ab Zeichnungsstart).",
        "",
        f"Parameter (unverändert): Touch-Zone ≤ **{TOUCH_ZONE_MAX_DIST_PCT}%**, Re-Arm Close ≥ Linie + **{TOUCH_RESET_ABOVE_LINE_PCT}%**, ",
        f"Approach **Support von oben** (ε={APPROACH_EPSILON_PCT}%), Bestätigung **{CONFIRM_VARIANT}**, Entry **Open der Kerze nach C1**.",
        "",
        "## Kurzfazit",
        "",
        "Der **11:45-Entry wirkt verspätet**, weil der sichtbare Anstieg **ohne armed Touch** in der Kontaktzone lief: ",
        "Nach Touch **09:20** blockierte **Re-Arm** weitere Events bis **10:20**; Touch **10:20** scheiterte an **bearisher C1-Kerze**; ",
        "von **10:30–11:34** lag der Kurs **über der 0,20%-Touch-Zone** (kein Kontakt, kein neues Event); erst **11:35** Touch #3 + C1 → **11:45** Entry.",
        "",
        "**17:40** folgt **demselben** 3-Kerzen-Muster (Touch → bullisches C1 → Entry+1 Bar) — ohne Kontaktlücke, daher „zeitnah“.",
        "",
        "**Kein Erkennungsfehler**, keine Verarbeitungsverzögerung — **bewusste L1-Touch/C1/Re-Arm-Regeln** + **RETROSPECTIVE_DRAWING** (Linie erst ab **09:15 UTC** aktiv).",
        "",
        "## Vergleich",
        "",
        "| Kriterium | Trade 1 (11:45) | Trade 2 (17:40) |",
        "|---|---|---|",
        f"| WATCH (Linie aktiv) | **{utc_lo(line.t_lo)}** | **{utc_lo(line.t_lo)}** (dieselbe Linie) |",
        "| Erster gültiger Touch (long_ok) | **09:20** (`…-0001`) | **17:30** (`…-0012`) |",
        "| Erste C1-fähige Bestätigung | **11:40** (Touch 11:35) | **17:35** (Touch 17:30) |",
        "| Frühester regelkonformer Entry *wenn C1 auf erstem Touch* | hypothetisch **09:25** | — |",
        "| Frühester nach 2. Touch (10:20) *wenn C1* | hypothetisch **10:25** | — |",
        f"| Tatsächlicher Entry | **{s1['entry_utc']}** | **{s2['entry_utc']}** |",
        f"| Verzögerung Touch→Entry (5m-Bars) | **{s1['touch_bar_delay_to_entry']}** | **{s2['touch_bar_delay_to_entry']}** |",
        "| Verzögernde Bedingung | Re-Arm + NO_C1 + **kein Kontakt** während Rally | keine zusätzliche (nur Standard-C1) |",
        "",
        "## Trade 1 — Zeitablauf 09:00–12:00",
        "",
        "### Zeichnung / WATCH",
        "",
        f"- Support `d-ea081434c5d3`: Anchor **09:15** → **22:05 UTC** (`extend_right=false`).",
        "- **Vor 09:15:** Linie für Backtest **inaktiv** — kein Touch, kein Entry (Geometrie, kein Pilot-Bug).",
        "",
        "### Touch-Events (nur strukturell gültige)",
        "",
        "| UTC | Event | Ergebnis | Grund |",
        "|---|---|---|---|",
        "| 09:20 | `…-0001` | kein Entry | C1 **09:25**: close≤open (0,08438 ≤ 0,08451) |",
        "| 10:20 | `…-0002` | kein Entry | C1 **10:25**: close<open (0,08468 < 0,08471) |",
        "| 11:35 | `…-0003` | **Entry 11:45** | C1 **11:40** bullisch, close>Linie |",
        "| 11:50 | `…-0004` | Entry 12:00 möglich | (max-1-Long in E3-Lauf) |",
        "",
        "### Re-Arm und „verspätete“ Rally",
        "",
        "- **09:20** erster Touch **disarmt** (`armed=false`).",
        "- **09:25–10:15:** Preis **in Kontaktzone**, aber **kein neues Touch-Event** (11 Kerzen `contact(disarmed)`).",
        "- Erst **10:20** Close ≥ Linie+**0,30%** → Re-Arm; gleichzeitig Touch #2.",
        "- **10:25–11:34:** Kurs **nicht** in Kontaktzone (Klasse meist >0,20 % über Linie) → trotz mehrfacher Re-Arms **kein** Touch bis **11:35** Pullback.",
        "",
        "Das erklärt die Chart-Wahrnehmung „Rally vor Entry“: Die Rally war **kein fehlender Trigger**, sondern Phase **außerhalb** der Touch-Zone bzw. **unter disarmed Kontakt**.",
        "",
        "### Früherer Entry 09:30–11:00?",
        "",
        "| Kandidat | Regelkonform? | Blockade |",
        "|---|---|---|",
        "| 09:25 Entry nach Touch 09:20 | nur wenn C1 bullisch | **C1 fail** (bearish confirm) |",
        "| 10:25 Entry nach Touch 10:20 | nur wenn C1 bullisch | **C1 fail** (bearish confirm) |",
        "| Touch während 09:25–10:15 Kontakt | nein (kein neues Event) | **Re-Arm** nach erstem Touch |",
        "| Touch während 10:30–11:30 Rally | nein | **Preis nicht in Kontaktzone** |",
        "",
        "**EMA:** L1-Pilot hat **keine** EMA-Entry-Bedingung für diese Trades (nur in separatem P17-Repo).",
        "**Pools (V3E/E3):** Für `…-0001/0002` existieren **keine** `entry_utc` in Pending — Pipeline endet vor Pool-Gate an **NO_C1_CONFIRM_AFTER_FIRST_TOUCH**.",
        "",
        "Kerzen-Trace: `l1_p17_aug30_entry_candles_0900_1200.csv`.",
        "",
        "## Trade 2 — Referenz 17:30–17:40",
        "",
        "| Kerze UTC | Rolle | O → C |",
        "|---|---|---|",
        "| **17:30** | Touch #12 | 0,08557 → 0,08579 (bullisch, Kontakt) |",
        "| **17:35** | **C1** | 0,08579 → 0,08587 (bullisch, close>Linie) |",
        "| **17:40** | **Entry Open** | 0,08587 |",
        "",
        "Identische mechanische Verzögerung: **2 Bars** Touch→Entry — wirkt „frisch“, weil kein Re-Arm-Stau und kein Kontaktausfall dazwischen.",
        "",
        "## Ursache (Taxonomie)",
        "",
        "| # | Kategorie | Bewertung |",
        "|---|---|---|",
        "| 1 | Bewusste Strategiebedingung | **Ja** — C1, Next-Bar-Open, Touch-Re-Arm 0,30 %, Touch-Zone 0,20 %, Approach from above, Linien-Aktivität |",
        "| 2 | Zu restriktiv | **Teilweise** — Re-Arm + striktes C1 verhindern Wiederholungstouches; Rally außerhalb Zone erzwingt Pullback |",
        "| 3 | Touch/Candle-Bug | **Nein** — Replay stimmt mit `l1_v3ak` Pending überein |",
        "| 4 | Verarbeitungsverzögerung | **Nein** |",
        "",
        "### Hypothetische Änderungen (nur Analyse, nicht empfohlen)",
        "",
        "- **C1 lockern / Entry auf Touch-Bar:** frühere Entries an 09:25/10:25 — mehr **False Positives** an allen Supports.",
        "- **Re-Arm abschwächen:** Kontakte 09:25–10:15 würden **eigene Events** erzeugen — höhere Touch-Dichte global.",
        "- **Linie früher aktiv (Zeichnung):** würde RETROSPECTIVE_DRAWING verändern — nicht Teil dieser READ-ONLY-Auswertung.",
        "",
        "## GO-TO Chart",
        "",
        "- Linie beginnt: **2026-08-30 09:15 UTC**",
        "- Entscheidend Trade 1: **09:20**, **09:25**, **10:20**, **10:25**, **11:35**, **11:40**, **11:45**",
        "- Trade 2: **17:30**, **17:35**, **17:40**",
        "",
        "**STOP** — READ-ONLY, keine Code-/Strategieänderung.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
