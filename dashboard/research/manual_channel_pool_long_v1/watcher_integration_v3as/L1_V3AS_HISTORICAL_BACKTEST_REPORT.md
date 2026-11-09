# L1 V3AS — Historischer Vergleich (Research-Charts, retrospektiv)

**Run:** `20261109T174223Z` · Equality-Gate **PASS**
**SSOT:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard/research_charts/user_data/drawings.json`
**SHA256:** `afff18cf062e72d8ff1ad855ee3b46b6633526bdc423c96ffb3a1791ee02923b`
**Modus:** `RETROSPECTIVE_EXPLORATORY_NOT_CAUSALLY_VALIDATED`

## 1. Drawing-Inventar

| Kennzahl | Wert |
|---|---|
| Zeichnungen in JSON | 9 |
| Parsbare Linien (5m-Geometrie) | 5 |
| Trendlinien | 5 |
| V-Lines (nicht Touch-SSOT) | 4 |
| Long-tauglich (Stichprobe) | 5 |

**Lookahead:** Manuelle Linien · nicht kausal validiert. Keine Multi-TF-Neuinterpretation.
Detail: `l1_v3as_drawings_inventory.csv`

### Anker (Trends)

- `d-1731fb4bb889`: 2026-08-28 18:50:00 UTC @ 0.08414394918832001 → 2026-08-29 06:35:00 UTC @ 0.08534280492307726 · eligible=yes
- `d-8521dc37f7b9`: 2026-08-29 08:50:00 UTC @ 0.08419823699517694 → 2026-08-30 00:40:00 UTC @ 0.0852975650840298 · eligible=yes
- `d-c0791ea0c65e`: 2026-08-26 16:35:00 UTC @ 0.08381461440974818 → 2026-08-27 16:55:00 UTC @ 0.0888682287726552 · eligible=yes
- `d-e6e9b83d2be1`: 2026-08-30 23:25:00 UTC @ 0.08117252426335579 → 2026-08-31 06:45:00 UTC @ 0.08215314773323426 · eligible=yes
- `d-ea081434c5d3`: 2026-08-30 09:15:00 UTC @ 0.08430877233576727 → 2026-08-30 22:05:00 UTC @ 0.0858983768758056 · eligible=yes

**Daten:** 2026-08-26 16:35:00 UTC → Sim-Ende 2026-10-19 13:05:00 UTC · **15510** Kerzen 5m

## 2. Varianten-Vergleich

| Variante | Touch-Epi | Kandidaten | D2↓ | CAP6↓ | E3↓ | Slot↓ | Trades | TP/SL/SBE | Hit% | Σ gross% | Ø Pos.min |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|
| V3AK | 59 | 25 | 0 | 5 | 18 | 3 | 3 | 2/1/0 | 66.67 | 4.4023 | 318.3 |
| Watcher_A2 | 38 | 36 | 0 | 6 | 23 | 11 | 2 | 1/1/0 | 50.0 | 4.7822 | 1292.5 |
| Watcher_A3 | 24 | 14 | 0 | 5 | 9 | 2 | 3 | 2/1/0 | 66.67 | 4.7857 | 408.3 |
| Watcher_B | 26 | 11 | 0 | 2 | 9 | 0 | 1 | 0/1/0 | 0.0 | -1.2 | 1640.0 |

**Kein Gewinner-Ranking** — explorativer Retrospektiv-Vergleich.

## 3. Artefakte (Chart-Review)

- `l1_v3as_all_executed_trades.csv` — Trendlinien-ID, Entry/SL/TP-%, Exit, Rendite
- `l1_v3as_by_month.csv` / `l1_v3as_by_line.csv`
- `l1_v3as_variant_summary.csv`
- `l1_v3as_data_scope.json`

Downstream unverändert (D2, CAP6, E3, SL, SBE, `execute_variant_v3y`, 1-Long).

**STOP**