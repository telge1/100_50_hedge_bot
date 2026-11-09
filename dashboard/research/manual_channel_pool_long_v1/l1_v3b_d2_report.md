# L1 V3B — Kanonisches D2-Gate

## Reproduzierbarkeit

- Git: `b83b1d5d7e75459b469a5be7d247646149ef4754`
- drawings SHA256: `3e7041de2814607b4bd4ad4a07df143c155072b09473b96ff151fe1e2e0a1cc4` (19 Zeichnungen)
- D2 compute: `pd1_exact_density_trp_adapter.computeExactBandsForSide-parity-v1` min_depth=2
- Preisfenster: entry×0.92 … entry×1.15 (L1 V3A Parität)
- SSOT: `load_historical_pool_state_at_close` 5m PD1
- Fenster: 2026-08-25 .. 2026-08-29 exkl.

## A. Technischer D2-Abgleich (Audit-Referenzen)

- 2026-08-25 21:15:00 UTC: MATCH — bands 55 (ref 55), first D2 above entry 3.6109% (ref 3.6109%)
- 2026-08-26 15:10:00 UTC: MATCH — bands 69 (ref 69), first D2 above entry 1.8948% (ref 1.8948%)

## B. Kandidaten

CSV: `l1_v3b_d2_candidates.csv`

| Entry UTC | channel room % | micro qual | D2 bands | D2 qual | nearest D2 % | D2 gate |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 2026-08-25 21:15:00 UTC | 8.2132 | 62 | 55 | 55 | 3.6109 | yes |
| 2026-08-26 15:10:00 UTC | 8.8285 | 74 | 69 | 69 | 1.8948 | yes |

Micro-Proxy-Zähler (62/74) sind **einzelne Micro-Bänder**; D2-qual (55/69) sind **kanonische Depth≥2-Bänder**.

## D. Gesamtvergleich

| Variante | Executed | SL | Ch-TP | Σ Brutto % |
| --- | ---: | ---: | ---: | ---: |
| Micro-Proxy (V3A) | 2 | 1 | 1 | 5.3449 |
| **D2 kanonisch** | 2 | 1 | 1 | 5.3449 |
| D2 + Research (nicht kalibriert) | 0 | 0 | 0 | 0.0000 |

### Entscheidungsänderungen Micro → D2

_Keine ausgeführten Trade-Unterschiede._

### Micro-Proxy

| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | 6.5449 |

#### Stop-Loss — Micro-Proxy

| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | PnL % |
| --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-25 21:15:00 UTC | 0.0858500000 | 0.0848198000 | 0.0924500000 | 0.0848198000 | 2026-08-26 14:40:00 UTC | -1.2000 |

### Kanonisches D2-Gate

| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | PnL % |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-26 15:10:00 UTC | 0.0844400000 | 0.0834267200 | 0.0915100000 | 0.0899665421 | 2026-08-28 01:30:00 UTC | CHANNEL_UPPER | 6.5449 |

#### Stop-Loss — Kanonisches D2-Gate

| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | PnL % |
| --- | --- | --- | --- | --- | --- | ---: |
| 2026-08-25 21:15:00 UTC | 0.0858500000 | 0.0848198000 | 0.0925240000 | 0.0848198000 | 2026-08-26 14:40:00 UTC | -1.2000 |

## Abschlussfragen

1. **Exakt dieselben D2-Bänder wie Charts?** Siehe Parität oben — bei MATCH: ja (gleiche SSOT + Sweep-Port).
2. **Bandgrenzen/Origin-Tiefe identisch?** `depth` = distinct logical origins im Sweep; Stücktest Parität.
3. **Kandidatenänderungen:** 0 dokumentierte Micro↔D2-Abweichungen.
4. **25.08. Verlust abgelehnt (D2)?** micro exit=SL reject=—
   — D2-Variante: exit=SL | d2_gate=yes | qualified_d2=55
5. **26.08. Gewinner zugelassen?** D2 exit=CHANNEL_UPPER pnl=6.5449
