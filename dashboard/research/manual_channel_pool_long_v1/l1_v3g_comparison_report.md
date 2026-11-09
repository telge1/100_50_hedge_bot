# L1 V3G — Backtest 25.08.–20.09.2026

- **Daten vollständig (Kerzen):** ja (7488/7488, Lücken=0)
- **Pool-SSOT vollständig (Proben):** ja (Fehler=0)
- **Backtest-Anspruch:** full_26d
- drawings SHA256: `e062a5a0b6802363cdf9048cba26b460344cef711ed93133c909ade466252ce8` | V3F-vergleichbar: **False**
- D2: `pd1_exact_density_trp_adapter.computeExactBandsForSide-parity-v1` | Filter: Nähe≤2.5% Q1≥1

_Linien mit `extend_right` bleiben über Anker hinaus aktiv (`ON_EXTENDED_RAY`) — retrospektiv._

## Gesamtergebnis

| Kennzahl | A | B | C |
| --- | ---: | ---: | ---: |
| Touch-Events | 187 | = | = |
| Entry-Kandidaten | 35 | = | = |
| Zugelassene Kandidaten | 29 | 27 | 24 |
| Ausgeführte Trades | 3 | 2 | 2 |
| Gewinner | 2 | 2 | 2 |
| Stop-Loss | 1 | 0 | 0 |
| Kanal-TP | 2 | 2 | 2 |
| Pool-TP | 0 | 0 | 0 |
| Offen am Ende | 0 | 0 | 0 |
| Summe Gewinne % | 8.9204 | 8.9204 | 8.9204 |
| Summe Verluste % | -1.2000 | 0.0000 | 0.0000 |
| **Gesamtbrutto %** | **7.7204** | **8.9204** | **8.9204** |
| Ablehnungen D2-Nähe | 0 | 2 | 2 |
| Ablehnungen Staffelung | 0 | 0 | 3 |

## Zeiträume (Exit-UTC-Zuordnung)

### Variante A
| Segment | exec | Gewinne | SL | Σ Gew % | Σ Verl % | Brutto % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 25.08.–01.09. | 3 | 2 | 1 | 8.9204 | -1.2000 | 7.7204 |
| 01.09.–10.09. | 0 | 0 | 0 | 0.0000 | 0.0000 | 0.0000 |
| 10.09.–20.09. | 0 | 0 | 0 | 0.0000 | 0.0000 | 0.0000 |
- Z1 vs V3F-Ref brutto 7.7204: **7.7204** (match=True)

### Variante B
| Segment | exec | Gewinne | SL | Σ Gew % | Σ Verl % | Brutto % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 25.08.–01.09. | 2 | 2 | 0 | 8.9204 | 0.0000 | 8.9204 |
| 01.09.–10.09. | 0 | 0 | 0 | 0.0000 | 0.0000 | 0.0000 |
| 10.09.–20.09. | 0 | 0 | 0 | 0.0000 | 0.0000 | 0.0000 |
- Z1 vs V3F-Ref brutto 8.9204: **8.9204** (match=True)

### Variante C
| Segment | exec | Gewinne | SL | Σ Gew % | Σ Verl % | Brutto % |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 25.08.–01.09. | 2 | 2 | 0 | 8.9204 | 0.0000 | 8.9204 |
| 01.09.–10.09. | 0 | 0 | 0 | 0.0000 | 0.0000 | 0.0000 |
| 10.09.–20.09. | 0 | 0 | 0 | 0.0000 | 0.0000 | 0.0000 |
- Z1 vs V3F-Ref brutto —: **8.9204** (match=False)

## Filtervergleich

### A ausgeführt, B blockiert (gleiches touch_event_id)
- `2026-08-25 21:15:00 UTC` d-03350098db64: A=SL -1.2000% → B NO_TRADE (REJECTED_FIRST_D2_TOO_FAR) near=3.610949% Q=0/6/12/11

### B ausgeführt, C blockiert
_Keine unterschiedliche Ausführung B vs C._

### B ausgeführt, A nicht (gleiches Event / Slot)
### Variante A

#### Gewinn-Trades
| Entry UTC | Entry | SL | TP@Entry | Exit UTC | Exit | Typ | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 2026-08-28 01:30:00 UTC | 0.0899665421 | CHANNEL_UPPER | d-a6d9a05da2f1 | 6.5449 |
| 2026-08-29 07:20:00 UTC | 0.0844000000 | 0.0833872000 | 0.0882760000 | 2026-08-30 16:55:00 UTC | 0.0864048985 | CHANNEL_UPPER | d-248d74050cc8 | 2.3755 |

#### Stop-Loss-Trades
| Entry UTC | Entry | SL | TP@Entry | SL UTC | Exit | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-25 21:15:00 UTC | 0.0858500000 | 0.0848198000 | 0.0925240000 | 2026-08-26 14:40:00 UTC | 0.0848198000 | d-03350098db64 | -1.2000 |

#### Offene Trades am Fensterende
_Keine._

### Variante B

#### Gewinn-Trades
| Entry UTC | Entry | SL | TP@Entry | Exit UTC | Exit | Typ | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 2026-08-28 01:30:00 UTC | 0.0899665421 | CHANNEL_UPPER | d-a6d9a05da2f1 | 6.5449 |
| 2026-08-29 07:20:00 UTC | 0.0844000000 | 0.0833872000 | 0.0882760000 | 2026-08-30 16:55:00 UTC | 0.0864048985 | CHANNEL_UPPER | d-248d74050cc8 | 2.3755 |

#### Stop-Loss-Trades
_Keine._

#### Offene Trades am Fensterende
_Keine._

### Variante C

#### Gewinn-Trades
| Entry UTC | Entry | SL | TP@Entry | Exit UTC | Exit | Typ | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 2026-08-28 01:30:00 UTC | 0.0899665421 | CHANNEL_UPPER | d-a6d9a05da2f1 | 6.5449 |
| 2026-08-29 07:20:00 UTC | 0.0844000000 | 0.0833872000 | 0.0882760000 | 2026-08-30 16:55:00 UTC | 0.0864048985 | CHANNEL_UPPER | d-248d74050cc8 | 2.3755 |

#### Stop-Loss-Trades
_Keine._

#### Offene Trades am Fensterende
_Keine._

