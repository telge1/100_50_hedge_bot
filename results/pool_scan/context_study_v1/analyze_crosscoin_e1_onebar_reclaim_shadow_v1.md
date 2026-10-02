# Cross-Coin E1 vs E1R One-Bar Reclaim Shadow v1

### E1R definition
Start wie E1. Bei close<=EMA: wenn rising_3+Struktur intakt → PENDING eine Bar; Reclaim wenn nächste Bar close>EMA+rising_3+Struktur; sonst Reset. Pending-Signale: PENDING_NOT_RESOLVED.

### XRP 12 June reclaim
{
  "dip_time": "2026-06-12T06:45:00+00:00",
  "dip_close": 1.1279,
  "ema200": 1.1280170021188072,
  "distance_below_ema_pct": -0.0104,
  "rising_3": true,
  "cluster_1_top": 1.0509,
  "cluster_2_top": 1.0902,
  "cluster_3_top": 1.1065,
  "gap_1_2": 0.01585,
  "gap_2_3": 0.00575,
  "gap_1_2_status": "positive",
  "gap_2_3_status": "positive",
  "overlap_1_2": false,
  "overlap_2_3": false,
  "structural_gap_intact": true,
  "structure_fail_reason": null,
  "cluster_2_pool_ids": [
    "lld:XRPUSDT:15m:lower:1780741800",
    "lld:XRPUSDT:15m:lower:1780776000",
    "lld:XRPUSDT:15m:lower:1780768800",
    "lld:XRPUSDT:15m:lower:1780784100",
    "lld:XRPUSDT:15m:lower:1781127900",
    "lld:XRPUSDT:15m:lower:1780794000",
    "lld:XRPUSDT:15m:lower:1781133300",
    "lld:XRPUSDT:15m:lower:1781125200"
  ],
  "cluster_3_pool_ids": [
    "lld:XRPUSDT:15m:lower:1781198100",
    "lld:XRPUSDT:15m:lower:1781145900",
    "lld:XRPUSDT:15m:lower:1781195400",
    "lld:XRPUSDT:15m:lower:1781190000"
  ],
  "next_bar_time": "2026-06-12T07:00:00+00:00",
  "next_close": 1.1294,
  "next_ema200": 1.1280307632917543,
  "next_distance_close_to_ema_pct": 0.1214,
  "rising_3_next_bar": true,
  "structure_next_bar": {
    "rising_3": true,
    "cluster_1_top": 1.0509,
    "cluster_2_top": 1.0902,
    "cluster_3_top": 1.1065,
    "gap_1_2": 0.01585,
    "gap_2_3": 0.00575,
    "gap_1_2_status": "positive",
    "gap_2_3_status": "positive",
    "overlap_1_2": false,
    "overlap_2_3": false,
    "structural_gap_intact": true,
    "structure_fail_reason": null,
    "cluster_2_pool_ids": [
      "lld:XRPUSDT:15m:lower:1780741800",
      "lld:XRPUSDT:15m:lower:1780776000",
      "lld:XRPUSDT:15m:lower:1780768800",
      "lld:XRPUSDT:15m:lower:1780784100",
      "lld:XRPUSDT:15m:lower:1781127900",
      "lld:XRPUSDT:15m:lower:1780794000",
      "lld:XRPUSDT:15m:lower:1781133300",
      "lld:XRPUSDT:15m:lower:1781125200"
    ],
    "cluster_3_pool_ids": [
      "lld:XRPUSDT:15m:lower:1781198100",
      "lld:XRPUSDT:15m:lower:1781145900",
      "lld:XRPUSDT:15m:lower:1781195400",
      "lld:XRPUSDT:15m:lower:1781190000"
    ]
  },
  "result": "EMA_RECLAIM_SUCCESS"
}

### XRP BAD-4
[
  {
    "signal_num": 8,
    "entry_open": "2026-06-11T19:15:00+00:00",
    "entry_price": 1.1414,
    "stop": 1.1533521,
    "tp": 1.1065,
    "pool_id": "lld:XRPUSDT:15m:upper:1781199900",
    "first_hit": "SL",
    "same_bar_sl_tp": false,
    "exit_time": "2026-06-12T15:15:00+00:00",
    "exit_price": 1.1533521,
    "outcome": "stop",
    "result_status": "unresolved",
    "mfe_pct": 1.5069,
    "mae_pct": 1.3405,
    "pnl_pct": -1.0471,
    "risk_pct": 1.0471,
    "r_multiple": -1.0,
    "horizon_mark_pnl_pct": null,
    "symbol": "XRPUSDT",
    "variant": "without_guard",
    "cumulative_pnl_pct": 5.0409,
    "equity_peak_pct": 10.0474,
    "drawdown_from_peak_pct": 5.0065,
    "signal_time": "2026-06-11T19:15:00+00:00",
    "decision_time": "2026-06-11T19:30:00+00:00",
    "decision_dt": "2026-06-11 19:30:00+00:00",
    "final_outcome": "MOVE_041_THEN_SL",
    "current_guard_allows": true,
    "E1_active": true,
    "E1R_active": true,
    "E1_status": "ACTIVE",
    "E1R_status": "ACTIVE",
    "ignored_E1": true,
    "ignored_E1R": true,
    "would_ignore_E1": true,
    "would_ignore_E1R": true
  },
  {
    "signal_num": 9,
    "entry_open": "2026-06-14T21:45:00+00:00",
    "entry_price": 1.1675,
    "stop": 1.1861175,
    "tp": 1.1308,
    "pool_id": "lld:XRPUSDT:15m:upper:1780979400",
    "first_hit": "SL",
    "same_bar_sl_tp": false,
    "exit_time": "2026-06-15T00:00:00+00:00",
    "exit_price": 1.1861175,
    "outcome": "stop",
    "result_status": "bad",
    "mfe_pct": 0.4454,
    "mae_pct": 1.6959,
    "pnl_pct": -1.5946,
    "risk_pct": 1.5946,
    "r_multiple": -1.0,
    "horizon_mark_pnl_pct": null,
    "symbol": "XRPUSDT",
    "variant": "without_guard",
    "cumulative_pnl_pct": 3.4463,
    "equity_peak_pct": 10.0474,
    "drawdown_from_peak_pct": 6.6011,
    "signal_time": "2026-06-14T21:45:00+00:00",
    "decision_time": "2026-06-14T22:00:00+00:00",
    "decision_dt": "2026-06-14 22:00:00+00:00",
    "final_outcome": "MOVE_041_THEN_SL",
    "current_guard_allows": true,
    "E1_active": true,
    "E1R_active": true,
    "E1_status": "ACTIVE",
    "E1R_status": "ACTIVE",
    "ignored_E1": true,
    "ignored_E1R": true,
    "would_ignore_E1": true,
    "would_ignore_E1R": true
  },
  {
    "signal_num": 10,
    "entry_open": "2026-06-14T22:30:00+00:00",
    "entry_price": 1.1744,
    "stop": 1.1830113,
    "tp": 1.1308,
    "pool_id": "lld:XRPUSDT:15m:upper:1780983000",
    "first_hit": "SL",
    "same_bar_sl_tp": false,
    "exit_time": "2026-06-15T00:00:00+00:00",
    "exit_price": 1.1830113,
    "outcome": "stop",
    "result_status": "bad",
    "mfe_pct": 0.5535,
    "mae_pct": 1.0984,
    "pnl_pct": -0.7333,
    "risk_pct": 0.7333,
    "r_multiple": -1.0,
    "horizon_mark_pnl_pct": null,
    "symbol": "XRPUSDT",
    "variant": "without_guard",
    "cumulative_pnl_pct": 2.713,
    "equity_peak_pct": 10.0474,
    "drawdown_from_peak_pct": 7.3344,
    "signal_time": "2026-06-14T22:30:00+00:00",
    "decision_time": "2026-06-14T22:45:00+00:00",
    "decision_dt": "2026-06-14 22:45:00+00:00",
    "final_outcome": "MOVE_041_THEN_SL",
    "current_guard_allows": true,
    "E1_active": true,
    "E1R_active": true,
    "E1_status": "ACTIVE",
    "E1R_status": "ACTIVE",
    "ignored_E1": true,
    "ignored_E1R": true,
    "would_ignore_E1": true,
    "would_ignore_E1R": true
  },
  {
    "signal_num": 11,
    "entry_open": "2026-06-14T23:30:00+00:00",
    "entry_price": 1.1793,
    "stop": 1.1863179,
    "tp": 1.1308,
    "pool_id": "lld:XRPUSDT:15m:upper:1780959600",
    "first_hit": "SL",
    "same_bar_sl_tp": false,
    "exit_time": "2026-06-15T00:00:00+00:00",
    "exit_price": 1.1863179,
    "outcome": "stop",
    "result_status": "bad",
    "mfe_pct": 0.0254,
    "mae_pct": 0.6784,
    "pnl_pct": -0.5951,
    "risk_pct": 0.5951,
    "r_multiple": -1.0,
    "horizon_mark_pnl_pct": null,
    "symbol": "XRPUSDT",
    "variant": "without_guard",
    "cumulative_pnl_pct": 2.1179,
    "equity_peak_pct": 10.0474,
    "drawdown_from_peak_pct": 7.9295,
    "signal_time": "2026-06-14T23:30:00+00:00",
    "decision_time": "2026-06-14T23:45:00+00:00",
    "decision_dt": "2026-06-14 23:45:00+00:00",
    "final_outcome": "SL_FIRST",
    "current_guard_allows": true,
    "E1_active": true,
    "E1R_active": true,
    "E1_status": "ACTIVE",
    "E1R_status": "ACTIVE",
    "ignored_E1": true,
    "ignored_E1R": true,
    "would_ignore_E1": true,
    "would_ignore_E1R": true
  }
]

### XRP current vs E1 vs E1R
| Metrik | CURRENT | E1 | E1R |
|---|--:|--:|--:|
| allowed_trades | 25 | 15 | 15 |
| ignored_trades | 0 | 10 | 10 |
| sl | 8 | 2 | 2 |
| TP_FIRST | 17 | 13 | 13 |
| tp_first_lost | 0 | 4 | 4 |
| MOVE_041_THEN_SL | 6 | 2 | 2 |
| DIRECT_SL | 2 | 0 | 0 |
| mfe_ge_041_before_sl | 6 | 2 | 2 |
| median_mae | 0.3417 | 0.2524 | 0.2524 |
| median_mfe | 1.2265 | 1.4152 | 1.4152 |
| max_sl_streak | 4 | 1 | 1 |
| bad4_removed | 0 | 4 | 4 |

### XRP additional ignores caused by reclaim
count=0

### ADA current vs E1 vs E1R
| Metrik | CURRENT | E1 | E1R |
|---|--:|--:|--:|
| allowed_trades | 18 | 11 | 11 |
| ignored_trades | 0 | 7 | 7 |
| sl | 7 | 2 | 2 |
| TP_FIRST | 11 | 9 | 9 |
| tp_first_lost | 0 | 2 | 2 |
| MOVE_041_THEN_SL | 4 | 2 | 2 |
| DIRECT_SL | 3 | 0 | 0 |
| mfe_ge_041_before_sl | 4 | 2 | 2 |
| median_mae | 0.2825 | 0.2505 | 0.2505 |
| median_mfe | 1.6563 | 2.0038 | 2.0038 |
| max_sl_streak | 4 | 2 | 2 |
| bad4_removed | 0 | 0 | 0 |

### DOGE current vs E1 vs E1R
| Metrik | CURRENT | E1 | E1R |
|---|--:|--:|--:|
| allowed_trades | 28 | 22 | 22 |
| ignored_trades | 0 | 6 | 6 |
| sl | 8 | 3 | 3 |
| TP_FIRST | 19 | 18 | 18 |
| tp_first_lost | 0 | 1 | 1 |
| MOVE_041_THEN_SL | 4 | 2 | 2 |
| DIRECT_SL | 4 | 1 | 1 |
| mfe_ge_041_before_sl | 4 | 2 | 2 |
| median_mae | 0.2625 | 0.2141 | 0.2141 |
| median_mfe | 1.3489 | 1.4429 | 1.4429 |
| max_sl_streak | 4 | 1 | 1 |
| bad4_removed | 0 | 0 | 0 |

### Reclaim success/failure counts
{
  "XRPUSDT": [
    36,
    81
  ],
  "ADAUSDT": [
    36,
    63
  ],
  "DOGEUSDT": [
    24,
    57
  ]
}

### Lost TP_FIRST
{"XRPUSDT": [4, 4], "ADAUSDT": [2, 2], "DOGEUSDT": [1, 1]}

### SL reduction
{"XRPUSDT": [6, 6], "ADAUSDT": [5, 5], "DOGEUSDT": [5, 5]}

### SL streaks
{"XRPUSDT": [1, 1], "ADAUSDT": [2, 2], "DOGEUSDT": [1, 1]}

### MAE / MFE
XRP CURRENT 0.3417/1.2265; E1 0.2524/1.4152; E1R 0.2524/1.4152

### Episode duration
{
  "XRPUSDT": {
    "E1": {
      "count": 118,
      "median_hours": 0.75,
      "p75_hours": 2.5,
      "p90_hours": 10.55,
      "max_hours": 76.5
    },
    "E1R": {
      "count": 82,
      "median_hours": 1.625,
      "p75_hours": 3.9375,
      "p90_hours": 20.125,
      "max_hours": 80.5
    }
  },
  "ADAUSDT": {
    "E1": {
      "count": 100,
      "median_hours": 0.75,
      "p75_hours": 2.5,
      "p90_hours": 16.35,
      "max_hours": 75.5
    },
    "E1R": {
      "count": 64,
      "median_hours": 2.25,
      "p75_hours": 4.5,
      "p90_hours": 25.5,
      "max_hours": 76.25
    }
  },
  "DOGEUSDT": {
    "E1": {
      "count": 82,
      "median_hours": 1.125,
      "p75_hours": 3.1875,
      "p90_hours": 8.975,
      "max_hours": 66.5
    },
    "E1R": {
      "count": 58,
      "median_hours": 2.0,
      "p75_hours": 5.25,
      "p90_hours": 25.325,
      "max_hours": 66.75
    }
  }
}

### Gap / cluster structure during successful reclaims
Siehe reclaim_events[].gap_* in JSON.

### Verdict
1. q1_xrp_1206: Reclaim event: EMA_RECLAIM_SUCCESS
2. q2_extra_sl: Siehe additional_ignores_vs_e1 pro Coin.
3. q3_extra_tp: {'XRPUSDT': 0, 'ADAUSDT': 0, 'DOGEUSDT': 0}
4. q4_stickiness: {'XRPUSDT': (80.5, 76.5), 'ADAUSDT': (76.25, 75.5), 'DOGEUSDT': (66.75, 66.5)}
5. q5_cross_coin: Vergleich cross-Tabelle.
