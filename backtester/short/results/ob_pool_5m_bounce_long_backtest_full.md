# 5m Pool Bounce Long Backtest (DOGEUSDT)

Mirrored companion to `results/ob_pool_5m_bounce_rule.md`.

- universe: `full`
- n_signals: **71**
- min TP room (entry -> next upper pool bottom): **0.8%**
- long SL: touched pool bottom - **0.2%**
- long TP: nearest ACTIVE upper pool **bottom**
- OB/delta watch start: **0.8%** before support touch

## Overall (all ranks)

- rows: **137**
- reached: **85**
- bounce / pierce / weak: **41** / **15** / **0**
- bounce rate (reached): **48%**
- mean bounce % when bounce: **2.02%**

## Tradeable gap filter (TP room >= 0.8%)

- tradeable rows: **69**
- bounce rate among tradeable reached: **59%**
- mean upper gap %: **2.49%**

## Long trades (gap OK + reversal candle)

- trades taken: **69**
- winrate: **62%**
- mean / sum pnl %: **0.200% / 13.82%**
- exits: `{'timeout': 22, 'tp': 24, 'sl': 23}`

## Flow-confirmed long trades only

- flow trades: **21**
- winrate: **67%**
- mean / sum pnl %: **0.420% / 8.82%**

## Flow confirmation (OB<=0.95 + Δ<=-100k at touch)

- flow-confirmed reached: **26**
- bounce rate when flow-confirmed: **46%**
- gap + flow bounce rate: **57%** (n=21)

## By rank

| rank | reached | bounce rate | pierce rate | tradeable bounce rate | mean bounce % |
|---|---|---|---|---|---|
| 1 | 36/70 | 56% | 11% | 67% | 2.23% |
| 2 | 49/67 | 43% | 22% | 54% | 1.82% |

## By source

| source | reached | bounce rate | flow-confirmed bounce | n_flow |
|---|---|---|---|---|
| fakeout | 64/88 | 48% | 47% | 17 |
| scanner_breakout | 13/29 | 54% | 33% | 6 |
| strong_breakout | 8/20 | 38% | 67% | 3 |

## Columns

Each row has: `decision_ts`, `touch_ts`, `rank`, `pool_bottom/top`, `next_upper_pool_gap_pct`, `tradeable_gap`, `watch_ts`, `bounce_pct`, `pierce_pct`, `outcome`, `ob_ratio_at_watch/touch`, `delta_at_watch/touch`.
