# L1 V3AK — Substantial Upper-Pool Entry Gate

Run: `20261109T125737Z` · Snapshot `b0718d82a94e8ad1…`
Variante **V3E-B** · TP fix **CAP6_D20_BLOCK** (unverändert vs. V3AH)

## Summary

| Model | Kandidaten | Trades | Block Gate | TP | SBE | SL | Σ% | Ø/Trade |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| E0_BASELINE | 22 | 3 | 0 | 1 | 1 | 1 | 3.6988 | 1.2329 |
| E1_SUBSTANTIAL_TP2a | 22 | 3 | 5 | 1 | 1 | 1 | 3.6988 | 1.2329 |
| E2_SUBSTANTIAL_TP2b | 22 | 3 | 11 | 1 | 1 | 1 | 3.6988 | 1.2329 |
| E3_SUBSTANTIAL_TP2c | 22 | 3 | 15 | 2 | 0 | 1 | 4.4766 | 1.4922 |

## Referenztrade

- **E0_BASELINE**: pass=True zone=`n/a` 
- **E1_SUBSTANTIAL_TP2a**: pass=True zone=`0.08536-0.08546` 
- **E2_SUBSTANTIAL_TP2b**: pass=True zone=`0.08604-0.086276` 
- **E3_SUBSTANTIAL_TP2c**: pass=True zone=`0.08604-0.086276` 

Alle E-Modelle **führen den Referenz-Long aus** (substantielle Zone innerhalb 2,5 % vorhanden). CAP6-TP bleibt **0.08929** (Zone bei 5,16 % — unabhängig vom Entry-Gate).

## Entry-Gate vs. chronologische Trades

| Modell | Zusätzlich blockiert (Gate) | vs E0 neue Slots | vs E0 entfallen |
|--------|----------------------------:|------------------|-----------------|
| E0 | 0 | — | — |
| E1 | 5 | 0 | 0 |
| E2 | 11 | 0 | 0 |
| E3 | 15 | `te-d-ea081434c5d3-0003`, `…0012` | `te-d-1731fb4bb889-0002`, `te-d-8521dc37f7b9-0002` |

**Interpretation:** E1/E2 blockieren Pending-Events, ändern aber bei **V3E-B + 1-Long** dieselben **3** Ausführungen wie E0 (blockierte Kandidaten waren ohnehin nicht chronologisch dran). **E3** verschiebt die Slot-Reihenfolge: **Σ% 4.48** (2×TP, 1×SL) vs. E0 **3.70** (1×TP, 1×SBE, 1×SL) — Effekt gemischt aus Filter **und** neuen Entries, nicht rein „TP-Qualität“.

## Artefakte

- `l1_v3ak_entry_gate_decisions.csv`
- `l1_v3ak_trades_all.csv`
- `l1_v3ak_chart_review.csv`
- `l1_v3ak_summary.csv`

**STOP** — keine Übernahme in V3AB/V3AH/V3Y.