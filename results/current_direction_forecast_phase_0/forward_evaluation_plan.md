# Forward Evaluation Plan

**Generated (UTC):** `2026-09-05T14:33:50Z`

## Important non-transfer

LLD Nearest-Target TEST Acc 0.791 / directional 0.854 is a **different target** (pool first-touch) and must **not** be cited as accuracy for CURRENT_DIRECTION_FORECAST.

```text
historical_full_ob_training_samples = 0
calibrated_accuracy = unknown
```

Calibrated direction probability is **not** possible today.

## Manual vs automatic

| Mode | Pros | Cons |
|------|------|------|
| Manual CLI `--now` | Exact chart time | **Selection bias** toward interesting moments |
| Auto shadow sampler (e.g. every 15m BTC+DOGE) | Objective sample | Needs stable data + bridge first |

## Phased plan

1. **Technical CLI pilot** (after bridge): ≥10 fully closed forecasts; freeze/hash/outcome gates pass. Prefer **State Analyzer** output first.  
2. **Provisional heuristic eval**: ≥200 rule/auto forecasts; multiple UTC days; no critical gaps; per-horizon metrics. Heuristic weights frozen **before** looking at results.  
3. **Final untouched forward test:** ≥14 new closed UTC days; ≥1000 forecasts; contracts frozen; no changes during test.

## Selection-bias control

- Manual pilots labeled `sample_mode=manual`.  
- Decision metrics computed primarily on `sample_mode=scheduled_shadow`.  
- Never mix LLD destination labels into this evaluation.
