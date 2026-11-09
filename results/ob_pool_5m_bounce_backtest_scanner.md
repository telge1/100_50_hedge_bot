# 5m Pool Bounce Backtest (DOGEUSDT)

Implements `results/ob_pool_5m_bounce_rule.md`.

- universe: `scanner`
- n_signals: **15**
- min lower-pool gap (from touched top): **0.5%**
- OB/delta watch start: **0.8%** before pool bottom
- bounce window: **36** × 5m (~3.0h)
- bounce min: **0.25%**

## Overall (all ranks)

- rows: **72**
- reached: **42**
- bounce / pierce / weak: **25** / **17** / **0**
- bounce rate (reached): **60%**
- mean bounce % when bounce: **2.20%**

## Tradeable gap filter (>= 0.5%)

- tradeable rows: **71**
- bounce rate among tradeable reached: **59%**
- mean lower gap %: **4.06%**

## Flow confirmation (OB≥1.05 + Δ≥100k at touch)

- flow-confirmed reached: **19**
- bounce rate when flow-confirmed: **89%**
- gap + flow bounce rate: **89%** (n=18)

## By rank

| rank | reached | bounce rate | pierce rate | tradeable bounce rate | mean bounce % |
|---|---|---|---|---|---|
| 1 | 9/15 | 44% | 56% | 44% | 1.79% |
| 2 | 11/15 | 64% | 36% | 60% | 2.38% |
| 3 | 10/15 | 60% | 40% | 60% | 2.62% |
| 4 | 6/14 | 50% | 50% | 50% | 1.16% |
| 5 | 6/13 | 83% | 17% | 83% | 2.40% |

## Columns

Each row has: `decision_ts`, `touch_ts`, `rank`, `pool_bottom/top`, `next_lower_pool_gap_pct`, `tradeable_gap`, `watch_ts`, `bounce_pct`, `pierce_pct`, `outcome`, `ob_ratio_at_watch/touch`, `delta_at_watch/touch`.
