# EMA59-Touch Pool/Cluster Calibration

Trigger = **EMA59 touch** (same as DOGE OB calibration).
Measure = max excursion **above/below EMA59**, then pool clusters on that path.

Symbol: `DOGEUSDT`
Window: `2026-09-05T17:00:00+00:00` → `2026-09-19T11:00:00+00:00`
Touches analyzed: **171** (first-in-cluster only=True)

## By side

```json
{
  "long": {
    "n": 89,
    "mean_exc_vs_ema59_pct": 1.8997372340527736,
    "median_exc_vs_ema59_pct": 1.621827149497608,
    "p75_exc_vs_ema59_pct": 2.3707422705470034,
    "mean_confirm_delta": 18756.839234018986,
    "strongest_hit_rate": 0.7865168539325843,
    "mean_reversal_strength_sum": 8.274968557838875,
    "pct_reclaimed_ema59": 0.5842696629213483
  },
  "short": {
    "n": 82,
    "mean_exc_vs_ema59_pct": 1.6687192529348653,
    "median_exc_vs_ema59_pct": 1.7321762938017282,
    "p75_exc_vs_ema59_pct": 2.219116329531621,
    "mean_confirm_delta": -162965.6328998384,
    "strongest_hit_rate": 0.6463414634146342,
    "mean_reversal_strength_sum": 6.9350321291608985,
    "pct_reclaimed_ema59": 0.5975609756097561
  }
}
```

## Dist × |Δ| reachability

```json
[
  {
    "dist_band": "0.0-0.8%",
    "delta_bucket": "abs_delta_lt_200k",
    "n_clusters": 112,
    "hit_rate": 0.9285714285714286,
    "mean_strength_sum": 7.192193736968401
  },
  {
    "dist_band": "0.0-0.8%",
    "delta_bucket": "abs_delta_ge_200k",
    "n_clusters": 56,
    "hit_rate": 0.8928571428571429,
    "mean_strength_sum": 5.4118832068982385
  },
  {
    "dist_band": "0.8-1.5%",
    "delta_bucket": "abs_delta_lt_200k",
    "n_clusters": 56,
    "hit_rate": 0.6428571428571429,
    "mean_strength_sum": 2.878260234103863
  },
  {
    "dist_band": "0.8-1.5%",
    "delta_bucket": "abs_delta_ge_200k",
    "n_clusters": 27,
    "hit_rate": 0.8148148148148148,
    "mean_strength_sum": 6.374193421144064
  },
  {
    "dist_band": "1.5-2.5%",
    "delta_bucket": "abs_delta_lt_200k",
    "n_clusters": 53,
    "hit_rate": 0.4339622641509434,
    "mean_strength_sum": 2.8365675259535066
  },
  {
    "dist_band": "1.5-2.5%",
    "delta_bucket": "abs_delta_ge_200k",
    "n_clusters": 24,
    "hit_rate": 0.3333333333333333,
    "mean_strength_sum": 2.0436869716429094
  },
  {
    "dist_band": "2.5-99.0%",
    "delta_bucket": "abs_delta_lt_200k",
    "n_clusters": 86,
    "hit_rate": 0.023255813953488372,
    "mean_strength_sum": 0.5047111786850951
  },
  {
    "dist_band": "2.5-99.0%",
    "delta_bucket": "abs_delta_ge_200k",
    "n_clusters": 32,
    "hit_rate": 0.0,
    "mean_strength_sum": 0.1262095681416761
  }
]
```

## Hypotheses

- long: n=89, mean max vs EMA59=1.90%, p75=2.37%, strongest-hit=79%, EMA59-reclaim after extreme=58%.
- short: n=82, mean max vs EMA59=1.67%, p75=2.22%, strongest-hit=65%, EMA59-reclaim after extreme=60%.
- From EMA59 touch: near clusters (<0.8%) are usually reachable → checkpoint/noise.
- From EMA59 touch: far clusters (1.5–2.5%) need strong |Δ| + mass/OB or stay unreachable.

## Rule draft

1. Start clock at EMA59 touch (`touch_ts`), not at decision_ts.
2. Target = heaviest cluster on the continuation side of EMA59.
3. Near micro-clusters under ~0.8% are usually noise.
4. Far clusters only if |confirm Δ| strong and OB supportive.

JSON: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/ob_pool_ema59_touch_cluster_calibration.json`
