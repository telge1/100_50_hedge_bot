# E1R V1 — 4-month OOS (Apr–Jul 2026)

**Verdict:** `ROBUST_OOS`

## Data coverage
{
  "candle_15m": "2025-12-11 .. 2026-09-26 (strict_complete_buckets=True)",
  "pane_load": "2026-02-10 .. 2026-09-30",
  "warmup_note": "PANE_FROM 2026-02-10; EMA200 needs ~200x15m bars inside pane",
  "pool_path": "scanner_pools_for_index + H4FloorGuard (floor_guard_signal_list_v1 pipeline)",
  "data_gap": "No candles after 2026-09-26 09:00 UTC in current store"
}

Extended candles available to **2026-09-26T12:00:00+00:00** (not in primary 4mo report).

## Dev parity (Jun–Jul E1R trade count)
{
  "recomputed": {
    "XRPUSDT": 15,
    "ADAUSDT": 11,
    "DOGEUSDT": 22
  },
  "freeze": {
    "XRPUSDT": 15,
    "ADAUSDT": 11,
    "DOGEUSDT": 22
  },
  "ok": true
}

## Comparison
| Metrik | Jun–Jul Freeze | 4 Monate | OOS Apr–May |
|---|---:|---:|---:|
| Trades | 48 | 55 | 7 |
| PnL % | 64.2888 | 68.1495 | 3.8607 |
| PF | 13.0798 | None | None |
| Max DD % | 1.7465 | None | None |
| Winrate | 0.8333333333333334 | 0.8148 | 0.5714 |
| Max SL-Serie | 2 | 2 | 2 |

## Per coin (research, E1R allowed)
{
  "XRPUSDT": {
    "trades": 18,
    "tp_count": 14,
    "sl_count": 4,
    "winrate": 0.7778,
    "gross_profit": 25.1208,
    "gross_loss": -2.6872,
    "total_pnl_pct": 22.4336,
    "profit_factor": 9.3483,
    "max_drawdown_pct": 1.2164,
    "max_sl_streak": 2,
    "mean_mae": 0.3766,
    "mean_mfe": 1.8016,
    "median_mae": 0.293,
    "median_mfe": 1.2822,
    "mean_tp_dist_pct": 1.7053,
    "mean_sl_dist_pct": 0.6423,
    "mean_rr": 2.7113,
    "median_rr": 2.4128
  },
  "ADAUSDT": {
    "trades": 15,
    "tp_count": 12,
    "sl_count": 3,
    "winrate": 0.8,
    "gross_profit": 22.4279,
    "gross_loss": -3.5191,
    "total_pnl_pct": 18.9088,
    "profit_factor": 6.3732,
    "max_drawdown_pct": 2.1017,
    "max_sl_streak": 2,
    "mean_mae": 0.4377,
    "mean_mfe": 1.8238,
    "median_mae": 0.2505,
    "median_mfe": 1.8868,
    "mean_tp_dist_pct": 1.8482,
    "mean_sl_dist_pct": 0.7703,
    "mean_rr": 2.7609,
    "median_rr": 2.3446
  },
  "DOGEUSDT": {
    "trades": 22,
    "tp_count": 18,
    "sl_count": 3,
    "winrate": 0.8571,
    "gross_profit": 28.5566,
    "gross_loss": -1.7495,
    "total_pnl_pct": 26.8071,
    "profit_factor": 16.3227,
    "max_drawdown_pct": 1.0579,
    "max_sl_streak": 1,
    "mean_mae": 0.2752,
    "mean_mfe": 1.5975,
    "median_mae": 0.2141,
    "median_mfe": 1.4429,
    "mean_tp_dist_pct": 1.6464,
    "mean_sl_dist_pct": 0.5693,
    "mean_rr": 3.0838,
    "median_rr": 2.679
  }
}

## Per month
{
  "2026-04": {
    "trades": 6,
    "tp_count": 3,
    "sl_count": 3,
    "winrate": 0.5,
    "gross_profit": 5.4554,
    "gross_loss": -2.6338,
    "total_pnl_pct": 2.8216,
    "profit_factor": 2.0713,
    "max_drawdown_pct": 2.6338,
    "max_sl_streak": 2,
    "mean_mae": 0.6983,
    "mean_mfe": 1.3756,
    "median_mae": 0.6351,
    "median_mfe": 0.9606,
    "mean_tp_dist_pct": 1.6422,
    "mean_sl_dist_pct": 0.7305,
    "mean_rr": 2.4031,
    "median_rr": 2.0656
  },
  "2026-05": {
    "trades": 1,
    "tp_count": 1,
    "sl_count": 0,
    "winrate": 1.0,
    "gross_profit": 1.0391,
    "gross_loss": 0,
    "total_pnl_pct": 1.0391,
    "profit_factor": null,
    "max_drawdown_pct": 0.0,
    "max_sl_streak": 0,
    "mean_mae": 0.1663,
    "mean_mfe": 1.4131,
    "median_mae": 0.1663,
    "median_mfe": 1.4131,
    "mean_tp_dist_pct": 1.0391,
    "mean_sl_dist_pct": 0.8247,
    "mean_rr": 1.26,
    "median_rr": 1.26
  },
  "2026-06": {
    "trades": 20,
    "tp_count": 19,
    "sl_count": 1,
    "winrate": 0.95,
    "gross_profit": 36.4931,
    "gross_loss": -0.6916,
    "total_pnl_pct": 35.8015,
    "profit_factor": 52.7662,
    "max_drawdown_pct": 0.6916,
    "max_sl_streak": 1,
    "mean_mae": 0.2043,
    "mean_mfe": 2.0751,
    "median_mae": 0.1608,
    "median_mfe": 1.6873,
    "mean_tp_dist_pct": 1.9796,
    "mean_sl_dist_pct": 0.6359,
    "mean_rr": 3.363,
    "median_rr": 2.8277
  },
  "2026-07": {
    "trades": 28,
    "tp_count": 21,
    "sl_count": 6,
    "winrate": 0.7778,
    "gross_profit": 33.1177,
    "gross_loss": -4.6304,
    "total_pnl_pct": 28.4873,
    "profit_factor": 7.1522,
    "max_drawdown_pct": 1.7465,
    "max_sl_streak": 2,
    "mean_mae": 0.3913,
    "mean_mfe": 1.5629,
    "median_mae": 0.2831,
    "median_mfe": 1.4229,
    "mean_tp_dist_pct": 1.577,
    "mean_sl_dist_pct": 0.6327,
    "mean_rr": 2.683,
    "median_rr": 2.5889
  }
}

## Execution-realistic (1 pos/coin)
{
  "trades": 47,
  "tp_count": 38,
  "sl_count": 8,
  "winrate": 0.8261,
  "gross_profit": 66.8742,
  "gross_loss": -6.9568,
  "total_pnl_pct": 59.9174,
  "profit_factor": 9.6128,
  "max_drawdown_pct": 2.1116,
  "max_sl_streak": 2,
  "mean_mae": 0.3596,
  "mean_mfe": 1.7752,
  "median_mae": 0.2538,
  "median_mfe": 1.4662,
  "mean_tp_dist_pct": 1.7599,
  "mean_sl_dist_pct": 0.6604,
  "mean_rr": 2.8999,
  "median_rr": 2.5419
}

## Stability
{
  "all_coins_profitable": true,
  "all_months_profitable": true,
  "coin_pnl": {
    "XRPUSDT": 22.4336,
    "ADAUSDT": 18.9088,
    "DOGEUSDT": 26.8071
  },
  "monthly_pnl": {
    "2026-04": 2.8216,
    "2026-05": 1.0391,
    "2026-06": 35.8015,
    "2026-07": 28.4873
  },
  "pnl_without_best_coin": 41.3424,
  "pnl_without_best_month": 32.348,
  "best_coin": "DOGEUSDT",
  "best_month": "2026-06"
}

## Costs
{
  "fees": "NOT_AVAILABLE",
  "funding": "NOT_AVAILABLE",
  "slippage": "NOT_AVAILABLE"
}
