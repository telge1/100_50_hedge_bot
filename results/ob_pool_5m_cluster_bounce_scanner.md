# 5m Cluster Bounce Analysis (DOGEUSDT)

- universe: `scanner`
- n_signals: **15**
- timeframe: **5m** ACTIVE upper pools → mass clusters
- bounce window: **36** × 5m bars (~3.0h)
- bounce min reverse: **0.25%** of entry
- OB / public-trade: **not used** (price structure only)

## Method

1. At each long signal, load causal 5m ACTIVE upper pools.
2. Group into clusters (gap ≤ 0.10%).
3. Rank by `strength_sum` (rank 1 = heaviest).
4. On first touch into each ranked cluster, measure reverse from local high.
5. Aggregate bounce / pierce rates per rank.

## By mass rank

| rank | reached | bounce rate | pierce rate | mean bounce % (when bounced) | mean strength | mean dist % |
|---|---|---|---|---|---|---|
| 1 | 9/15 (60%) | 44% | 67% | 1.79% | 9.11 | 2.37% |
| 2 | 11/15 (73%) | 64% | 45% | 2.38% | 5.20 | 2.32% |
| 3 | 10/15 (67%) | 60% | 90% | 2.62% | 3.31 | 2.25% |
| 4 | 6/14 (43%) | 50% | 50% | 1.16% | 1.95 | 3.16% |
| 5 | 6/13 (46%) | 83% | 100% | 2.40% | 1.33 | 4.24% |

## Overall

- signals analyzed: **15**
- mean clusters/signal: **8.266666666666667**
- mean ranked reached/signal: **2.8**

## Takeaways

- Heaviest cluster (rank 1): bounce rate **44%** of reached, mean reverse **1.79%**.
- Next-smaller (rank 2): bounce rate **64%** of reached, mean reverse **2.38%**.
- Next: add OB/delta only after these structure rates look stable.
