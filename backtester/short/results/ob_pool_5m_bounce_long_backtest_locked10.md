# 5m Pool Bounce Long Backtest (DOGEUSDT)

Mirrored companion to `results/ob_pool_5m_bounce_rule.md`.

- universe: `locked10`
- n_signals: **10**
- min TP room (entry -> next upper pool bottom): **0.8%**
- long SL: touched pool bottom - **0.2%**
- long TP: nearest ACTIVE upper pool **bottom**
- OB/delta watch start: **0.8%** before support touch

## Overall (all ranks)

- rows: **19**
- reached: **12**
- bounce / pierce / weak: **7** / **0** / **0**
- bounce rate (reached): **58%**
- mean bounce % when bounce: **2.12%**

## Tradeable gap filter (TP room >= 0.8%)

- tradeable rows: **9**
- bounce rate among tradeable reached: **78%**
- mean upper gap %: **2.79%**

## Long trades (gap OK + reversal candle)

- trades taken: **9**
- winrate: **89%**
- mean / sum pnl %: **0.671% / 6.04%**
- exits: `{'timeout': 3, 'tp': 5, 'sl': 1}`

## Flow-confirmed long trades only

- flow trades: **4**
- winrate: **75%**
- mean / sum pnl %: **0.569% / 2.28%**

## Flow confirmation (OB<=0.95 + Δ<=-100k at touch)

- flow-confirmed reached: **6**
- bounce rate when flow-confirmed: **33%**
- gap + flow bounce rate: **50%** (n=4)

## By rank

| rank | reached | bounce rate | pierce rate | tradeable bounce rate | mean bounce % |
|---|---|---|---|---|---|
| 1 | 5/10 | 60% | 0% | 100% | 2.65% |
| 2 | 7/9 | 57% | 0% | 67% | 1.73% |

## By source

| source | reached | bounce rate | flow-confirmed bounce | n_flow |
|---|---|---|---|---|
| calibrated_locked | 12/19 | 58% | 33% | 6 |

## Columns

Each row has: `decision_ts`, `touch_ts`, `rank`, `pool_bottom/top`, `next_upper_pool_gap_pct`, `tradeable_gap`, `watch_ts`, `bounce_pct`, `pierce_pct`, `outcome`, `ob_ratio_at_watch/touch`, `delta_at_watch/touch`.
