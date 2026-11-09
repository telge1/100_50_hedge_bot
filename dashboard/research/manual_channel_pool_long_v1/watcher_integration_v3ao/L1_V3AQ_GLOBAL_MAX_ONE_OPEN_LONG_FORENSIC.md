# L1 V3AQ — Forensik `GLOBAL_MAX_ONE_OPEN_LONG` (30.08.2026)

**READ-ONLY** · Keine Regeländerung · Equality aus V3AP unverändert gültig.

## Aufgabe 1 — Blockierender Trade (A1/A2 @ 09:35)

| Feld | Wert |
|---|---|
| Blockierter Kandidat | `d-ea081434c5d3` Entry **2026-08-30 09:35:00 UTC** @ 0.0845200000 |
| Trade-ID (Blocker) | `te-d-8521dc37f7b9-0007` |
| Trendlinie (Blocker) | `d-8521dc37f7b9` |
| Blocker Entry | 2026-08-29 17:40:00 UTC @ 0.0848500000 |
| SL / TP | 0.0838318000 / 0.0879100000 |
| Exit | 2026-08-30 21:00:00 UTC · **SL** |
| Rendite Blocker | -1.2000% |
| Noch offen um 09:35? | **yes** |
| `position_free_from_idx` vor Entscheid | 1206 (Entry idx 1068) |

## Aufgabe 2 — Chronologie 08:00–12:00 UTC

Vollständig: `l1_v3aq_timeline_0830_1200.csv` (pro Simulation).

### V3AK
- 2026-08-30 11:45:00 UTC `te-d-ea081434c5d3-0003` line=`d-ea081434c5d3` → **EXECUTED:TP** 
- 2026-08-30 12:00:00 UTC `te-d-ea081434c5d3-0004` line=`d-ea081434c5d3` → **GLOBAL_MAX_ONE_OPEN_LONG** reject=GLOBAL_MAX_ONE_OPEN_LONG

### WATCHER_A1_W12_003
- 2026-08-30 09:35:00 UTC `te-d-ea081434c5d3-0001` line=`d-ea081434c5d3` → **GLOBAL_MAX_ONE_OPEN_LONG** reject=GLOBAL_MAX_ONE_OPEN_LONG
- 2026-08-30 09:55:00 UTC `te-d-ea081434c5d3-0002` line=`d-ea081434c5d3` → **GLOBAL_MAX_ONE_OPEN_LONG** reject=GLOBAL_MAX_ONE_OPEN_LONG
- 2026-08-30 10:10:00 UTC `te-d-ea081434c5d3-0003` line=`d-ea081434c5d3` → **GLOBAL_MAX_ONE_OPEN_LONG** reject=GLOBAL_MAX_ONE_OPEN_LONG

### WATCHER_A2_W12_003
- 2026-08-30 09:35:00 UTC `te-d-ea081434c5d3-0001` line=`d-ea081434c5d3` → **GLOBAL_MAX_ONE_OPEN_LONG** reject=GLOBAL_MAX_ONE_OPEN_LONG
- 2026-08-30 09:55:00 UTC `te-d-ea081434c5d3-0002` line=`d-ea081434c5d3` → **GLOBAL_MAX_ONE_OPEN_LONG** reject=GLOBAL_MAX_ONE_OPEN_LONG
- 2026-08-30 10:10:00 UTC `te-d-ea081434c5d3-0003` line=`d-ea081434c5d3` → **GLOBAL_MAX_ONE_OPEN_LONG** reject=GLOBAL_MAX_ONE_OPEN_LONG

### WATCHER_A3_W12_003
- 2026-08-30 10:10:00 UTC `te-d-ea081434c5d3-0001` line=`d-ea081434c5d3` → **EXECUTED:TP** 

## Aufgabe 3 — Warum A3 @ 10:10, A1/A2 @ 09:35 blockiert

Jede Variante ist eine **eigene** chronologische Simulation (eigene Pending-Liste, eigenes `position_free_from_idx`).

| Simulation | Trades gesamt | `te-d-8521dc37f7b9-0007` (29.08 17:40) | `position_free_from_idx` vor EA08 10:10 |
|---|---:|---|---:|
| **A1/A2** | 2 | **ausgeführt** (SL 30.08 21:00) | **1206** (Entry idx 1075 blockiert) |
| **A3** | 3 | **nicht ausgeführt** (0× 8521 in Executed) | **195** (Slot frei → **EXECUTED:TP** 10:10) |

- A1/A2 führen den **8521-Long vom 29.08** chronologisch aus; dieser läuft bis **21:00 UTC** → blockiert alle EA08-Früheinstiege am 30.08 (09:35, 09:55, 10:10).
- A3 hat **andere** Entry-Signale; 8521 wird in dieser Simulation **nicht** gehandelt (nur `te-d-c0791ea0c65e-0001`, Exit 27.08 08:45) → Slot frei.
- A3 @ 10:10 ist **keine** Lockerung derselben Sperre, sondern **eine andere Trade-Historie** in der Variante.

## Aufgabe 4 — Hypothetischer A1/A2-Exit (ohne Slot)

- **HYPOTHETICAL_NO_SLOT** Entry 2026-08-30 09:35:00 UTC → Exit **TP** 2026-08-30 12:05:00 UTC · **0.7099%** — _NOT included in backtest sum — diagnostic only_
- **HYPOTHETICAL_NO_SLOT** Entry 2026-08-30 09:35:00 UTC → Exit **TP** 2026-08-30 12:05:00 UTC · **0.7099%** — _NOT included in backtest sum — diagnostic only_

## Gegenüberstellung (30.08. · `d-ea081434c5d3`)

| Variante | Entry | Slot | Ausführung | Exit / % |
|---|---|---|---|---|
| V3AK | 11:45 @ 0,08491 | frei (nach TP 12:05 auf 11:45-Trade) | ja | TP +0,5182 % |
| A1/A2 | 09:35 @ 0,08452 | **belegt** durch 8521 | **nein** | — |
| A3 | 10:10 @ 0,08463 | frei (kein 8521 in A3-Sim) | ja | TP +0,5790 % |

## Aufgabe 5 — Bewertung

- Die Regel **maximal ein offener Long** arbeitet deterministisch über sortierte `_entry_idx` und `position_free_from_idx = exit_idx + 1` nach `simulate_exit_v3y`.
- Block bei 09:35 ist **regelkonform** — nicht D2/CAP6/E3, sondern **Trade-Management**.
- Blocker **`te-d-8521dc37f7b9-0007`** war um 09:35 **tatsächlich noch offen** (`still_open_at_blocked_entry=yes`, Exit erst 21:00).
- Positionsbegrenzung funktioniert **korrekt und deterministisch** (siehe `l1_v3aq_timeline_0830_1200.csv`).

**STOP**