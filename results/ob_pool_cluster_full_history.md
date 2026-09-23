# OB + Pool Cluster Full-History Calibration

Symbol: `DOGEUSDT`
Window: `2026-09-05T17:00:00+00:00` → `2026-09-19T11:00:00+00:00`
Gap pct: `0.1`

## Universe (Phase A)

- total rows analyzed: **71**
- errors: **0**

| source | n | mean max% | strongest hit |
|--------|---|-----------|---------------|
| fakeout | 46 | 1.84 | 76% |
| scanner_breakout | 15 | 2.46 | 87% |
| strong_breakout | 10 | 2.84 | 60% |

## Outcomes (Phase D)

```json
{
  "working_unmapped": 3,
  "working_to_strong_cluster": 35,
  "failed_early": 13,
  "overshoot_beyond_strong": 17,
  "working_partial": 3
}
```

### By outcome

```json
{
  "working_unmapped": {
    "n": 3,
    "mean_max_exc_pct": 3.123388050184463,
    "mean_confirm_delta": 1457257.5963629398,
    "mean_entry_ob": 1.674604384430255,
    "mean_reversal_strength_sum": null,
    "mean_reversal_n_pools": null,
    "strongest_hit_rate": 0.0
  },
  "working_to_strong_cluster": {
    "n": 35,
    "mean_max_exc_pct": 1.695735444432546,
    "mean_confirm_delta": 146330.50326115088,
    "mean_entry_ob": 1.4241166407719208,
    "mean_reversal_strength_sum": 12.431297936036517,
    "mean_reversal_n_pools": 7.142857142857143,
    "strongest_hit_rate": 1.0
  },
  "failed_early": {
    "n": 13,
    "mean_max_exc_pct": 0.25553266319866524,
    "mean_confirm_delta": 18472.99204919926,
    "mean_entry_ob": 0.8202943049876353,
    "mean_reversal_strength_sum": 7.7108159003647625,
    "mean_reversal_n_pools": 6.777777777777778,
    "strongest_hit_rate": 0.15384615384615385
  },
  "overshoot_beyond_strong": {
    "n": 17,
    "mean_max_exc_pct": 4.345813107510109,
    "mean_confirm_delta": 97365.21630941177,
    "mean_entry_ob": 1.5187360833320953,
    "mean_reversal_strength_sum": 1.6133551868700458,
    "mean_reversal_n_pools": 5.0,
    "strongest_hit_rate": 1.0
  },
  "working_partial": {
    "n": 3,
    "mean_max_exc_pct": 1.2722950962149768,
    "mean_confirm_delta": 112288.41806000001,
    "mean_entry_ob": 1.0619941505223232,
    "mean_reversal_strength_sum": 1.8275067771606182,
    "mean_reversal_n_pools": 2.6666666666666665,
    "strongest_hit_rate": 0.0
  }
}
```

## Dist × Delta reachability grid

```json
[
  {
    "dist_band": "0.0-0.8%",
    "delta_bucket": "delta_lt_200k",
    "n_clusters": 53,
    "hit_rate": 0.9433962264150944,
    "mean_strength_sum": 5.375212404313336
  },
  {
    "dist_band": "0.0-0.8%",
    "delta_bucket": "delta_ge_200k",
    "n_clusters": 22,
    "hit_rate": 0.7727272727272727,
    "mean_strength_sum": 8.954487969460837
  },
  {
    "dist_band": "0.8-1.5%",
    "delta_bucket": "delta_lt_200k",
    "n_clusters": 38,
    "hit_rate": 0.5789473684210527,
    "mean_strength_sum": 5.14046540795857
  },
  {
    "dist_band": "0.8-1.5%",
    "delta_bucket": "delta_ge_200k",
    "n_clusters": 16,
    "hit_rate": 0.6875,
    "mean_strength_sum": 6.183335238918085
  },
  {
    "dist_band": "1.5-2.5%",
    "delta_bucket": "delta_lt_200k",
    "n_clusters": 33,
    "hit_rate": 0.5151515151515151,
    "mean_strength_sum": 2.844373904117167
  },
  {
    "dist_band": "1.5-2.5%",
    "delta_bucket": "delta_ge_200k",
    "n_clusters": 16,
    "hit_rate": 0.1875,
    "mean_strength_sum": 2.875066386478517
  },
  {
    "dist_band": "2.5-99.0%",
    "delta_bucket": "delta_lt_200k",
    "n_clusters": 51,
    "hit_rate": 0.0784313725490196,
    "mean_strength_sum": 0.396109248700845
  },
  {
    "dist_band": "2.5-99.0%",
    "delta_bucket": "delta_ge_200k",
    "n_clusters": 31,
    "hit_rate": 0.03225806451612903,
    "mean_strength_sum": 1.10192192318347
  }
]
```

## Reversal cluster mass

- mean strength_sum: **8.396911396450525**
- mean n_pools: **6.3125**
- mean width_pct: **0.407986038466495**

## Phase E — TP hypotheses

- Near clusters (<0.8%) are usually reachable → treat as noise / checkpoint, not primary TP.
- Far clusters (1.5–2.5%) stay hard to reach even with confirmΔ≥200k → require strong OB + high cluster mass.
- Failed-early moves have weaker entry OB than working moves → OB is a TP-distance filter.
- Working reversals sit in heavier clusters → TP candidate = heaviest meaningful cluster, not nearest pool.
- Scanner longs: strongest-by-mass hit rate 87% (n=15).

## Suggested rule draft (not live yet)

1. Rank upper clusters by **strength_sum** (mass), not by nearest distance.
2. Ignore / skip near micro-clusters as primary TP when entry OB is supportive.
3. Only project TP to a far heavy cluster when confirmΔ is strong **and** entry OB is not flat/ask-heavy.
4. Use the heaviest reached / reversal cluster as the calibration target for TP.

Raw JSON: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/ob_pool_cluster_full_history.json`
