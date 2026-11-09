# L1 V3AI — Vollständiger V3AH-Audit (READ-ONLY)

Quelle: `realistic_pool_tp_v3ah/` — **keine** Änderungen an V3AH-Logik oder bestehenden CSVs.

## 1. Trade-Übersicht (Modelle mit V3E-B)

| Nr | Modell | Entry UTC | TP | Exit | PnL % |
|---:|---|---|---|---|---:|
| 5 | BASELINE_V3AB | 2026-08-26 20:30:00 UTC | 0.0949600000 | SUPPORT_BREAK_EXIT | 2.8854 |
| 6 | BASELINE_V3AB | 2026-08-28 20:15:00 UTC | 0.0949600000 | SUPPORT_BREAK_EXIT | -0.2596 |
| 7 | BASELINE_V3AB | 2026-08-29 10:25:00 UTC | 0.0949600000 | SL | -1.2000 |
| 8 | BASELINE_V3AB | 2026-08-31 02:15:00 UTC | 0.0945500000 | SL | -1.2000 |
| 12 | CAP3_D20_BLOCK | 2026-08-27 05:55:00 UTC | 0.0892900000 | TP | 2.6676 |
| 13 | CAP3_D20_BLOCK | 2026-08-30 11:45:00 UTC | 0.0853500000 | TP | 0.5182 |
| 14 | CAP3_D20_BLOCK | 2026-08-30 15:10:00 UTC | 0.0879100000 | SUPPORT_BREAK_EXIT | -0.6796 |
| 19 | CAP3_D20_ALLOW | 2026-08-26 20:30:00 UTC | — | SUPPORT_BREAK_EXIT | 2.8854 |
| 20 | CAP3_D20_ALLOW | 2026-08-28 20:15:00 UTC | — | SUPPORT_BREAK_EXIT | -0.2596 |
| 21 | CAP3_D20_ALLOW | 2026-08-29 10:25:00 UTC | — | SL | -1.2000 |
| 22 | CAP3_D20_ALLOW | 2026-08-31 02:15:00 UTC | — | SL | -1.2000 |
| 26 | CAP5_D20_BLOCK | 2026-08-27 05:25:00 UTC | 0.0892900000 | TP | 3.0705 |
| 27 | CAP5_D20_BLOCK | 2026-08-28 20:15:00 UTC | 0.0879100000 | SUPPORT_BREAK_EXIT | -0.2596 |
| 28 | CAP5_D20_BLOCK | 2026-08-29 10:25:00 UTC | 0.0879100000 | SL | -1.2000 |
| 33 | CAP5_D20_ALLOW | 2026-08-26 20:30:00 UTC | — | SUPPORT_BREAK_EXIT | 2.8854 |
| 34 | CAP5_D20_ALLOW | 2026-08-28 20:15:00 UTC | 0.0879100000 | SUPPORT_BREAK_EXIT | -0.2596 |
| 35 | CAP5_D20_ALLOW | 2026-08-29 10:25:00 UTC | 0.0879100000 | SL | -1.2000 |
| 36 | CAP5_D20_ALLOW | 2026-08-31 02:15:00 UTC | — | SL | -1.2000 |
| 40 | CAP6_D20_BLOCK | 2026-08-26 20:30:00 UTC | 0.0892900000 | TP | 5.1584 |
| 41 | CAP6_D20_BLOCK | 2026-08-28 20:15:00 UTC | 0.0879100000 | SUPPORT_BREAK_EXIT | -0.2596 |
| 42 | CAP6_D20_BLOCK | 2026-08-29 10:25:00 UTC | 0.0879100000 | SL | -1.2000 |
| 47 | CAP6_D20_ALLOW | 2026-08-26 20:30:00 UTC | 0.0892900000 | TP | 5.1584 |
| 48 | CAP6_D20_ALLOW | 2026-08-28 20:15:00 UTC | 0.0879100000 | SUPPORT_BREAK_EXIT | -0.2596 |
| 49 | CAP6_D20_ALLOW | 2026-08-29 10:25:00 UTC | 0.0879100000 | SL | -1.2000 |
| 50 | CAP6_D20_ALLOW | 2026-08-31 02:15:00 UTC | — | SL | -1.2000 |
| 54 | CAP6_D25_BLOCK | 2026-08-26 20:30:00 UTC | 0.0892900000 | TP | 5.1584 |
| 55 | CAP6_D25_BLOCK | 2026-08-28 20:15:00 UTC | 0.0879100000 | SUPPORT_BREAK_EXIT | -0.2596 |
| 56 | CAP6_D25_BLOCK | 2026-08-29 10:25:00 UTC | 0.0879100000 | SL | -1.2000 |
| 61 | CAP6_D25_ALLOW | 2026-08-26 20:30:00 UTC | 0.0892900000 | TP | 5.1584 |
| 62 | CAP6_D25_ALLOW | 2026-08-28 20:15:00 UTC | 0.0879100000 | SUPPORT_BREAK_EXIT | -0.2596 |
| 63 | CAP6_D25_ALLOW | 2026-08-29 10:25:00 UTC | 0.0879100000 | SL | -1.2000 |
| 64 | CAP6_D25_ALLOW | 2026-08-31 02:15:00 UTC | — | SL | -1.2000 |

## 2. Variante C

Ausgeführte Trades V3E-C: **0** (erwartet 0).

## 3. Referenztrade `te-d-c0791ea0c65e-0003` (V3E-B)

| Modell | Gewähltes TP | Zone | Exit UTC | Grund | PnL % |
|--------|--------------|------|----------|-------|------:|
| BASELINE_V3AB | 0.09496 (max D2) | — | 2026-08-27 13:50 | SUPPORT_BREAK_EXIT | 2.8854 |
| CAP3_D20_BLOCK | — (Entry blockiert) | — | — | NO_TRADE | — |
| CAP3_D20_ALLOW | kein Pool-TP | — | 2026-08-27 13:50 | SUPPORT_BREAK_EXIT | 2.8854 |
| CAP5_D20_BLOCK | — (blockiert) | — | — | NO_TRADE | — |
| CAP5_D20_ALLOW | kein Pool-TP | — | 2026-08-27 13:50 | SUPPORT_BREAK_EXIT | 2.8854 |
| CAP6_D20_BLOCK | **0.08929** | 0.08929–0.089624 | **2026-08-27 08:45** | **TP** | **5.1584** |
| CAP6_D20_ALLOW | **0.08929** | 0.08929–0.089624 | **2026-08-27 08:45** | **TP** | **5.1584** |
| CAP6_D25_* | identisch CAP6 | ∫D rank 3, 6 Origins, max depth 5 | TP 08:45 | TP | 5.1584 |

**Pool @ Entry:** 15 Merge-Zonen; Violett-Rang **3/15** (D20 cut≤3, D25 cut≤4). CAP5 scheitert an `dist>5%`; CAP6 wählt erste dominante Zone in Preisreihenfolge.

**TP-Touch:** 2026-08-27 **08:45 UTC** — Kerze im Chart-Review (`l1_v3ai_chart_review.csv`, trade_no 40/47).

**Supportbruch (Baseline):** Bruch + Bestätigung führt Exit **Open** 2026-08-27 13:50 @ 0.08736 (Details in `l1_v3ai_chart_review.csv`: `break_utc`, `confirm_utc`, `exit_fill_open_utc`).

### Referenz — Legacy-Zeilen (Regel-Resolver-Check)

- **BASELINE_V3AB**: Regel-TP `max D2` · IST `0.0949600000` · SUPPORT_BREAK_EXIT · 2.8854%
- **CAP3_D20_BLOCK**: Regel-TP `NO_QUALIFIED_TP` · IST `` · not_traded · %
- **CAP3_D20_ALLOW**: Regel-TP `NO_QUALIFIED_TP` · IST `` · SUPPORT_BREAK_EXIT · 2.8854%
- **CAP5_D20_BLOCK**: Regel-TP `NO_QUALIFIED_TP` · IST `` · not_traded · %
- **CAP5_D20_ALLOW**: Regel-TP `NO_QUALIFIED_TP` · IST `` · SUPPORT_BREAK_EXIT · 2.8854%
- **CAP6_D20_BLOCK**: Regel-TP `0.08929` · IST `0.0892900000` · TP · 5.1584%
- **CAP6_D20_ALLOW**: Regel-TP `0.08929` · IST `0.0892900000` · TP · 5.1584%
- **CAP6_D25_BLOCK**: Regel-TP `0.08929` · IST `0.0892900000` · TP · 5.1584%
- **CAP6_D25_ALLOW**: Regel-TP `0.08929` · IST `0.0892900000` · TP · 5.1584%

## 4. 100%-Touch — Ursache & korrigierte Metriken

### Definition in `hit_rate.py` (V3AH)

1. **Touch** = erste 5m-Kerze mit `high >= zone.bottom` ab `_entry_idx`.
2. Scan bis **Pilot-Ende** (`simulation_end`, 2026-10-19), **nicht** bis Trade-Exit.
3. Jede **Merge-Zone** pro Entry = eigener Kandidat → Mehrfachzählung über Entries.
4. **Keine** ACTIVE-Pool-Prüfung am Touch-Zeitpunkt.
5. `hit_rate_pct` = Anteil Kandidaten mit jemals erreichtem High — daher oft **100 %** bei langem Fenster.

### Drei getrennte Kennzahlen (Neuberechnung V3AI)

| Horizon | Metrik | N | True | Rate % |
|---|---|---:|---:|---:|
| 24 | price_high_touch | 262 | 47 | 17.94 |
| 24 | executable_tp_simulated | 262 | 42 | 16.03 |
| 48 | price_high_touch | 262 | 49 | 18.7 |
| 48 | executable_tp_simulated | 262 | 42 | 16.03 |
| 72 | price_high_touch | 262 | 49 | 18.7 |
| 72 | executable_tp_simulated | 262 | 42 | 16.03 |
| pilot_end | price_high_touch | 262 | 262 | 100.0 |
| pilot_end | executable_tp_simulated | 262 | 42 | 16.03 |
| pilot_end | touch_before_adverse_not_tp | 262 | 0 | 0.0 |

## 5. BLOCK vs ALLOW

BLOCKiert (NO_QUALIFIED_TP, long erlaubt): **27** Pending-Rows.
- `te-d-1731fb4bb889-0002` @ 2026-08-28 20:15:00 UTC (CAP3_D20_BLOCK)
- `te-d-8521dc37f7b9-0002` @ 2026-08-29 10:25:00 UTC (CAP3_D20_BLOCK)
- `te-d-8521dc37f7b9-0003` @ 2026-08-29 10:45:00 UTC (CAP3_D20_BLOCK)
- `te-d-8521dc37f7b9-0008` @ 2026-08-29 18:40:00 UTC (CAP3_D20_BLOCK)
- `te-d-8521dc37f7b9-0009` @ 2026-08-29 20:05:00 UTC (CAP3_D20_BLOCK)
- `te-d-c0791ea0c65e-0003` @ 2026-08-26 20:30:00 UTC (CAP3_D20_BLOCK)
- `te-d-c0791ea0c65e-0005` @ 2026-08-26 20:45:00 UTC (CAP3_D20_BLOCK)
- `te-d-c0791ea0c65e-0007` @ 2026-08-27 05:25:00 UTC (CAP3_D20_BLOCK)
- `te-d-c0791ea0c65e-0010` @ 2026-08-27 06:20:00 UTC (CAP3_D20_BLOCK)
- `te-d-e6e9b83d2be1-0002` @ 2026-08-31 00:00:00 UTC (CAP3_D20_BLOCK)
- `te-d-e6e9b83d2be1-0004` @ 2026-08-31 02:15:00 UTC (CAP3_D20_BLOCK)
- `te-d-e6e9b83d2be1-0006` @ 2026-08-31 03:10:00 UTC (CAP3_D20_BLOCK)
- `te-d-ea081434c5d3-0006` @ 2026-08-30 15:20:00 UTC (CAP3_D20_BLOCK)
- `te-d-ea081434c5d3-0010` @ 2026-08-30 16:20:00 UTC (CAP3_D20_BLOCK)
- `te-d-c0791ea0c65e-0003` @ 2026-08-26 20:30:00 UTC (CAP5_D20_BLOCK)
- … +12 weitere

## 6. Diese Trades sollten wir zuerst visuell kontrollieren

1. **2026-08-26 20:30 UTC** — Referenz-Entry @ 0.08491 (`d-c0791ea0c65e`); Overlay D2 Depth 2 + Support.
2. **2026-08-27 08:45 UTC** — CAP6 **TP** @ 0.08929 (High-Touch); vergleichen mit V3AB-Fern-TP 0.09496.
3. **2026-08-27 13:50 UTC** — Baseline **SUPPORT_BREAK_EXIT** (Open-Fill); prüfen Bruch-/Confirm-Kerzen (`break_utc`/`confirm_utc` in Chart-CSV).
4. **2026-08-27 05:55 UTC** — CAP3 BLOCK **neuer Slot** TP 0.08929 (`te-c0791ea0-0007` ähnlich); früherer Entry wegen freier Position.
5. **2026-08-30 11:45–12:05 UTC** — Kurzer CAP3-TP (+0.52 %) nach 4 Bars Haltezeit.

## 7. BLOCK vs ALLOW (Zusammenfassung)

| Effekt | Beobachtung |
|--------|-------------|
| **BLOCK** | 27 Pending-Events mit `NO_QUALIFIED_TP` trotz `long_trigger_allowed=yes` (Details `l1_v3ai_block_vs_allow.csv`). Referenz **blockiert** bei CAP3/CAP5. |
| **ALLOW ohne TP** | Gleicher Exit wie Baseline **ohne** Fern-TP (nur SL+SBE), z. B. Referenz CAP3_ALLOW: SBE +2.89 %. |
| **ALLOW mit TP** | CAP6: Referenz identisch zu BLOCK (TP 5.16 %). |
| **Neue Slots** | CAP3_BLOCK: `te-d-c0791ea0c65e-0007` @ 05:55 → TP; nicht in Baseline-B-Set. |
| **Reiner TP-Effekt** | CAP6 vs Baseline **gleicher Entry-Count (4)** → PnL-Delta **+2.27 %** nur durch TP-Wechsel (5.16 vs SBE 2.89 auf Referenz). CAP3_BLOCK: **−1 Entry**, **+2.28 %** Σ durch frühere Slots — Mischung Slot + TP. |

**Variante A (D2)** vs **B (V3E-B):** Bei diesem Snapshot **identische** ausgeführte Trades je Szenario (D2-Gate nur vs. V3E-B Nearness).

## 8. Variante A (D2) — Tradezähler

Gleiche 4 Baseline-Trades; CAP-Szenarien spiegeln B (siehe `l1_v3ai_trade_list.csv`, `variant=D2`).

## Artefakte

- `l1_v3ai_chart_review.csv`
- `l1_v3ai_tp_selection_audit.csv`
- `l1_v3ai_hit_rate_validation.csv`
- `l1_v3ai_trade_list.csv`
- `l1_v3ai_block_vs_allow.csv`

**STOP**