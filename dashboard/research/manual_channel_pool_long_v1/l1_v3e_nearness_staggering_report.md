# L1 V3E — D2-Nähe + frühe Staffelung

## Reproduzierbarkeit

- Git: `b83b1d5d7e75459b469a5be7d247646149ef4754`
- drawings SHA256: `3e7041de2814607b4bd4ad4a07df143c155072b09473b96ff151fe1e2e0a1cc4` (19 Zeichnungen)
- Allowlist vorhanden: 
- Allowlist fehlt: d-03350098db64, d-0e5e7004dccd, d-1ff495203465, d-248d74050cc8, d-a6d9a05da2f1
- D2: `pd1_exact_density_trp_adapter.computeExactBandsForSide-parity-v1` depth≥2
- MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT = **2.50**
- MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER = **1**
- Fenster: 2026-08-25 .. 2026-08-29 exkl. (identisch für A/B/C)

## Referenz-Parität (V3D)

- 2026-08-25 21:15:00 UTC: MATCH — near 3.6109% (ref 3.6109%), Q1 bands 0 (ref 0)
- 2026-08-26 15:10:00 UTC: MATCH — near 1.8948% (ref 1.8948%), Q1 bands 6 (ref 6)

## Vergleichstabelle

| Kennzahl | A (Baseline D2) | B (+Nähe) | C (+Staffelung) |
| --- | ---: | ---: | ---: |
| Touch-Ereignisse | 45 | same | same |
| Long-Trigger erlaubt | 42 | same | same |
| Zugelassene Kandidaten (ohne 1-Long-Limit) | 24 | 22 | 19 |
| Ausgeführt | 2 | 1 | 1 |
| Gewinner | 1 | 1 | 1 |
| Stop-Loss | 1 | 0 | 0 |
| Kanal-TP | 1 | 1 | 1 |
| Pool-TP | 0 | 0 | 0 |
| Offen am Fensterende | 0 | 0 | 0 |
| Σ Gewinne % | 6.5449 | 6.5449 | 6.5449 |
| Σ Verluste % | -1.2000 | 0.0000 | 0.0000 |
| Σ Brutto % | 5.3449 | 6.5449 | 6.5449 |
| REJECTED_FIRST_D2_TOO_FAR | 0 | 2 | 2 |
| REJECTED_NO_EARLY_D2_STAGGERING | 0 | 0 | 3 |

### Referenzfälle B/C

- A loss @ 2026-08-25 21:15:00 UTC, Variante B: exit=NO_TRADE reject=REJECTED_FIRST_D2_TOO_FAR
- A loss @ 2026-08-25 21:15:00 UTC, Variante C: exit=NO_TRADE reject=REJECTED_FIRST_D2_TOO_FAR
- B win @ 2026-08-26 15:10:00 UTC, Variante B: exit=CHANNEL_UPPER reject=—
- B win @ 2026-08-26 15:10:00 UTC, Variante C: exit=CHANNEL_UPPER reject=—

### Variante A

| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | 6.5449 |

#### Stop-Loss — Variante A

| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | drawing_id | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-25 21:15:00 UTC | 0.0858500000 | 0.0848198000 | 0.0925240000 | 0.0848198000 | 2026-08-26 14:40:00 UTC | d-03350098db64 | -1.2000 |

### Variante B

| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | 6.5449 |

#### Stop-Loss — Variante B

_Keine SL-Trades._

### Variante C

| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | 6.5449 |

#### Stop-Loss — Variante C

_Keine SL-Trades._

## Auswertung (§10)

1. **Verhindert Nähe den 25.08.-Verlust?** Ja (B: REJECTED_FIRST_D2_TOO_FAR).
2. **Bleibt 26.08.-Gewinner?** Ja.
3. **Staffelung vs. nur Nähe:** Unterschied vorhanden (executed B=1 C=1).
4. **Weitere Blockierungen:** B blockiert 2 Kandidatenzeilen; C zusätzlich 3 wegen Staffelung.
5. **Neue Trades durch frühere Ablehnung?** Vergleich executed A=2 B=1 C=1 — chronologische Simulation.
6. **A vs B vs C identisch?** A≠B wenn executed oder rejects abweichen; B≠C: True.
7. **Gegen die Hypothese:** August-Fenster ist nicht out-of-sample; wenige Trades; fehlende Allowlist-Linie reduziert Kandidaten; hohe Kanal-TPs hängen weiter an Geometrie, nicht nur D2-Landschaft.

## Verhinderte / neue Trades (A → B)

- `te-d-03350098db64-0001` 2026-08-25 21:15:00 UTC: A=SL → B=NO_TRADE (REJECTED_FIRST_D2_TOO_FAR)
