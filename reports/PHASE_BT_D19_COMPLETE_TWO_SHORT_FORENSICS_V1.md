# PHASE BT-D19 — Complete A-to-Z Forensic of Two Unfilled Short Signals

**Datum:** 2026-10-25  
**Projekt:** `pools+ob+delta_bot`  
**Verdict:** Forensik abgeschlossen — **kein Execution-Bug** für die beiden Fälle unter dokumentiertem Modell; **semantische Lücke** zwischen Chart-Intuition und Exact-Band-Identity (Fall A).

**Methodik:** Read-only — BT-D9-Config, SSOT-5m-OHLC (`price_feed_v1`), BT-D9-`validated_cache`, Engine-Replay (`short_research_engine_v1`), BT-D13-CSV, BT-D17/D18-Vergleichsindex. **Keine Code-/Config-Änderung.**

**Artefakte:**

| Datei | Pfad |
|--------|------|
| Timeline Fall A | `results/phase_bt_d19_complete_two_short_forensics_v1/case_a_timeline.csv` |
| Timeline Fall B | `results/phase_bt_d19_complete_two_short_forensics_v1/case_b_timeline.csv` |
| Density / Pool-Lifecycle | `results/phase_bt_d19_complete_two_short_forensics_v1/density_origin_lifecycle.json` |
| Engine-Trace | `results/phase_bt_d19_complete_two_short_forensics_v1/engine_trace.json` |
| Fill-Vergleich (Modi × Fill-Modelle) | `results/phase_bt_d19_complete_two_short_forensics_v1/fill_decision_comparison.csv` |
| Signal-Landschaft @ Close | `results/phase_bt_d19_complete_two_short_forensics_v1/signal_landscape_at_close.json` |
| BT-D13 (Vorarbeit) | `results/phase_bt_d13_two_short_fill_forensics_v1/pending_bars.csv` |

**1m-OHLC:** Im Repo **kein** provenienzgesicherter 1m-Feed angebunden an dieselbe BT-D9-Pipeline → intrabar-Reihenfolge innerhalb 5m: **INTRABAR_UNRESOLVED** (nur 5m-H/L/O/C).

**OHLC-Zeitsemantik:** Keys in `prices` = **5m-Bar-CLOSE UTC** (`bar_close_utc`). Open der Kerze = Close − 5 min (`bar_open_utc` in CSV). Kein Lookahead: Density `collect_at_close(bar)` = Stand **nach** Schluss dieser Kerze.

---

## Executive summary

| Fall | Enddiagnose (primär) | Sekundär |
|------|----------------------|----------|
| **A — 15:35 UTC** | **BACKTESTER_CORRECT_NO_FILL** | **POOL_STATUS_SEMANTIC_MISMATCH** |
| **B — 17:00 UTC** | **BACKTESTER_CORRECT_NO_FILL** | — |

**Warum im Chart „gut“, aber kein Trade?**

- Das **Signal** ist zum jeweiligen **Close** kausal gültig (Bearish-Rejection, Gap, Pool-Nähe, Depth-2-Density).
- Gehandelt wird nur eine **Pending Sell-Limit** ab **nächstem 5m-Open** (15:40 / 17:05).
- **Fall A:** In der **ersten aktiven Kerze** liegt **High unter allen getesteten Limits**; gleichzeitig fehlt das **exakte** Band `0.08681|0.086838` im Density-Snapshot (Band **re-segmentiert**, nicht nur „inactive“ im Chart-Sinn). Preis hatte die Zone **vor** Aktivierung berührt — das zählt für das Modell **nicht**.
- **Fall B:** Während der gesamten Pending-Zeit (227 Bars bis Testende) **nie** `high ≥ entry` (bestes High **0.08584** vs Limit **0.08593**). Der spätere Abverkauf Richtung TP ist **kein** Fill-Beweis.

**BT-D17 (lower / upper / offset 0,1 %) und BT-D18 (legacy_v1 / causal_sell_limit_v2):** Auf dem 24h-Cache **identisch 0 Fills** — vollständiger Engine-Re-Run, nicht nur Ledger-Ablesen.

---

## Pflichttabelle

| Feld | Fall A (15:35) | Fall B (17:00) |
|------|----------------|----------------|
| **Signalzeit (decision close)** | 2026-09-11T15:35:00Z | 2026-09-11T17:00:00Z |
| **Signalpreis (close)** | 0.08668 | 0.08575 |
| **Entry-Pool-Unterkante** | 0.086810 | 0.085930 |
| **Entry-Pool-Oberkante** | 0.086838 | 0.085982 |
| **Order-Aktivierung** | 2026-09-11T15:40:00Z | 2026-09-11T17:05:00Z |
| **Entry-Limit (lower_edge)** | 0.086810 | 0.085930 |
| **Open bei Aktivierung** | 0.086680 | 0.085750 |
| **High bei Aktivierung** | 0.086710 | 0.085840 |
| **Pool-Deaktivierungszeitpunkt (exact band)** | 2026-09-11T15:40:00Z (close) | — (Band bleibt active) |
| **Pool-Deaktivierungsursache** | Exact-Match `pool_still_active` false: Band **0.08681\|0.086838** fehlt; Cache zeigt **Split** (0.08681\|0.08682 + 0.08682\|0.086838), **+1 Origin** — **kein** SSOT-Feld „PRICE_TOUCH“ | — |
| **Fill-Entscheidung** | Kein Fill (High &lt; Limit; kein marketable Open) | Kein Fill (High &lt; Limit über gesamtes Pending) |
| **Cancel-Grund** | `CANCELLED_POOL_INACTIVE` @ 15:40 | Kein Cancel (Pending bis Testende) |
| **Späterer Kurs** | Unter Entry geblieben / Struktur shift | Deutlich unter TP-Zone (ab ~18:00); **nach** fehlendem Entry |
| **Endgültige Diagnose** | BACKTESTER_CORRECT_NO_FILL + POOL_STATUS_SEMANTIC_MISMATCH | BACKTESTER_CORRECT_NO_FILL |

---

## A. Rekonstruierte Quellen

| Quelle | Verwendung |
|--------|------------|
| `tools/phase_bt_d9_first_24h_short_pnl_v1/configs/bt_d9_24h_v1.json` | Kanonische Strategie-Config (ohne `pending_limit_fill_model` → **legacy_v1** Default) |
| `results/backtester_runs/bt_d9_24h_import_v2_snapshots/result.json` / `latest_result.json` | 3 Signale, 0 Trades |
| `reports/PHASE_BT_D13_TWO_SHORT_FILL_FORENSICS_V1.md` + `pending_bars.csv` | Bar-für-Bar Pending |
| `results/phase_bt_d17_*` / `phase_bt_d18_*` | Entry-Modi × Fill-Modelle, je 0 Trades |
| `results/phase_bt_d9_first_24h_short_pnl_v1/cache_bundle/` | Density pro Close |
| `short_research_engine_v1.py` + `sell_limit_fill_v1.py` | Aktueller Codepfad (BT-D18: Fill vor Cancel auf aktiver Bar) |

---

## B. Zeitliche Chronologie (Kurz)

Details: **`case_a_timeline.csv`** (15:05–16:35 UTC), **`case_b_timeline.csv`** (16:30–18:00 + Pending-Fortsetzung in `pending_bars.csv` / `density_origin_lifecycle.json`).

### Fall A — relevante Kerzen

| Close UTC | Open | High | Low | Close | Exact pool active? | Order |
|-----------|------|------|-----|-------|-------------------|--------|
| 15:30 | 0.08663 | **0.08681** | 0.08654 | 0.08681 | nein (noch kein Signal-Pool) | — |
| **15:35** | **0.08681** | **0.08682** | 0.08655 | 0.08668 | **ja** | **SIGNAL → PENDING** |
| **15:40** | 0.08668 | **0.08671** | 0.08639 | 0.08639 | **nein** | **ACTIVE → CANCEL** |

### Fall B — Aktivierung + Best Approach

| Close UTC | Open | High | Low | Close | Pool active | High vs limit 0.08593 |
|-----------|------|------|-----|-------|-------------|-------------------------|
| **17:00** | — | — | — | (Signal) | ja | Signal-Close 0.08575 |
| **17:05** | 0.08575 | **0.08584** | 0.08556 | 0.08556 | ja | −0.00009 |
| … | … | ≤0.08584 | … | … | ja | nie ≥ limit |

---

## C. Pool-Deaktivierung — Preistouch?

**Fall A — belegt aus Cache-Einträgen (keine Screenshots):**

- **15:35 close:** Upper-Band exakt `(0.08681, 0.086838)`, Origins: `…1789137900|…1789140600` (2 Origins).
- **15:40 close:** **Kein** identisches Intervall; stattdessen u. a. `(0.08681, 0.08682)` und `(0.08682, 0.086838)` mit **drei** Origin-IDs (neu: `…1789140900`).

**Schlussfolgerung:**

1. Letzter Close mit **exact** Signal-Pool: **15:35**.
2. Verschwinden des Exact-Match ist **Density-Recompute / Bandgeometrie** am 15:40-Close — **nicht** automatisch „PRICE_TOUCH“ (im JSON **nicht** annotiert) → **UNKNOWN** als Touch-Ursache im SSOT-Sinn.
3. **5m-OHLC:** Signalkerze und 15:30 berühren die Unterkante — **aber** Pending war bis 15:40 **nicht aktiv** → kein Fill-Pfad.
4. **BT-D7:** `pool_still_active` verlangt **exakte** `lower|upper`-Float-Paarität → storniert, obwohl „nahe“ Bänder und teils gleiche Origins existieren → **POOL_STATUS_SEMANTIC_MISMATCH** (Chart vs Engine-Identity).

**Fall B:** Exact-Band über gesamtes Pending **stable active** (`pool_still_active` true in allen `pending_bars.csv`-Zeilen).

---

## D. Sell-Limit — alle Varianten @ Aktivierungsbar

Aus **`fill_decision_comparison.csv`** (Aktivierungs-close):

### Fall A @ 15:40

| Modus | Limit | Open≥L | High≥L | legacy fill | causal_v2 fill |
|-------|-------|--------|--------|-------------|----------------|
| lower | 0.086810 | nein | nein | nein | nein (`HIGH_BELOW_LIMIT`) |
| upper | 0.086838 | nein | nein | nein | nein |
| offset 0,1 % | 0.08672319 | nein | nein | nein | nein |

→ **Kein** marketable Open; **kein** Bracket-Touch. Danach (BT-D18): `CANCELLED_POOL_INACTIVE`.

### Fall B @ 17:05 (und gesamtes Pending)

| Modus | Limit | @17:05 High≥L | Irgendeine Pending-Bar |
|-------|-------|---------------|-------------------------|
| lower | 0.085930 | nein (0.08584) | **nie** |
| upper | 0.085982 | nein | **nie** |
| offset 0,1 % | 0.08584407 | nein | **nie** |

---

## E. Fall A — 15:35 im Detail

1. **Signal @ 15:35 close:** Pool active; geplantes Limit **0.086810**; SL/TP wie BT-D9-Snapshot.
2. **Aktivierung 15:40 open:** Kurs **unter** Limit (Open 0.086680); Kerze fällt (High 0.086710 &lt; 0.086810).
3. **Letzte 5m-Berührung Pool-Unterkante vor Aktivierung:** 15:30–15:35 (High ≥ 0.08681) — **vor** Sell-Limit-Aktivierung → Modell: **kein Fill**.
4. **Warum Band weg:** Exact-Segment 0.086838-Oberkante → Split + zusätzlicher Origin im Cache — **Strukturänderung**, nicht belegt als reiner Preis-Touch-Event.
5. **Offset 0,1 %:** Limit **0.086723** — High 0.086710 **bleibt darunter** → korrekt kein Fill.

**Engine-Trace (15:35→15:40):** `engine_trace.json` → `case_A`.

---

## F. Fall B — 17:00 im Detail

1. **Signal @ 17:00:** Close 0.08575 unter Pool-Unterkante 0.08593 — Proximity/Rejection dennoch Signal (BT-D7-Regeln unverändert).
2. **17:05:** Bester High der Pending-Phase **0.085840** (−9×10⁻⁵ vom Limit).
3. **Pool:** durchgehend active; **kein** Cancel.
4. **Backtester:** korrekt **NO_ENTRY_TOUCH** / kein Fill unter legacy **und** causal_v2.

**Späterer TP-Bereich:** Preis erreicht später ~0.084 ohne Entry — **Outcome-Forensik**, kein Signal-Lookahead.

---

## G. Signallage @ Close (kausal)

Siehe **`signal_landscape_at_close.json`** (Gap %, SL/TP-Distanzen, R:R aus `strategy_rules` @ Signal-Snapshot-Density).

- **A:** Gap nach oben ~0,5 %+; SL über Pool-Oberkante +0,2 %; TP `next_eligible_band` → geplant 0.085860.
- **B:** Analog; TP 0.084590.

---

## H. Engine-Trace & BT-D18

- **Replay** mit `bt_d9_24h_v1.json` + Default-Fill-Modell: Orders = BT-D13 (inkl. 12:00-Cancel, 15:35-Cancel, 17:00-Pending).
- Mit `causal_sell_limit_v2` explizit: **gleiche** Order-Events für diese Signale (kein Fill-Kandidat auf Aktivierungsbars).
- BT-D18 war **vollständiger Backtest** über Cache (BT-D17/18-Vergleichsskript), nicht nur Re-Lesen alter `result.json`.

**Kritischer Pfad Fall A @ 15:40 (BT-D18):**

1. `evaluate_short_sell_limit_fill` → **nicht** filled (`high < limit`).
2. `pool_still_active(0.08681, 0.086838, snap_15:40)` → **false**.
3. → `CANCELLED_POOL_INACTIVE`.

---

## I. Konsistenzprüfung

| Prüfpunkt | Ergebnis |
|-----------|----------|
| Close-UTC-Semantik | Konsistent mit BT-D9/BT-D7 |
| Kein Lookahead im Signal | OK |
| Density as-of close | OK (Cache-Einträge) |
| Touch vs Cancel | Fall A: kein Touch **nach** Aktivierung; Cancel korrekt nach fehlendem Fill |
| marketable @ Open | Fall A/B: Open &lt; Limit auf Aktivierungsbar |
| legacy vs causal | Für diese Fälle **identisch** (kein Open≥Limit) |
| TP/SL vs Snapshot | Stimmt mit `signal_snapshot` in `latest_result.json` |
| PnL | 0 Trades → 0 Netto (kein Fee-Pfad) |

---

## J. Einordnung

| Code | Fall A | Fall B |
|------|--------|--------|
| 1 BACKTESTER_CORRECT_NO_FILL | **ja** | **ja** |
| 2 BACKTESTER_EXECUTION_BUG_CONFIRMED | nein | nein |
| 3 POOL_STATUS_SEMANTIC_MISMATCH | **ja** (exact band vs visuelle Kontinuität) | nein |
| 4 INTRABAR_UNRESOLVED | optional (5m-only) | optional |
| 5 DATA_PROVENANCE_BLOCKED | 1m fehlt | 1m fehlt |

---

## STOP

Keine Codeänderung, keine Config-Änderung, kein Dashboard, kein neuer Parameter-Sweep, keine Großläufe.
