# L1 V3AH — Realistic Upper Pool TP (CAP × Dominance)

Run: `20261109T121009Z`
Drawings SHA256: `b0718d82a94e8ad12d27d570c0cd7ce97cf8088a2db21c4c92830cd80fc600cb`
Supportbruch: V3Y (Δ 0.2 %), unverändert
**Gross PnL % only; fees and slippage not included.**

## Methodik

- Kanonische PD1 5m Exact Unique-Origin D2 (`pd1_exact_d2_adapter` / V3W qualified bands)
- `merge_qualified_zones` + `enrich_zone_origins` (gap 0,15 %)
- ∫Density = Σ(depth × Segmentbreite) über qualifizierte D2-Bänder im Merge-Span
- Dominanz-Rang auf **allen** Entry-Zonen, dann erste passende Zone **aufsteigend** innerhalb CAP
- Kein Fern-D2-Fallback

**Beobachtungshorizont Trefferstudie:** From entry bar (inclusive) through pilot simulation_end (last loaded 5m bar). No extension beyond L1 pilot candle window.

## Referenztrade `te-d-c0791ea0c65e-0003`

- `CAP3` `D20`: ok=False TP=None dist=None% reason=NO_QUALIFIED_TP rank=None
- `CAP3` `D25`: ok=False TP=None dist=None% reason=NO_QUALIFIED_TP rank=None
- `CAP5` `D20`: ok=False TP=None dist=None% reason=NO_QUALIFIED_TP rank=None
- `CAP5` `D25`: ok=False TP=None dist=None% reason=NO_QUALIFIED_TP rank=None
- `CAP6` `D20`: ok=True TP=0.08929 dist=5.158403014957007% reason= rank=3
- `CAP6` `D25`: ok=True TP=0.08929 dist=5.158403014957007% reason= rank=3
- **CAP6 / D20–D25 (V3E-B):** Zone **0.08929**, Exit **TP +5.16 %** (08:45 UTC); Baseline **SUPPORT_BREAK +2.89 %**; CAP5: `NO_QUALIFIED_TP` (Dist > 5 %).

## Backtest — Variante B (Kern)

| Szenario | Trades | TP | SBE | SL | Σ% brutto | vs BL | Block | TP-Hit % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BASELINE_V3AB | 4 | 0 | 2 | 2 | 0.2258 | — | 0 | 0.0 |
| CAP3_D20_BLOCK | 3 | 2 | 1 | 0 | 2.5062 | +2.2804 | 14 | 66.7 |
| CAP3_D20_ALLOW | 4 | 0 | 2 | 2 | 0.2258 | +0.0000 | 0 | 0.0 |
| CAP3_D25_BLOCK | 3 | 2 | 1 | 0 | 2.5062 | +2.2804 | 13 | 66.7 |
| CAP3_D25_ALLOW | 4 | 0 | 2 | 2 | 0.2258 | +0.0000 | 0 | 0.0 |
| CAP5_D20_BLOCK | 3 | 1 | 1 | 1 | 1.6109 | +1.3851 | 6 | 33.3 |
| CAP5_D20_ALLOW | 4 | 0 | 2 | 2 | 0.2258 | +0.0000 | 0 | 0.0 |
| CAP5_D25_BLOCK | 3 | 1 | 1 | 1 | 1.6109 | +1.3851 | 5 | 33.3 |
| CAP5_D25_ALLOW | 4 | 0 | 2 | 2 | 0.2258 | +0.0000 | 0 | 0.0 |
| CAP6_D20_BLOCK | 3 | 1 | 1 | 1 | 3.6988 | +3.4730 | 4 | 33.3 |
| CAP6_D20_ALLOW | 4 | 1 | 1 | 2 | 2.4988 | +2.2730 | 0 | 25.0 |
| CAP6_D25_BLOCK | 3 | 1 | 1 | 1 | 3.6988 | +3.4730 | 3 | 33.3 |
| CAP6_D25_ALLOW | 4 | 1 | 1 | 2 | 2.4988 | +2.2730 | 0 | 25.0 |

## Varianten A / C

### V3E-A
- BASELINE_V3AB: trades=4 gross=0.2258
- CAP3_D20_BLOCK: trades=3 gross=2.5062
- CAP3_D20_ALLOW: trades=4 gross=0.2258
- CAP3_D25_BLOCK: trades=3 gross=2.5062
- CAP3_D25_ALLOW: trades=4 gross=0.2258
- CAP5_D20_BLOCK: trades=3 gross=1.6109
- CAP5_D20_ALLOW: trades=4 gross=0.2258
- CAP5_D25_BLOCK: trades=3 gross=1.6109
- CAP5_D25_ALLOW: trades=4 gross=0.2258
- CAP6_D20_BLOCK: trades=3 gross=3.6988
- CAP6_D20_ALLOW: trades=4 gross=2.4988
- CAP6_D25_BLOCK: trades=3 gross=3.6988
- CAP6_D25_ALLOW: trades=4 gross=2.4988
### V3E-C
- BASELINE_V3AB: trades=0 gross=0.0000
- CAP3_D20_BLOCK: trades=0 gross=0.0000
- CAP3_D20_ALLOW: trades=0 gross=0.0000
- CAP3_D25_BLOCK: trades=0 gross=0.0000
- CAP3_D25_ALLOW: trades=0 gross=0.0000
- CAP5_D20_BLOCK: trades=0 gross=0.0000
- CAP5_D20_ALLOW: trades=0 gross=0.0000
- CAP5_D25_BLOCK: trades=0 gross=0.0000
- CAP5_D25_ALLOW: trades=0 gross=0.0000
- CAP6_D20_BLOCK: trades=0 gross=0.0000
- CAP6_D20_ALLOW: trades=0 gross=0.0000
- CAP6_D25_BLOCK: trades=0 gross=0.0000
- CAP6_D25_ALLOW: trades=0 gross=0.0000

## TP-Entfernung → historische Trefferquote → Ø realisiertes PnL (Baseline B)

| Bucket % | Kandidaten | Touch % | Ausführbar TP % | Touch ohne TP-Exit | Ø Bars | BL-B Trades | Ø PnL % BL-B |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0.5_2 | 30 | 100.0 | 93.33 | 0 | 76.27 | 0 | None |
| 2_3 | 20 | 100.0 | 40.0 | 0 | 713.05 | 0 | None |
| 3_5 | 32 | 100.0 | 12.5 | 0 | 1219.12 | 0 | None |
| 5_6 | 15 | 100.0 | 13.33 | 0 | 1844.07 | 0 | None |
| 6_8 | 63 | 100.0 | 0.0 | 0 | 1995.24 | 0 | None |
| 8_12 | 88 | 100.0 | 0.0 | 0 | 2052.2 | 1 | 2.8854 |

Artefakte: `l1_v3ah_trades_all.csv`, `l1_v3ah_hit_rate_detail.csv`, `l1_v3ah_hit_rate_summary.json`

**STOP** — keine Übernahme in V3AB/V3Y/Produktion.