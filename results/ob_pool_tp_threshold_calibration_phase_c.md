# TP Cluster Threshold Calibration (Phase C)

Input: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/ob_pool_ema59_touch_cluster_calibration.json`
Symbol: `DOGEUSDT`
n touches: **171**

Same idea as DOGE OB calibration Phase C: quantiles + grid + walk-forward,
but for **pool-cluster TP projection** from EMA59 touch.

## LONG

- n=89 train=62 test=27
- labels: `{"working": 58, "working_partial": 4, "failed_early": 17, "weak": 10}`

### Quantile proposal

```json
{
  "side": "long",
  "n_working": 62,
  "n_failed": 17,
  "near_noise_max_dist_pct": 1.0,
  "min_target_dist_pct": 0.8,
  "max_target_dist_pct": 2.15,
  "min_cluster_strength_sum": 5.51,
  "typical_reversal_strength_sum": 5.51,
  "min_abs_confirm_delta": 71000.0,
  "failed_abs_confirm_delta_p75": 210000.0,
  "min_ob_ratio": 1.0,
  "failed_ob_p75": 1.53
}
```

### Suggested rule (quantile-primary)

```json
{
  "near_noise_max_dist_pct": 1.0,
  "min_target_dist_pct": 0.8,
  "max_target_dist_pct": 2.15,
  "min_cluster_strength_sum": 5.51,
  "min_abs_confirm_delta": 71000.0,
  "min_ob_ratio_long": 1.0,
  "max_ob_ratio_short": 1.0,
  "require_ob": true
}
```

- full-sample: hit=0.625 projected=16 coverage=0.21052631578947367
- train: hit=0.6428571428571429 projected=14 coverage=0.2641509433962264 score=0.5171159029649597
- test: hit=0.5 projected=2 coverage=0.08695652173913043 score=-1.0

### Suggested live wording

- ignore clusters closer than **1.0%** as primary TP
- TP candidate = heaviest cluster with dist **0.8–2.15%** and strength_sum ≥ **5.51**
- only project if |confirm Δ| ≥ **71000.0** and OB bid/ask ≥ **1.0**

## SHORT

- n=82 train=57 test=25
- labels: `{"working": 40, "working_partial": 11, "failed_early": 19, "weak": 12}`

### Quantile proposal

```json
{
  "side": "short",
  "n_working": 51,
  "n_failed": 19,
  "near_noise_max_dist_pct": 1.0,
  "min_target_dist_pct": 0.8,
  "max_target_dist_pct": 2.57,
  "min_cluster_strength_sum": 6.91,
  "typical_reversal_strength_sum": 6.91,
  "min_abs_confirm_delta": 76000.0,
  "failed_abs_confirm_delta_p75": 300000.0,
  "max_ob_ratio": 1.05,
  "failed_ob_p25": 0.812
}
```

### Suggested rule (quantile-primary)

```json
{
  "near_noise_max_dist_pct": 1.0,
  "min_target_dist_pct": 0.8,
  "max_target_dist_pct": 2.57,
  "min_cluster_strength_sum": 6.91,
  "min_abs_confirm_delta": 76000.0,
  "min_ob_ratio_long": 1.05,
  "max_ob_ratio_short": 1.05,
  "require_ob": true
}
```

- full-sample: hit=0.6 projected=15 coverage=0.18518518518518517
- train: hit=0.8 projected=10 coverage=0.17857142857142858 score=0.6257142857142857
- test: hit=0.2 projected=5 coverage=0.2 score=0.09999999999999998

### Suggested live wording

- ignore clusters closer than **1.0%** as primary TP
- TP candidate = heaviest cluster with dist **0.8–2.57%** and strength_sum ≥ **6.91**
- only project if |confirm Δ| ≥ **76000.0** and OB bid/ask ≤ **1.05** (ask-heavy)

## Status

- Suggestion only — **not** wired into live exit code yet.
- Next: apply rule on the 10 locked scanner longs as a sanity check,
  then implement in `simulate_long` if stable.
