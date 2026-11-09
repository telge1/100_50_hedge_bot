# 5m Cluster Bounce Analysis (DOGEUSDT)

- universe: `locked10`
- n_signals: **10**
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
| 1 | 6/10 (60%) | 67% | 50% | 1.79% | 9.87 | 2.09% |
| 2 | 7/10 (70%) | 71% | 29% | 2.36% | 5.54 | 2.35% |
| 3 | 6/10 (60%) | 67% | 83% | 2.44% | 3.40 | 2.23% |
| 4 | 2/9 (22%) | 50% | 50% | 0.50% | 1.92 | 3.99% |
| 5 | 3/8 (38%) | 67% | 100% | 2.84% | 1.43 | 4.50% |

## Overall

- signals analyzed: **10**
- mean clusters/signal: **8.3**
- mean ranked reached/signal: **2.4**

## Takeaways

- Heaviest cluster (rank 1): bounce rate **67%** of reached, mean reverse **1.79%**.
- Next-smaller (rank 2): bounce rate **71%** of reached, mean reverse **2.36%**.
- Next: add OB/delta only after these structure rates look stable.
