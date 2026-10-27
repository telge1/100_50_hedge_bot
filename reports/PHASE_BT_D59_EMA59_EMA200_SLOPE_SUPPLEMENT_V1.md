# BT-D59 Supplement — EMA59 / EMA200 Slope Research V1

Generated: 2026-10-27T08:07:51Z

## Method
- Causal % change: `(EMA_now - EMA_{t-n}) / EMA_{t-n} * 100` for n ∈ {6,12,24} bars.
- EMA(9/20/59/200): standard recursive α=2/(n+1); first observed close in loaded price series seeds each EMA (same as BT-D54). Not TradingView seed.
- OHLC: OHLC via price_feed_v1 → pd1_landscape_viewer historical_window (≤72h export cap); bars before vis_from are unavailable — 168h warmup may be partial.
- Pool/stack fields merged from existing `asof_upper_pool_evolution.csv` (same BT-D59 run).
- **No fixed trading thresholds**; booleans are exploratory composites only.

## Horizon comparison (EMA59 vs EMA200)
- **6b** reacts first to local flattening; **12b** balances noise; **24b** shows slow EMA200 still lagging.
- `ema59_flattening_6v12`: EMA59 6b slope > 12b slope → recent loss of downward EMA59 momentum.
- `combo_flat59_fall200_12`: flattening EMA59 + EMA200 still falling on 12b horizon.

## Reference snapshots
- **Sep-9 16:30 (good hypothesis):** ema59_6/12/24=-0.2291752570947396, -0.4584025397753656, -0.9542021639904549; ema200_6/12/24=-0.09005390691769105, -0.17139119689469645, -0.3104094574891047; combo_flat59_fall200_12=True
- **Sep-11 12:25 (dense hypothesis):** ema59 slopes -0.04952470766243224, -0.0776216700142012, -0.29236314375789246; ema200_12=-0.044154627708832946; combo=True; combo_plus_stack=True

## Signal comparison (which leads earlier?)
- **EMA59-only flattening** appears before EMA200 turns on dense Sep-11 paths (see `FIRST_combo_flat59_fall200_12` in `ema_slope_signal_comparison.csv`).
- **EMA200-only falling** is true in both good Sep-9 and bad Sep-11 downtrends — **not selective alone**.
- **Combination** (flat EMA59 + falling EMA200) is more selective than EMA200 alone; add stack/cross/chop from raw columns for manual review.

## False block vs avoided bad (audit)
```
[
  {
    "decision_close_utc": "2026-09-09T16:30:00Z",
    "window_id": "sep9_desired_short_1",
    "reference_class": "good_short_user_hypothesis",
    "audit_outcome": "false_block_risk",
    "combo_flat59_fall200_12": true,
    "research_combo_plus_stack": false,
    "ema59_slope_6b_pct": -0.2291752570947396,
    "ema200_slope_12b_pct": -0.17139119689469645
  },
  {
    "decision_close_utc": "2026-09-09T20:55:00Z",
    "window_id": "sep9_desired_short_2",
    "reference_class": "good_short_user_hypothesis",
    "audit_outcome": "false_block_risk",
    "combo_flat59_fall200_12": true,
    "research_combo_plus_stack": false,
    "ema59_slope_6b_pct": -0.3420156660736859,
    "ema200_slope_12b_pct": -0.3720104724096296
  },
  {
    "decision_close_utc": "2026-09-11T12:25:00Z",
    "window_id": "sep11_dense_upper",
    "reference_class": "dense_upper_user_hypothesis",
    "audit_outcome": "warning_present",
    "combo_flat59_fall200_12": true,
    "research_combo_plus_stack": true,
    "ema59_slope_6b_pct": -0.04952470766243224,
    "ema200_slope_12b_pct": -0.044154627708832946
  },
  {
    "decision_close_utc": "2026-09-11T12:35:00Z",
    "window_id": "sep11_dense_upper",
    "reference_class": "dense_upper_user_hypothesis",
    "audit_outcome": "warning_present",
    "combo_flat59_fall200_12": true,
    "research_combo_plus_stack": true,
    "ema59_slope_6b_pct": 0.03164709481037491,
    "ema200_slope_12b_pct": -0.015818963089911747
  }
]
```

## Conclusion (research hypothesis)
`combo_flat59_fall200_12` is **TRUE on Sep-9 good anchors** (false-block risk) — EMA59 flattening vs falling EMA200 is **not selective alone** in a continuing downtrend.
`research_combo_plus_stack` is **FALSE on Sep-9 anchors** but **TRUE at Sep-11 12:25/12:35** — adding upper-stack context improves separation in this sample (still not a production threshold).
EMA200 slope alone stays negative in both good and dense cases — **poor standalone warning**.
EMA59 **6b** slopes turn less negative / positive earlier than EMA200 at Sep-11 12:35 (see CSV).

**Do not** promote to strategy gate without 168h OHLC and held-out validation.

## Outputs
- `results/phase_bt_d59_market_regime_v1/ema59_ema200_slope_research.csv`
- `results/phase_bt_d59_market_regime_v1/ema_slope_signal_comparison.csv`
- `results/phase_bt_d59_market_regime_v1/ema_slope_audit_false_block.csv`

## VERDICT
EMA_SLOPE_RESEARCH_SUPPLEMENT_COMPLETE (research-only; partial OHLC warmup unchanged)
