# LONG V3 Phase 3 — Multi-Month Backtest (Variant A)

- **Run ID:** `phase3_a_20261030T160156Z_01a05c48`
- **Status:** `COMPLETED_VALIDATED`
- **Completed UTC:** 2026-10-30T16:06:14Z
- **Git HEAD:** `cfd3470d68f4d0cb949d5d9b5925720420dbcf42`
- **Eval window:** `2026-06-08T00:00:00Z` → `2026-09-30T23:40:00Z`
- **Merged closes:** 35133

## Variant scope

- **Variant A (FROZEN_AT_PLACE_V2J):** executed — statistics below.
- **Variant B (DYNAMIC_UPPER_GROWTH_V2K):** **not executed** in Phase 3; no A/B trade delta or dynamic-repricing comparison in this run.

## Month coverage

- **2026-05** (BLOCKED): None → None
- **2026-06** (FULL): 2026-06-01T00:00:00Z → 2026-06-30T23:55:00Z
- **2026-07** (FULL): 2026-07-01T00:00:00Z → 2026-07-31T23:55:00Z
- **2026-08** (FULL): 2026-08-01T00:00:00Z → 2026-08-31T23:55:00Z
- **2026-09** (PARTIAL): 2026-09-01T00:00:00Z → 2026-09-30T23:40:00Z

## Global summary (Variant A)

```json
{
  "fills": 36,
  "places": 45,
  "tp_exits": 23,
  "sl_exits": 13,
  "ema_exits": 0,
  "ambiguous_exits": 0,
  "cancels": 9,
  "replaces": 13,
  "sum_gross_return_pct": -1.850189,
  "sum_wins_pct": 17.635899,
  "sum_losses_pct": -19.486088,
  "fees_slippage": "not_modeled",
  "entry_config": "sup=V2E_QUALIFIED|sl=DEEPER_Q_UPPER|rec=NEXT_BAR|V2E|age=CAUSAL_REGION_LINEAGE_5|form=CAUSAL_BELOW_PRICE_SEGMENT|life=LINEAGE_AWARE_V2J",
  "touch_event_counts": {
    "TOUCH_BEGIN": 108,
    "RECOVERY_CONFIRMED": 45,
    "TOUCH_ABORT_INVALID": 50,
    "TOUCH_ABORT_BREAK": 11,
    "RESET_CONTEXT_LOST": 2,
    "TOUCH_REJECTED_NOT_ACTIVE_AT_CLOSE": 1
  }
}
```

## Monthly summary (Variant A)

### 2026-06
```json
{
  "month_key": "2026-06",
  "completed_trades": 8,
  "win_trades": 5,
  "loss_trades": 3,
  "hit_rate": 0.625,
  "tp_exits": 5,
  "sl_exits": 3,
  "open_positions_end_slice": 0,
  "unfilled_buy_limits_estimate": 1,
  "cancels": 1,
  "replaces": 1,
  "avg_tp_target_pct": 0.643889,
  "avg_sl_distance_pct": 2.272863,
  "avg_realized_win_pct": 0.643889,
  "avg_realized_loss_pct": -2.162815,
  "sum_gross_trade_returns_pct": -3.269002,
  "fees_slippage": "not_modeled",
  "max_consecutive_sl": 2,
  "worst_loss_streak_sum_pct": -4.188024,
  "note": "Eval starts 2026-06-08T00:00:00Z; first ~168h of June are warmup-only in merged timeline."
}
```

### 2026-07
```json
{
  "month_key": "2026-07",
  "completed_trades": 6,
  "win_trades": 4,
  "loss_trades": 2,
  "hit_rate": 0.666667,
  "tp_exits": 4,
  "sl_exits": 2,
  "open_positions_end_slice": 0,
  "unfilled_buy_limits_estimate": 3,
  "cancels": 3,
  "replaces": 2,
  "avg_tp_target_pct": 0.892304,
  "avg_sl_distance_pct": 2.059514,
  "avg_realized_win_pct": 0.892304,
  "avg_realized_loss_pct": -1.578076,
  "sum_gross_trade_returns_pct": 0.413065,
  "fees_slippage": "not_modeled",
  "max_consecutive_sl": 1,
  "worst_loss_streak_sum_pct": -2.104975,
  "note": ""
}
```

### 2026-08
```json
{
  "month_key": "2026-08",
  "completed_trades": 7,
  "win_trades": 4,
  "loss_trades": 3,
  "hit_rate": 0.571429,
  "tp_exits": 4,
  "sl_exits": 3,
  "open_positions_end_slice": 0,
  "unfilled_buy_limits_estimate": 1,
  "cancels": 1,
  "replaces": 2,
  "avg_tp_target_pct": 0.691823,
  "avg_sl_distance_pct": 1.274451,
  "avg_realized_win_pct": 0.691823,
  "avg_realized_loss_pct": -0.96059,
  "sum_gross_trade_returns_pct": -0.114478,
  "fees_slippage": "not_modeled",
  "max_consecutive_sl": 1,
  "worst_loss_streak_sum_pct": -1.15983,
  "note": ""
}
```

### 2026-09
```json
{
  "month_key": "2026-09",
  "completed_trades": 15,
  "win_trades": 10,
  "loss_trades": 5,
  "hit_rate": 0.666667,
  "tp_exits": 10,
  "sl_exits": 5,
  "open_positions_end_slice": 0,
  "unfilled_buy_limits_estimate": 4,
  "cancels": 4,
  "replaces": 8,
  "avg_tp_target_pct": 0.807995,
  "avg_sl_distance_pct": 2.008742,
  "avg_realized_win_pct": 0.807995,
  "avg_realized_loss_pct": -1.391944,
  "sum_gross_trade_returns_pct": 1.120226,
  "fees_slippage": "not_modeled",
  "max_consecutive_sl": 1,
  "worst_loss_streak_sum_pct": -3.033473,
  "note": ""
}
```

## Variant B

Not run — additional/removed trades from dynamic repricing: **N/A**.

*Sum of per-trade gross returns is not account equity or drawdown; fees/slippage not modeled.*