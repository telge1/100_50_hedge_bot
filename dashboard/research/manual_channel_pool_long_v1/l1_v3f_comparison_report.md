# L1 V3F — Backtest 25.08.–01.09.2026 (V3E-Filter)

## Reproduzierbarkeit

- Git: `b83b1d5d7e75459b469a5be7d247646149ef4754`
- Fenster: **2026-08-25 00:00:00 UTC .. 2026-09-01 00:00:00 UTC** (exkl.)
- Kerzen geladen: **2016** (erwartet 2016)
- Datenlücken: **0**
- drawings SHA256: `3e7041de2814607b4bd4ad4a07df143c155072b09473b96ff151fe1e2e0a1cc4`
- Gleicher Stand wie V3E (Präfix `3e7041de2814607b`): **ja**
- Allowlist fehlt: d-1ff495203465
- Filter unverändert: D2-Gate, Nähe≤2.5%, Q1≥1

## Vergleichstabelle (gesamtes Fenster)

| Kennzahl | A | B | C |
| --- | ---: | ---: | ---: |
| Touch-Ereignisse | 74 | = | = |
| Freigegebene Kandidaten (ohne 1-Long-Cap) | 29 | 27 | 24 |
| Ausgeführte Trades | 3 | 2 | 2 |
| Gewinner (realisiert) | 2 | 2 | 2 |
| Stop-Loss | 1 | 0 | 0 |
| Kanal-TP | 2 | 2 | 2 |
| Pool-TP | 0 | 0 | 0 |
| Am Fensterende offen | 0 | 0 | 0 |
| Σ realisierte Gewinne % | 8.9204 | 8.9204 | 8.9204 |
| Σ realisierte Verluste % | -1.2000 | 0.0000 | 0.0000 |
| **Gesamtbrutto realisiert %** | **7.7204** | **8.9204** | **8.9204** |
| MTM offen % (nicht in Brutto) | 0.0000 | 0.0000 | 0.0000 |
| REJECTED_FIRST_D2_TOO_FAR | 0 | 2 | 2 |
| REJECTED_NO_EARLY_D2_STAGGERING | 0 | 0 | 3 |

## Neues Segment 29.08.–01.09.

| | Kandidaten (Entry) | Executed A | Executed B | Executed C |
| --- | ---: | ---: | ---: | ---: |
| nur 29.08+ | 7 | 1 | 1 | 1 |

- Kandidaten mit Entry im Segment: **7**
- GLOBAL_MAX_ONE_OPEN_LONG im Segment (A): **4**

## Unterschied zu V3E (25.08.–29.08. exkl.)

V3E-Referenz: A exec=2 brutto=5.3449% | B/C exec=1 brutto=6.5449%.

- Variante A im alten Teil: Kandidaten 28, executed 2 (SL 1, Gewinn 1)
- Variante B im alten Teil: Kandidaten 28, executed 1 (SL 0, Gewinn 1)
- Variante C im alten Teil: Kandidaten 28, executed 1 (SL 0, Gewinn 1)

## Auswertung Segment 29.08.–01.09. (§10)

1. Weitere Long-Kandidaten: **7** mit Entry-Zeit im Segment.
2. Davon ausgeführt: A=1, B=1, C=1.
3. A/B/C-Unterschied im Segment: siehe executed-Spalten; B/C identisch bis auf Stagger-Rejects: False.
4./5. Staffelung: nur relevant wenn zusätzliche Kandidaten im Segment — numerisch oben.
6. 1-Long-Regel: 4 Ablehnungen `GLOBAL_MAX_ONE_OPEN_LONG` im Segment (Variante A).
7. Schwellen: keine Parameteränderung; Gegenbeispiele im erweiterten Fenster dokumentieren Limitation.

### Variante A

#### Gewinn-Trades (realisiert)
| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | d-a6d9a05da2f1 | 6.5449 |
| 2026-08-29 07:20:00 UTC | 0.0844000000 | 0.0833872000 | 0.0882760000 | 0.0864048985 | 2026-08-30 16:55:00 UTC | CHANNEL_UPPER | d-248d74050cc8 | 2.3755 |

#### Stop-Loss-Trades

| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-25 21:15:00 UTC | 0.0858500000 | 0.0848198000 | 0.0925240000 | 0.0848198000 | 2026-08-26 14:40:00 UTC | d-03350098db64 | -1.2000 |

#### Am Fensterende offen (nicht in Σ realisiert)

_Keine._

### Variante B

#### Gewinn-Trades (realisiert)
| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | d-a6d9a05da2f1 | 6.5449 |
| 2026-08-29 07:20:00 UTC | 0.0844000000 | 0.0833872000 | 0.0882760000 | 0.0864048985 | 2026-08-30 16:55:00 UTC | CHANNEL_UPPER | d-248d74050cc8 | 2.3755 |

#### Stop-Loss-Trades

_Keine._

#### Am Fensterende offen (nicht in Σ realisiert)

_Keine._

### Variante C

#### Gewinn-Trades (realisiert)
| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | d-a6d9a05da2f1 | 6.5449 |
| 2026-08-29 07:20:00 UTC | 0.0844000000 | 0.0833872000 | 0.0882760000 | 0.0864048985 | 2026-08-30 16:55:00 UTC | CHANNEL_UPPER | d-248d74050cc8 | 2.3755 |

#### Stop-Loss-Trades

_Keine._

#### Am Fensterende offen (nicht in Σ realisiert)

_Keine._

