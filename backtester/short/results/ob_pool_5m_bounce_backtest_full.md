# 5m Pool Bounce Backtest (DOGEUSDT)

Implements `results/ob_pool_5m_bounce_rule.md`.

- universe: `full`
- n_signals: **71**
- min TP room (pool bottom → next lower top): **0.8%**
- short SL: pool top + **0.2%**
- short TP: next lower pool **top**
- OB/delta watch start: **0.8%** before pool bottom
- bounce window: **36** × 5m (~3.0h)
- bounce min: **0.25%**

## Overall (all ranks)

- rows: **138**
- reached: **84**
- bounce / pierce / weak: **61** / **23** / **0**
- bounce rate (reached): **73%**
- mean bounce % when bounce: **2.32%**

## Tradeable gap filter (TP room >= 0.8%)

- tradeable rows: **78**
- bounce rate among tradeable reached: **71%**
- mean lower gap %: **2.17%**

## Short trades (gap OK + reversal candle)

- trades taken: **78**
- winrate: **56%**
- mean / sum pnl %: **0.284% / 22.12%**
- exits: `{'tp': 29, 'sl': 32, 'timeout': 17}`

## Flow-confirmed short trades only

- flow trades: **38**
- winrate: **76%**
- mean / sum pnl %: **0.931% / 35.38%**

## Flow confirmation (OB≥1.05 + Δ≥100k at touch)

- flow-confirmed reached: **42**
- bounce rate when flow-confirmed: **90%**
- gap + flow bounce rate: **89%** (n=38)

## By rank

| rank | reached | bounce rate | pierce rate | tradeable bounce rate | mean bounce % |
|---|---|---|---|---|---|
| 1 | 34/70 | 62% | 38% | 62% | 1.86% |
| 2 | 50/68 | 80% | 20% | 77% | 2.57% |

## By source

| source | reached | bounce rate | flow-confirmed bounce | n_flow |
|---|---|---|---|---|
| fakeout | 53/91 | 77% | 89% | 27 |
| scanner_breakout | 20/30 | 55% | 89% | 9 |
| strong_breakout | 11/17 | 82% | 100% | 6 |

## Columns

Each row has: `decision_ts`, `touch_ts`, `rank`, `pool_bottom/top`, `next_lower_pool_gap_pct`, `tradeable_gap`, `watch_ts`, `bounce_pct`, `pierce_pct`, `outcome`, `ob_ratio_at_watch/touch`, `delta_at_watch/touch`.
