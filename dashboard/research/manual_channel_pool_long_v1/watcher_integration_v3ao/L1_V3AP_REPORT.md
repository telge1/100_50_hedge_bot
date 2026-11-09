# L1 V3AP — Parametrisierter Entry-Vergleich und Trade-Forensik

**Equality-Gate:** `run_v3ao_equality_test.py` → **PASS** (vor Matrix-Lauf ausgeführt).  
**Unverändert:** D2 ACTIVE SSOT, CAP6, V3AH TP, E3, SL, SBE, `execute_variant_v3y`, V3AK-Originalcode.

**Run-ID:** `20261109T170918Z` · Snapshot SHA `b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb`

---

## Datenbasis und Lookahead

Siehe `l1_v3ap_data_scope.json`:

| Feld | Wert |
|---|---|
| Symbol / TF | DOGEUSDT · 5m |
| Pilot-Fenster | 2026-08-26 16:35 UTC → 2026-08-31 06:50 UTC |
| Simulationsende | 2026-10-19 13:05 UTC |
| Kerzen | 15 510 |
| Drawings | 5 (eingefrorener Snapshot) |

**Einschränkung:** Retrospective Drawings — Linienanker nicht kausal am Touch bewiesen. Pools **nur** zum Confirm-Bar-Close (`pools_at_confirm_open`). **Keine** Netto-PnL-Spalte im Executor (nur `gross_pnl_pct`; Gebühren/Slippage nicht modelliert).

---

## Parametermatrix

- **144** Watcher-Konfigurationen + **1** V3AK-Baseline = **145** Zeilen  
- Raster: Touch 0,00 / 0,01 / 0,02 / 0,03 / 0,05 / 0,20 % · A1/A2 · A3 ×4 Mindestanstieg · B ×2 Reclaim-Bars · W6/12/24  

**Artefakte:**

| Datei | Inhalt |
|---|---|
| `l1_v3ap_parameter_matrix.csv` | Vollständige Funnel-Matrix |
| `l1_v3ap_v3ak_comparison.csv` | Auszug V3AK vs. repräsentative Watcher |
| `l1_v3ap_block_statistics.csv` | Blockierungsgründe je Stufe |
| `l1_v3ap_all_executed_trades.csv` | Alle ausgeführten Trades (UTC, Entry, SL, TP, Exit, %) |
| `l1_v3ap_by_line_summary.csv` | Aggregation nach `touch_drawing_id` |
| `l1_v3ap_by_period_summary.csv` | Trades nach Monat (`entry_utc`) |
| `l1_v3ap_aug30_forensic.csv` | Einzelanalyse 30.08.2026 |

---

## V3AK-Baseline (Funnel)

| Stufe | Anzahl |
|---:|---:|
| Touch-Episoden (`build_touch_events`) | 53 |
| Entry-Signale (C1 mit `_entry_idx`) | 22 |
| Pending-Kandidaten | 22 |
| D2 abgelehnt | 0 |
| CAP6 abgelehnt | 4 |
| E3 abgelehnt | 15 |
| Executor ohne Trade | 50 |
| **Trades eröffnet** | **3** |
| TP / SL / SBE | 2 / 1 / 0 |
| Σ gross % | **4,4766** |

---

## Historischer Vergleich (Auszug)

**A1/A2, W12 — Stabilität der Touch-Toleranz** (gleiche Trades und Σ% über 0,00–0,05 %):

| tol % | Trades | Σ gross % |
|---:|---:|---:|
| 0,01–0,05 | 2 | 4,7822 |
| 0,03 (Soll-Startwert) | 2 | 4,7822 |

→ **Benachbarte Toleranzen 0,01–0,05 % mit A1/A2/W12 liefern identische Ausführung** — gut für Robustheit, unabhängig vom Peak-Gewinn.

**A3, tol 0,03 %, W12** (Mindestanstieg):

| min gain % | Trades | Σ gross % |
|---:|---:|---:|
| 0,05 / 0,10 | 2 | 4,7822 |
| 0,15 | 2 | 4,2067 |
| 0,20 | 3 | 4,7857 |

→ Sprung bei 0,15 % und 0,20 % — **0,10 %** wirkt stabiler Nachbar zu 0,05 %; **0,15 %** Ausreißer (weniger Slots).

**Szenario B:** 36 Läufe, Trades 1–2, oft Σ% **−1,2** — im Gesamtfenster schlechter als A-Pfade (Break/Reclaim selten produktiv).

**Nicht empfohlen (allein nach Gewinn):** `LEGACY 0,20 %` und isolierte B-Konfigurationen ohne Nachbarstabilität.

---

## 30.08.2026 — Long `d-ea081434c5d3`

| Variante | Touch | Confirm | Entry | D2 | CAP6 | E3 | Ausführung | Exit | gross % |
|---|---|---|---|---|---|---|---|---|---|
| **V3AK `te-…-0003`** | 11:35 | 11:40 | **11:45** @ 0,08491 | OK | OK / TP 0,08535 | pass | **ja** | **TP** 12:05 | **+0,5182** |
| **Watcher A1 W12** | 09:25 | 09:30 | **09:35** @ 0,08452 | OK | OK / TP 0,08512 | pass | **nein** | — | — |
| **Watcher A2 W12** | 09:25 | 09:30 | **09:35** @ 0,08452 | OK | OK | pass | **nein** | — | — |
| **Watcher A3 W12** | 09:25 | 10:05 | **10:10** @ 0,08463 | OK | OK / TP 0,08512 | pass | **ja** | **TP** 12:05 | **+0,5790** |

**Befund zum frühen Watcher-Entry (A1/A2):**

- Touch → Confirm → Entry-Kandidat wird korrekt erzeugt; **D2, CAP6 und E3 bestehen**.
- **Kein Trade** in der globalen `execute_variant_v3y`-Timeline: früherer Kandidat verliert gegen **GLOBAL_MAX_ONE_OPEN_LONG** / chronologische Slot-Belegung durch andere Linien/Entries (vgl. Executor-Statistik in der Matrix).
- **A3** verzögert den Entry (10:10) und erhält einen **ausgeführten TP** — nicht identisch mit V3AK-11:45, aber **tatsächlich gehandelt**.

Detail: `l1_v3ap_aug30_forensic.csv`

---

## Empfehlung weiterer Tests

1. **Primär:** `touch_tolerance_pct ∈ {0,02, 0,03, 0,05}` + **A2** oder **A1** + **W12** — stabiler Kern laut Nachbarschaft.
2. **Sekundär:** **A3** mit `confirmation_min_gain_pct` **0,05–0,10** (nicht 0,15 % isoliert).
3. **Forschung B:** nur mit eigenem KPI-Set; nicht mit A mischen.
4. **Pflicht vor jedem Lauf:** `run_v3ao_equality_test.py`.

**Runner:** `run_v3ap_param_matrix.py`, Forensik `run_v3ap_aug30_forensic.py`.

---

**STOP** — isolierte Forschung, kein Produktions-Merge.
