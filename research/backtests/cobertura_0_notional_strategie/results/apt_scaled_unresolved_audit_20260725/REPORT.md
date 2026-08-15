# APT Scaled Unresolved Audit (22 cases)

**Decision: `UNRESOLVED_AUDIT_PASS_WITH_WARNINGS`**

Scope: all unresolved `individual_tp_scaled` runs from
`apt_multistart_validation_20260725`. Exact 60d replay fingerprint vs
`raw_runs.csv`; no strategy/parameter changes.

## Answers

1. Recover at 90d: **14 / 22** (8 remain open past day 90).
2. Recover at 120d: **22 / 22** (all late cases recover between ~day 94 and ~day 105).
3. Recover by data end: **22 / 22** (hard unresolved forever: **0**).
4. Near-BE (best economics during 60d): within 1 USDT **2**, within 5 **6**, within 10 **9**.
   - near_be_1 → recover by 90d: 2/2; fell back >20 USDT after near-BE: 2
   - near_be_5 → recover by 90d: 5/6; fell back >20 USDT after near-BE: 6
   - near_be_10 → recover by 90d: 8/9; fell back >20 USDT after near-BE: 9
5. Most frequent primary cause: **TP_HARVEST_TOO_SLOW** (primary counts={'TP_HARVEST_TOO_SLOW': 18, 'OTHER': 2, 'V_REVERSAL': 1, 'INSUFFICIENT_REBOUND': 1}; any-cause counts={'TP_HARVEST_TOO_SLOW': 18, 'LARGE_OPEN_OVERLAY': 15, 'V_REVERSAL': 19, 'OTHER': 2, 'OVERLAY_SATURATED': 9, 'NEAR_BE_AT_HORIZON': 2, 'INSUFFICIENT_REBOUND': 1}).
6. Overlay grows faster than TP harvest: **18/22** cases (adds outpace scaled partial closes for most of the path).
7. Hard unresolved until data end: **0**. There is **no permanent unresolved cohort** on APT for this sample; the 60d cutoff and secondarily the 90d window create the open set.
8. Economic profile: **slow but capital-heavy**, not a few USDT short of BE. Median end distance-to-BE `83.2` USDT; median best econ `-13.0`; median max drawdown `179.2`; median max overlay notional `606.7` USDT. Cases with heavy drawdown/overlay/end-gap: **18/22**. Mostly delayed recovery under heavy interim inventory, not harmless near-misses.
9. Replay mismatches: **0**; invariant fails: **0**. No silent corrections.
10. Multi-Coin readiness for `individual_tp_scaled`: **cautious pilot only**. APT multistart recovery is strong by day 120 and fingerprints are clean, but unresolved cases show systematic overlay/TP harvest lag, large interim overlay notionals, and frequent post-near-BE givebacks. Do not treat the 60d unresolved cluster as economically mild.

## Cause rules (deterministic)

```
Cause-classification rules (deterministic, non-ML):
- CONTINUED_DOWNTREND: max_drop_from_start_pct <= -10 AND
  (end_ret_pct <= -8 OR end_price within 3% of window min)
- INSUFFICIENT_REBOUND: max_drop_from_start_pct <= -5 AND
  max_rally_from_low_pct < 5
- TP_HARVEST_TOO_SLOW: overlay_grows_faster_than_tp_harvest is True
  (cumulative adds exceed cumulative TP closes for >50% of post-first-add bars)
- OVERLAY_SATURATED: max_overlay_to_core_ratio >= 3.5 OR number_of_short_adds >= 7
- FEES_DRAG: total_fees_usdt >= 5 AND best_total_economics_usdt < 0.25
- NEAR_BE_AT_HORIZON: best_total_economics_usdt >= -1.0
- LARGE_OPEN_OVERLAY: unresolved_overlay_qty >= 0.5 * initial_long_qty
- LOW_VOLATILITY_AFTER_DROP: max_drop <= -5 AND
  (max_price - min_price)/start_price < 0.08 after the low is set
  (proxy: max_rally_from_low < 4 AND continued mild drift)
- V_REVERSAL: max_rally_from_low_pct >= 10 AND max_drop_from_start_pct <= -5
  AND still unresolved (rebound happened but BE not locked)
- OTHER: none of the above

```

## Worst-case lists

See `worst_case_lists.csv`: largest drawdown / overlay / end distance-to-BE, closest miss (best econ), and unresolved-until-data-end (empty here).

## Artifacts

- `unresolved_case_summary.csv|json`
- `unresolved_order_timeline.csv`
- `unresolved_fill_ledger.csv`
- `unresolved_tranche_state.csv`
- `unresolved_cause_classification.csv`
- `unresolved_extended_horizon.csv`
- `unresolved_near_be_analysis.csv`
- `unresolved_market_path.csv`
- `replay_mismatches.csv`, `invariant_violations.csv`
- `cases/<run_id>.md` (22 walkthroughs)

## Decision

`UNRESOLVED_AUDIT_PASS_WITH_WARNINGS`
