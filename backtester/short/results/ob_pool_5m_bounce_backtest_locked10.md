# 5m Pool Bounce Backtest (DOGEUSDT)

Implements `results/ob_pool_5m_bounce_rule.md`.

- universe: `locked10`
- n_signals: **10**
- min lower-pool gap (from touched top): **0.8%**
- short SL: pool top + **0.2%**
- short TP: next lower pool **top**
- OB/delta watch start: **0.8%** before pool bottom
- bounce window: **36** × 5m (~3.0h)
- bounce min: **0.25%**

## Overall (all ranks)

- rows: **47**
- reached: **24**
- bounce / pierce / weak: **16** / **8** / **0**
- bounce rate (reached): **67%**
- mean bounce % when bounce: **2.18%**

## Tradeable gap filter (>= 0.8%)

- tradeable rows: **47**
- bounce rate among tradeable reached: **67%**
- mean lower gap %: **4.33%**

## Short trades (gap OK + reversal candle)

- trades taken: **21**
- winrate: **43%**
- mean / sum pnl %: **0.115% / 2.41%**
- exits: `{'tp': 4, 'sl': 11, 'timeout': 6}`

## Flow-confirmed short trades only

- flow trades: **11**
- winrate: **45%**
- mean / sum pnl %: **0.466% / 5.12%**

## Flow confirmation (OB≥1.05 + Δ≥100k at touch)

- flow-confirmed reached: **13**
- bounce rate when flow-confirmed: **92%**
- gap + flow bounce rate: **92%** (n=13)

## By rank

| rank | reached | bounce rate | pierce rate | tradeable bounce rate | mean bounce % |
|---|---|---|---|---|---|
| 1 | 6/10 | 67% | 33% | 67% | 1.79% |
| 2 | 7/10 | 71% | 29% | 71% | 2.36% |
| 3 | 6/10 | 67% | 33% | 67% | 2.44% |
| 4 | 2/9 | 50% | 50% | 50% | 0.50% |
| 5 | 3/8 | 67% | 33% | 67% | 2.84% |

## By source

| source | reached | bounce rate | flow-confirmed bounce | n_flow |
|---|---|---|---|---|
| calibrated_locked | 24/47 | 67% | 92% | 13 |

## Columns

Each row has: `decision_ts`, `touch_ts`, `rank`, `pool_bottom/top`, `next_lower_pool_gap_pct`, `tradeable_gap`, `watch_ts`, `bounce_pct`, `pierce_pct`, `outcome`, `ob_ratio_at_watch/touch`, `delta_at_watch/touch`.
