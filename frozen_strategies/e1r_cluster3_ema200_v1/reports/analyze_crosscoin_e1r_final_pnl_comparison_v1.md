# Final Baseline vs E1R PnL comparison v1

### Code status
{
  "classification": {
    "A_production_baseline": "Live/Scanner-Pfad: Short-Entry-Scanner (find_short_entry_15m_v1) + bestehende Guards au\u00dferhalb dieses Reports. `with_guard` in floor_guard_signal_list_v1 = dokumentierte Baseline-Kohorte Jun\u2013Jul 2026 (Floor-Guard gefilterte Signale), nicht E1R.",
    "B_shadow_analysis_only": "E1R und alle analyze_*ema200* / crosscoin_e1* Skripte unter results/pool_scan/context_study_v1/ \u2014 reine Shadow-/Forensik-Logik, keine Integration in pool_pattern/machine oder Live-Bot gefunden.",
    "C_not_integrated": "E1R one-bar reclaim, E1, GAP3, Entry-Forensik, D4-Shadows \u2014 nur JSON/MD/CSV-Artefakte."
  },
  "e1r_in_executable_strategy_path": false,
  "note": "Repo grep: E1R nur in context_study_v1 Shadow-Skripten referenziert."
}

### Run manifest
{
  "repo": "/home/telgenbuescher/projects/pools+ob+delta_bot",
  "branch": "pools-ob-delta-bot",
  "head": "37fcc1c8bee0d25d830fed8df4f42c5b116255c8",
  "signal_source": "results/pool_scan/context_study_v1/floor_guard_signal_list_v1.json",
  "window": [
    "2026-06-01T00:00:00+00:00",
    "2026-07-31T23:59:59+00:00"
  ],
  "coins": [
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT"
  ],
  "baseline_files": [
    "results/pool_scan/context_study_v1/floor_guard_signal_list_v1.json",
    "results/pool_scan/context_study_v1/floor_guard_signal_list_v1.md",
    "results/pool_scan/context_study_v1/find_short_entry_15m_v1.py"
  ],
  "e1r_shadow_files": [
    "results/pool_scan/context_study_v1/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_ema200_cluster3_persistent_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_bull_regime_exit_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_ema200_cluster_transition_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_pool_ladder_forensics_v1.py"
  ],
  "e1r_canonical": "analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py"
}

### PnL methodology
{
  "position_size": "NOT_AVAILABLE \u2014 nur pro-Trade pnl_pct aus Signal-Backtest",
  "leverage": "NOT_AVAILABLE",
  "sl_tp": "Aus Scanner/Signal-JSON (stop/tp Felder); Entry = 15m Close",
  "fees": "NOT_AVAILABLE",
  "funding": "NOT_AVAILABLE",
  "slippage": "NOT_AVAILABLE",
  "compounding": false,
  "equity_definition": "Start 100 + chronologische Summe pnl_pct pro ausgef\u00fchrtem Trade (wie cumulative_pnl_pct in floor_guard_signal_list_v1.md)",
  "pnl_pct_source": "floor_guard_signal_list_v1.json field pnl_pct",
  "pnl_R": "r_multiple aus Signal-JSON wenn vorhanden",
  "net_pnl": "identisch pnl_pct (keine Fee-Logik)",
  "variants": {
    "A_raw": "pnl_pct ohne Fees",
    "B_net": "NOT_AVAILABLE"
  }
}

### Baseline parity
{
  "XRPUSDT": {
    "ok": true,
    "checks": {
      "baseline_trades": true,
      "baseline_sl": true,
      "e1r_trades": true,
      "e1r_sl": true,
      "e1r_tp_lost": true,
      "e1r_max_sl_streak": true
    },
    "expected_baseline": {
      "trades": 25,
      "sl": 8,
      "tp": 17
    },
    "expected_e1r": {
      "trades": 15,
      "sl": 2,
      "tp_first_lost": 4,
      "max_sl_streak": 1
    },
    "baseline_sl_tp": {
      "sl": 8,
      "tp": 17
    }
  },
  "ADAUSDT": {
    "ok": true,
    "checks": {
      "baseline_trades": true,
      "baseline_sl": true,
      "e1r_trades": true,
      "e1r_sl": true,
      "e1r_tp_lost": true,
      "e1r_max_sl_streak": true
    },
    "expected_baseline": {
      "trades": 18,
      "sl": 7,
      "tp": 11
    },
    "expected_e1r": {
      "trades": 11,
      "sl": 2,
      "tp_first_lost": 2,
      "max_sl_streak": 2
    },
    "baseline_sl_tp": {
      "sl": 7,
      "tp": 11
    }
  },
  "DOGEUSDT": {
    "ok": true,
    "checks": {
      "baseline_trades": true,
      "baseline_sl": true,
      "e1r_trades": true,
      "e1r_sl": true,
      "e1r_tp_lost": true,
      "e1r_max_sl_streak": true
    },
    "expected_baseline": {
      "trades": 28,
      "sl": 8,
      "tp": 19
    },
    "expected_e1r": {
      "trades": 22,
      "sl": 3,
      "tp_first_lost": 1,
      "max_sl_streak": 1
    },
    "baseline_sl_tp": {
      "sl": 8,
      "tp": 19
    }
  }
}

### E1R parity
{
  "XRPUSDT": {
    "ok": true,
    "checks": {
      "baseline_trades": true,
      "baseline_sl": true,
      "e1r_trades": true,
      "e1r_sl": true,
      "e1r_tp_lost": true,
      "e1r_max_sl_streak": true
    },
    "expected_baseline": {
      "trades": 25,
      "sl": 8,
      "tp": 17
    },
    "expected_e1r": {
      "trades": 15,
      "sl": 2,
      "tp_first_lost": 4,
      "max_sl_streak": 1
    },
    "baseline_sl_tp": {
      "sl": 8,
      "tp": 17
    }
  },
  "ADAUSDT": {
    "ok": true,
    "checks": {
      "baseline_trades": true,
      "baseline_sl": true,
      "e1r_trades": true,
      "e1r_sl": true,
      "e1r_tp_lost": true,
      "e1r_max_sl_streak": true
    },
    "expected_baseline": {
      "trades": 18,
      "sl": 7,
      "tp": 11
    },
    "expected_e1r": {
      "trades": 11,
      "sl": 2,
      "tp_first_lost": 2,
      "max_sl_streak": 2
    },
    "baseline_sl_tp": {
      "sl": 7,
      "tp": 11
    }
  },
  "DOGEUSDT": {
    "ok": true,
    "checks": {
      "baseline_trades": true,
      "baseline_sl": true,
      "e1r_trades": true,
      "e1r_sl": true,
      "e1r_tp_lost": true,
      "e1r_max_sl_streak": true
    },
    "expected_baseline": {
      "trades": 28,
      "sl": 8,
      "tp": 19
    },
    "expected_e1r": {
      "trades": 22,
      "sl": 3,
      "tp_first_lost": 1,
      "max_sl_streak": 1
    },
    "baseline_sl_tp": {
      "sl": 8,
      "tp": 19
    }
  }
}

### XRP PnL
| | Baseline | E1R |
|---|--:|--:|
| total_pnl_pct | 22.4895 | 22.7352 |
| expectancy_per_trade_pct | 0.89958 | 1.51568 |
| profit_factor | 4.5685 | 16.4577 |
| max_drawdown_pct | 3.9701 | 0.8254 |

### ADA PnL
| | Baseline | E1R |
|---|--:|--:|
| total_pnl_pct | 13.9321 | 14.7465 |
| expectancy_per_trade_pct | 0.774006 | 1.340591 |
| profit_factor | 3.1659 | 8.0165 |
| max_drawdown_pct | 3.3525 | 2.1017 |

### DOGE PnL
| | Baseline | E1R |
|---|--:|--:|
| total_pnl_pct | 25.0226 | 26.8071 |
| expectancy_per_trade_pct | 0.926763 | 1.276529 |
| profit_factor | 5.2289 | 16.3227 |
| max_drawdown_pct | 3.1578 | 1.0579 |

### Cross-coin PnL
| Metrik | Baseline | E1R |
|---|---:|---:|
| Trades gesamt | 71 | 48 |
| Gewinner | 47 | 40 |
| Verlierer | 23 | 7 |
| Gesamt-PnL % | 61.4442 | 64.2888 |
| Gesamt-PnL USDT | NOT_AVAILABLE | NOT_AVAILABLE |
| Profit Factor | 4.2943 | 13.0798 |
| Expectancy | 0.877774 | 1.367847 |
| Max Drawdown % | 10.2078 | 1.7465 |
| Max Verlustserie | 4 | 2 |
| Median MAE | 0.295 | 0.23535 |
| Median MFE | 1.3764 | 1.5292 |


### Drawdown
{
  "per_coin": {
    "XRPUSDT": {
      "baseline": {
        "max_drawdown_pct": 3.9701,
        "peak_timestamp": "2026-06-03T07:15:00+00:00",
        "trough_timestamp": "2026-06-14T23:30:00+00:00",
        "recovery_timestamp": "2026-06-18T00:30:00+00:00",
        "dd_duration_trades": 5
      },
      "e1r": {
        "max_drawdown_pct": 0.8254,
        "peak_timestamp": "2026-07-07T15:45:00+00:00",
        "trough_timestamp": "2026-07-09T07:45:00+00:00",
        "recovery_timestamp": "2026-07-13T15:15:00+00:00",
        "dd_duration_trades": 2
      }
    },
    "ADAUSDT": {
      "baseline": {
        "max_drawdown_pct": 3.3525,
        "peak_timestamp": "2026-07-13T14:30:00+00:00",
        "trough_timestamp": "2026-07-28T16:45:00+00:00",
        "recovery_timestamp": null,
        "dd_duration_trades": 5
      },
      "e1r": {
        "max_drawdown_pct": 2.1017,
        "peak_timestamp": "2026-07-13T14:30:00+00:00",
        "trough_timestamp": "2026-07-28T16:45:00+00:00",
        "recovery_timestamp": null,
        "dd_duration_trades": 3
      }
    },
    "DOGEUSDT": {
      "baseline": {
        "max_drawdown_pct": 3.1578,
        "peak_timestamp": "2026-06-03T21:45:00+00:00",
        "trough_timestamp": "2026-06-14T21:30:00+00:00",
        "recovery_timestamp": "2026-06-18T08:30:00+00:00",
        "dd_duration_trades": 5
      },
      "e1r": {
        "max_drawdown_pct": 1.0579,
        "peak_timestamp": "2026-07-13T14:45:00+00:00",
        "trough_timestamp": "2026-07-18T18:15:00+00:00",
        "recovery_timestamp": "2026-07-20T00:00:00+00:00",
        "dd_duration_trades": 4
      }
    }
  },
  "cross": {
    "baseline": {
      "max_drawdown_pct": 10.2078,
      "peak_timestamp": "2026-06-03T21:45:00+00:00",
      "trough_timestamp": "2026-06-14T23:30:00+00:00",
      "recovery_timestamp": "2026-06-19T01:45:00+00:00",
      "dd_duration_trades": 12
    },
    "e1r": {
      "max_drawdown_pct": 1.7465,
      "peak_timestamp": "2026-07-13T15:15:00+00:00",
      "trough_timestamp": "2026-07-18T18:15:00+00:00",
      "recovery_timestamp": "2026-07-27T17:00:00+00:00",
      "dd_duration_trades": 7
    }
  }
}

### Blocked winners
{
  "count": 7,
  "sum_pnl_pct": 10.4851,
  "avg_pnl_pct": 1.497871,
  "median_pnl_pct": 1.2312,
  "median_mae": 0.0434,
  "median_mfe": 1.2829
}

### Avoided losses
{
  "count": 16,
  "sum_pnl_pct": -13.3297,
  "avg_pnl_pct": -0.833106,
  "median_pnl_pct": -0.7237,
  "median_mae": 1.09005,
  "median_mfe": 0.39795
}

### BAD-4
[
  {
    "entry_open": "2026-06-11T19:15:00+00:00",
    "baseline_pnl_pct": -1.0471,
    "E1R_blocked": true,
    "avoided_loss_pct": -1.0471,
    "mae_pct": 1.3405,
    "mfe_pct": 1.5069,
    "final_outcome": "MOVE_041_THEN_SL"
  },
  {
    "entry_open": "2026-06-14T21:45:00+00:00",
    "baseline_pnl_pct": -1.5946,
    "E1R_blocked": true,
    "avoided_loss_pct": -1.5946,
    "mae_pct": 1.6959,
    "mfe_pct": 0.4454,
    "final_outcome": "MOVE_041_THEN_SL"
  },
  {
    "entry_open": "2026-06-14T22:30:00+00:00",
    "baseline_pnl_pct": -0.7333,
    "E1R_blocked": true,
    "avoided_loss_pct": -0.7333,
    "mae_pct": 1.0984,
    "mfe_pct": 0.5535,
    "final_outcome": "MOVE_041_THEN_SL"
  },
  {
    "entry_open": "2026-06-14T23:30:00+00:00",
    "baseline_pnl_pct": -0.5951,
    "E1R_blocked": true,
    "avoided_loss_pct": -0.5951,
    "mae_pct": 0.6784,
    "mfe_pct": 0.0254,
    "final_outcome": "SL_FIRST"
  }
]

### Equity curve
Baseline end equity 161.4442; E1R end equity 164.2888; CSV: analyze_crosscoin_e1r_final_equity_curve_v1.csv

### Freeze readiness
Ja, sofern Parität ok: reproduzierbar via floor_guard_signal_list_v1.json + analyze_crosscoin_e1_onebar_reclaim_shadow_v1.analyze_coin @ dokumentiertem HEAD.

### Freeze plan
{
  "proposed_tag": "e1r_cluster3_ema200_shadow_freeze_v1",
  "branch": "pools-ob-delta-bot",
  "head": "37fcc1c8bee0d25d830fed8df4f42c5b116255c8",
  "shadow_code": [
    "results/pool_scan/context_study_v1/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_ema200_cluster3_persistent_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_bull_regime_exit_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_ema200_cluster_transition_shadow_v1.py",
    "results/pool_scan/context_study_v1/analyze_xrp_pool_ladder_forensics_v1.py",
    "results/pool_scan/context_study_v1/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.py"
  ],
  "research_artifacts": [
    "results/pool_scan/context_study_v1/floor_guard_signal_list_v1.json",
    "results/pool_scan/context_study_v1/analyze_crosscoin_e1_onebar_reclaim_shadow_v1.json",
    "results/pool_scan/context_study_v1/analyze_crosscoin_e1r_final_pnl_comparison_v1.json"
  ],
  "production_code": "Nicht Teil dieses Freeze (E1R nicht integriert)",
  "config_constants": "REPORT_FROM/TO, PANE_FROM/TO in analyze_xrp_ema200_cluster3_persistent_shadow_v1.py; cluster gap 0.35% via price_clusters",
  "do_not_commit_automatically": true
}

