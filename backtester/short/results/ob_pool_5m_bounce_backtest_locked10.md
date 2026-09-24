# 5m Pool Bounce Backtest (DOGEUSDT)

Implements `results/ob_pool_5m_bounce_rule.md`.

- universe: `locked10`
- n_signals: **10**
- min TP room (pool bottom → next lower top): **0.8%**
- short SL: pool top + **0.2%**
- short TP: next lower pool **top**
- OB/delta watch start: **0.8%** before pool bottom
- bounce window: **36** × 5m (~3.0h)
- bounce min: **0.25%**

## Overall (all ranks)

- rows: **20**
- reached: **13**
- bounce / pierce / weak: **9** / **4** / **0**
- bounce rate (reached): **69%**
- mean bounce % when bounce: **2.11%**

## Tradeable gap filter (TP room >= 0.8%)

- tradeable rows: **13**
- bounce rate among tradeable reached: **69%**
- mean lower gap %: **2.10%**

## Short trades (gap OK + reversal candle)

- trades taken: **13**
- winrate: **62%**
- mean / sum pnl %: **0.287% / 3.73%**
- exits: `{'tp': 5, 'sl': 5, 'timeout': 3}`

## Flow-confirmed short trades only

- flow trades: **6**
- winrate: **83%**
- mean / sum pnl %: **0.878% / 5.27%**

## Flow confirmation (OB≥1.05 + Δ≥100k at touch)

- flow-confirmed reached: **6**
- bounce rate when flow-confirmed: **100%**
- gap + flow bounce rate: **100%** (n=6)

## By rank

| rank | reached | bounce rate | pierce rate | tradeable bounce rate | mean bounce % |
|---|---|---|---|---|---|
| 1 | 6/10 | 67% | 33% | 67% | 1.79% |
| 2 | 7/10 | 71% | 29% | 71% | 2.36% |

## By source

| source | reached | bounce rate | flow-confirmed bounce | n_flow |
|---|---|---|---|---|
| calibrated_locked | 13/20 | 69% | 100% | 6 |

## Columns

Each row has: `decision_ts`, `touch_ts`, `rank`, `pool_bottom/top`, `next_lower_pool_gap_pct`, `tradeable_gap`, `watch_ts`, `bounce_pct`, `pierce_pct`, `outcome`, `ob_ratio_at_watch/touch`, `delta_at_watch/touch`.
