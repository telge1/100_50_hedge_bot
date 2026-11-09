# L1 V3AR — Trade-Konkurrenz & Slot-Belegung

**Run:** `20261109T171938Z` · **READ-ONLY** · Positionslimit unverändert.
Snapshot `b0718d82a94e8ad1…` · Fenster s. `l1_v3ap_data_scope.json`.

## 1. Varianten-Statistik

| Variante | Executed | Slot-blocked (gate-OK) | Blocked % | Ø Pos.min | Ø Blocker.min | Σ realized % | Blocker loss/win |
|---|---:|---:|---:|---:|---:|---:|---:|
| V3AK | 3 | 3 | 50.0 | 311.7 | 311.7 | 4.4766 | 1/2 |
| A1_W12_003 | 2 | 10 | 83.33 | 1292.5 | 1570.5 | 4.7822 | 9/1 |
| A2_W12_003 | 2 | 10 | 83.33 | 1292.5 | 1570.5 | 4.7822 | 9/1 |
| A3_W12_003 | 3 | 1 | 25.0 | 408.3 | 925.0 | 4.7857 | 0/1 |

**Hypothetisch (blockiert):** siehe `l1_v3ar_hypothetical_blocked.csv` — Spalte `hypothetical_note=DIAGNOSTIC_ONLY_NOT_IN_BACKTEST_SUM`.

| Variante | Hypo-Zeilen | Unsafe-Σ hypo % (überlappend) |
|---|---:|---:|
| V3AK | 3 | 4.3905 |
| A1_W12_003 | 10 | -0.007 |
| A2_W12_003 | 10 | -0.007 |
| A3_W12_003 | 1 | 5.1584 |

_Hinweis:_ Mehrere blockierte Kandidaten können **dieselbe** offene Position blockieren; hypothetische Summen sind **nicht** gleichzeitig realisierbar.

## 2. Trade-Konkurrenz (V3AQ-Kernfall 30.08.)

A1/A2: Blocker **`te-d-8521dc37f7b9-0007`** (Entry 29.08 17:40, SL 30.08 21:00, −1,20 %) sperrt EA08-Früheinstiege; A3 führt diesen 8521-Trade in **eigener** Simulation **nicht** aus → anderer Slot.

## 3. Nach Linie / Periode

- `l1_v3ar_blocks_by_blocked_line.csv`
- `l1_v3ar_blocks_by_blocker_line.csv`
- `l1_v3ar_blocks_by_month.csv`

## 4. Beurteilung

Die Watcher-Logik erzeugt **gate-konforme** Signale, deren Ausführung durch **chronologische 1-Long-Regel** begrenzt wird — nicht durch fehlende D2/CAP6/E3.
Frühere Bestätigung (A1/A2) erhöht **Kandidatendichte** und damit **Slot-Kollisionen** vs. V3AK; A3 verändert zusätzlich **welche** früheren Trades überhaupt ausgeführt werden (eigenes Pending).
Keine Strategieempfehlung aus hypothetischer Einzelrendite (z. B. +0,71 % @ 09:35).

## Artefakte

- `l1_v3ar_executed_trades.csv`
- `l1_v3ar_slot_conflicts.csv`
- `l1_v3ar_hypothetical_blocked.csv`
- `l1_v3ar_variant_statistics.csv`

**STOP**