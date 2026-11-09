# L1 V3AL — E0 vs E3 Trade- und Entry-Gate-Audit

Quelle: V3AK-Lauf (Snapshot SHA256 `b0718d82a94e8ad1…`) · **READ-ONLY** · keine Regeländerung.

## Gesamtvergleich

| Kennzahl | E0 | E3 |
|---|---:|---:|
| Ausgeführte Trades | 3 | 3 |
| Gewinner | 1 | 2 |
| Verlierer | 2 | 1 |
| TP-Exits | 1 | 2 |
| Supportbruch-Exits | 1 | 0 |
| SL-Exits | 1 | 1 |
| Gesamtbrutto % (addiert) | +3.6988 | +4.4766 |
| Durchschnitt pro Trade % | +1.2329 | +1.4922 |
| Längste Verlustserie | 2 | 1 |

## Ergebnisdifferenz — Ursache

**Nicht** durch andere TP/SL/Supportbruch-Logik bei identischem Entry: das einzige identische ausgeführte Entry-Event ist 
`te-d-c0791ea0c65e-0003` (Referenz-Long); TP **0.08929**, Exit **TP +5.1584%** — in E0 und E3 **identisch**.

Die **+0.7778 %-Punkte** Brutto-Differenz entstehen **ausschließlich** durch:

1. **E3-Entry-Gate** blockiert `te-d-1731fb4bb889-0002` und `te-d-8521dc37f7b9-0002` (depth&lt;5 auf naher Merge-Zone).
2. **Chronologie / GLOBAL_MAX_ONE_OPEN_LONG:** Ohne die beiden E0-Los-Trades bleibt der Slot nach Trade 1 länger frei; 
   E3 führt stattdessen `te-d-ea081434c5d3-0003` (**TP +0.5182%**) und `te-d-ea081434c5d3-0012` (**SL −1.2%**) aus.
3. Netto: E0 verliert **−0.2596% (SBE)** und **−1.2% (SL)** auf den entfallenen Slots; E3 gewinnt **+0.5182%** auf dem 
   kurzen TP, verliert **−1.2%** auf dem zweiten EA-Long — Summe für diese zwei Slots: E0 **−1.4596%**, E3 **−0.6818%**.

**Fazit:** E3 schneidet hier besser ab wegen **geänderter Trade-Reihenfolge/Slot-Belegung**, nicht wegen „besserer“ Exits bei denselben Entries.

## GO-TO-Liste (Chart-Prüfung)

### Neu in E3

- **te-d-ea081434c5d3-0003** · Entry 2026-08-30 11:45:00 UTC @ 0.0849100000 · Support `d-ea081434c5d3` · Exit 2026-08-30 12:05:00 UTC TP 0.5182%
- **te-d-ea081434c5d3-0012** · Entry 2026-08-30 17:40:00 UTC @ 0.0858700000 · Support `d-ea081434c5d3` · Exit 2026-08-30 20:40:00 UTC SL -1.2000%

### In E0, nicht in E3

- **te-d-1731fb4bb889-0002** · Entry 2026-08-28 20:15:00 UTC @ 0.0847500000 · Support `d-1731fb4bb889` · Exit 2026-08-29 06:15:00 UTC SUPPORT_BREAK_EXIT -0.2596%
- **te-d-8521dc37f7b9-0002** · Entry 2026-08-29 10:25:00 UTC @ 0.0845800000 · Support `d-8521dc37f7b9` · Exit 2026-08-30 22:20:00 UTC SL -1.2000%

### Gemeinsam (identischer Pfad)

- **te-d-c0791ea0c65e-0003** · Entry 2026-08-26 20:30:00 UTC · CAP6-TP 0.0892900000 · TP 5.1584%

## Alle sechs Trades (Detail)

### E0 · `te-d-c0791ea0c65e-0003`

| Feld | Wert |
|---|---|
| Entry UTC | 2026-08-26 20:30:00 UTC |
| Entry-Preis | 0.0849100000 |
| C1 UTC | 2026-08-26 20:25:00 UTC |
| Support-ID | `d-c0791ea0c65e` |
| Entry-Gate-Zone |  –  |
| TP-Zone | 0.0892900000 – 0.0896240000 (5.1584 %) |
| TP / SL | 0.0892900000 / 0.0838910800 |
| TP-Touch UTC | 2026-08-27 08:45:00 UTC |
| SBE break / confirm | 2026-08-27 05:30:00 UTC / — |
| Exit | 2026-08-27 08:45:00 UTC @ 0.0892900000 · **TP** · **5.1584%** |
| Haltedauer | 12.25 h (735 min) |
| MFE / MAE | 5.4646% / -0.2473% |
| Zeichnung | RETROSPECTIVE_DRAWING |

### E0 · `te-d-1731fb4bb889-0002`

| Feld | Wert |
|---|---|
| Entry UTC | 2026-08-28 20:15:00 UTC |
| Entry-Preis | 0.0847500000 |
| C1 UTC | 2026-08-28 20:10:00 UTC |
| Support-ID | `d-1731fb4bb889` |
| Entry-Gate-Zone |  –  |
| TP-Zone | 0.0879100000 – 0.0883140000 (3.7286 %) |
| TP / SL | 0.0879100000 / 0.0837330000 |
| TP-Touch UTC | — |
| SBE break / confirm | 2026-08-29 02:55:00 UTC / 2026-08-29 03:55:00 UTC |
| Exit | 2026-08-29 06:15:00 UTC @ 0.0845300000 · **SUPPORT_BREAK_EXIT** · **-0.2596%** |
| Haltedauer | 10.00 h (600 min) |
| MFE / MAE | 0.7434% / -0.4484% |
| Zeichnung | RETROSPECTIVE_DRAWING |

### E0 · `te-d-8521dc37f7b9-0002`

| Feld | Wert |
|---|---|
| Entry UTC | 2026-08-29 10:25:00 UTC |
| Entry-Preis | 0.0845800000 |
| C1 UTC | 2026-08-29 10:20:00 UTC |
| Support-ID | `d-8521dc37f7b9` |
| Entry-Gate-Zone |  –  |
| TP-Zone | 0.0879100000 – 0.0883140000 (3.9371 %) |
| TP / SL | 0.0879100000 / 0.0835650400 |
| TP-Touch UTC | — |
| SBE break / confirm | 2026-08-30 00:20:00 UTC / — |
| Exit | 2026-08-30 22:20:00 UTC @ 0.0835650400 · **SL** · **-1.2000%** |
| Haltedauer | 35.92 h (2155 min) |
| MFE / MAE | 2.1873% / -1.4897% |
| Zeichnung | RETROSPECTIVE_DRAWING |

### E3 · `te-d-c0791ea0c65e-0003`

| Feld | Wert |
|---|---|
| Entry UTC | 2026-08-26 20:30:00 UTC |
| Entry-Preis | 0.0849100000 |
| C1 UTC | 2026-08-26 20:25:00 UTC |
| Support-ID | `d-c0791ea0c65e` |
| Entry-Gate-Zone | 0.08604 – 0.086276 |
| TP-Zone | 0.0892900000 – 0.0896240000 (5.1584 %) |
| TP / SL | 0.0892900000 / 0.0838910800 |
| TP-Touch UTC | 2026-08-27 08:45:00 UTC |
| SBE break / confirm | 2026-08-27 05:30:00 UTC / — |
| Exit | 2026-08-27 08:45:00 UTC @ 0.0892900000 · **TP** · **5.1584%** |
| Haltedauer | 12.25 h (735 min) |
| MFE / MAE | 5.4646% / -0.2473% |
| Zeichnung | RETROSPECTIVE_DRAWING |

### E3 · `te-d-ea081434c5d3-0003`

| Feld | Wert |
|---|---|
| Entry UTC | 2026-08-30 11:45:00 UTC |
| Entry-Preis | 0.0849100000 |
| C1 UTC | 2026-08-30 11:40:00 UTC |
| Support-ID | `d-ea081434c5d3` |
| Entry-Gate-Zone | 0.08535 – 0.085814 |
| TP-Zone | 0.0853500000 – 0.0858140000 (0.5182 %) |
| TP / SL | 0.0853500000 / 0.0838910800 |
| TP-Touch UTC | 2026-08-30 12:05:00 UTC |
| SBE break / confirm | — / — |
| Exit | 2026-08-30 12:05:00 UTC @ 0.0853500000 · **TP** · **0.5182%** |
| Haltedauer | 0.33 h (20 min) |
| MFE / MAE | 0.5182% / -0.1649% |
| Zeichnung | RETROSPECTIVE_DRAWING |

### E3 · `te-d-ea081434c5d3-0012`

| Feld | Wert |
|---|---|
| Entry UTC | 2026-08-30 17:40:00 UTC |
| Entry-Preis | 0.0858700000 |
| C1 UTC | 2026-08-30 17:35:00 UTC |
| Support-ID | `d-ea081434c5d3` |
| Entry-Gate-Zone | 0.08791 – 0.088314 |
| TP-Zone | 0.0879100000 – 0.0883140000 (2.3757 %) |
| TP / SL | 0.0879100000 / 0.0848395600 |
| TP-Touch UTC | — |
| SBE break / confirm | 2026-08-30 20:25:00 UTC / 2026-08-30 20:35:00 UTC |
| Exit | 2026-08-30 20:40:00 UTC @ 0.0848395600 · **SL** · **-1.2000%** |
| Haltedauer | 3.00 h (180 min) |
| MFE / MAE | 0.6521% / -1.4091% |
| Zeichnung | RETROSPECTIVE_DRAWING |

## Identische Gate-Passes (E0 und E3)

Sechs Kandidaten bestehen **beide** Gates (V3E-B + CAP6 + E3-Substantial):
`te-d-c0791ea0c65e-0003/0005`, `te-d-ea081434c5d3-0003/0004/0012/0013`.
Nur **ein** davon wird in beiden Modellen **ausgeführt** (`…c0791…-0003`); die übrigen scheitern an 
**GLOBAL_MAX_ONE_OPEN_LONG** oder werden in E0 durch laufende E0-Trades überdeckt.

## Entry-Gate — 22 Kandidaten

Chronologie: `l1_v3al_entry_decision_timeline.csv` (Spalten `e0_status`/`e3_status`, `e0_outcome`/`e3_outcome`).

### Ereignistabelle (Auszug, chronologisch)

| UTC | Kandidat | E0 | E3 | offene Pos. (E0) | Ergebnis E0 | Ergebnis E3 |
|---|---|---|---|---|---|---|
| 2026-08-26 20:30:00 UTC | `te-d-c0791ea0c65e-0003` | allowed | allowed | — | executed | executed |
| 2026-08-26 20:45:00 UTC | `te-d-c0791ea0c65e-0005` | allowed | allowed | — | skipped_max_one_open | skipped_max_one_open |
| 2026-08-27 05:25:00 UTC | `te-d-c0791ea0c65e-0007` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-27 05:55:00 UTC | `te-d-c0791ea0c65e-0009` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-27 06:20:00 UTC | `te-d-c0791ea0c65e-0010` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-27 07:35:00 UTC | `te-d-c0791ea0c65e-0012` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-28 20:15:00 UTC | `te-d-1731fb4bb889-0002` | allowed | blocked | — | executed | blocked_e3_gate |
| 2026-08-29 10:25:00 UTC | `te-d-8521dc37f7b9-0002` | allowed | blocked | — | executed | blocked_e3_gate |
| 2026-08-29 10:45:00 UTC | `te-d-8521dc37f7b9-0003` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-29 18:40:00 UTC | `te-d-8521dc37f7b9-0008` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-29 20:05:00 UTC | `te-d-8521dc37f7b9-0009` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-30 11:45:00 UTC | `te-d-ea081434c5d3-0003` | allowed | allowed | — | skipped_max_one_open | executed |
| 2026-08-30 12:00:00 UTC | `te-d-ea081434c5d3-0004` | allowed | allowed | — | skipped_max_one_open | skipped_max_one_open |
| 2026-08-30 15:10:00 UTC | `te-d-ea081434c5d3-0005` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-30 15:20:00 UTC | `te-d-ea081434c5d3-0006` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-30 16:20:00 UTC | `te-d-ea081434c5d3-0010` | blocked | blocked | — | blocked_gate | blocked_gate |
| 2026-08-30 17:30:00 UTC | `te-d-ea081434c5d3-0011` | allowed | blocked | — | skipped_max_one_open | blocked_e3_gate |
| 2026-08-30 17:40:00 UTC | `te-d-ea081434c5d3-0012` | allowed | allowed | — | skipped_max_one_open | executed |
| 2026-08-30 18:20:00 UTC | `te-d-ea081434c5d3-0013` | allowed | allowed | — | skipped_max_one_open | skipped_max_one_open |
| 2026-08-31 00:00:00 UTC | `te-d-e6e9b83d2be1-0002` | blocked | blocked | — | blocked_gate | blocked_e3_gate |
| 2026-08-31 02:15:00 UTC | `te-d-e6e9b83d2be1-0004` | blocked | blocked | — | blocked_gate | blocked_e3_gate |
| 2026-08-31 03:10:00 UTC | `te-d-e6e9b83d2be1-0006` | blocked | blocked | — | blocked_gate | blocked_e3_gate |

**E3-spezifisch blockiert (Gate, nicht Slot):** `1731…0002`, `8521…0002/0003/0008/0009`, mehrere 
`c0791…` während offenem Referenz-Long, `ea08…0005/0006/0011`, `e6e9…` (kein naher TP2c-Pool).

**Nur Slot (E3 erlaubt, E0 auch erlaubt):** z. B. `ea08…0003` — E0 `skipped_max_one_open` (noch `8521…-0002`), 
E3 `executed` (Slot frei).

**E3 vs E0 Codepfad:** Identisch bis `apply_substantial_entry_gate` (E3 setzt `_pick_ok=false` bei fehlender TP2c-Zone). 
Keine weiteren Abweichungen festgestellt.

## Artefakte

- `L1_V3AL_E0_VS_E3_TRADE_AUDIT.md` (dieses Dokument)
- `l1_v3al_e0_e3_chart_review.csv`
- `l1_v3al_entry_decision_timeline.csv`

**STOP** — keine Produktionsübernahme.