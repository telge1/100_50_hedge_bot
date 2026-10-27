# BT-D82 — TP/SL Lifecycle & SHORT_PAUSED Research Audit V1

**VERDICT:** `TRADE_ACCOUNTING_PASS`

**Baseline:** BT-D81 @ `f9d4a641` (tag `pre_bt_d82_stable_20261027`)  
**Git:** `f9d4a6415fdd3b69488f075832c5d08443069ab5`  
**Window:** `2026-09-20T00:00:00Z` … `2026-09-26T23:55:00Z` (**2016** closes, D81 cache read-only)

## B — Exit accounting audit

| Layer | TP exit | SL exit | Intrabar both-hit |
|-------|---------|---------|-------------------|
| **D46 / D7** (`simulate_short_exit`, `phase_bt_d46_integrated_short_backtester_v1/integrated_backtester_v1.py:_try_exit_on_bar`) | `low <= tp` | `high >= sl` | `pnl.intrabar_ambiguity_policy` |
| **Isolated research runner** (`isolated_chronological_runner_v1`) | **not modeled** | `high >= sl` only | not applied |
| **Gap** | isolated_chronological_runner_v1._try_exit_sl records SL only (high >= sl); does not evaluate TP or intrabar_ambiguity_policy. |

**5m convention:** bar key = **decision close** UTC; OHLC is the completed bucket for that key.  
**Fill ordering (isolated):** per bar — SL check on open position → pending fill → EMA cancel → D41 pending review → new signals.  
**Chunk/day boundaries:** replay is one continuous state machine; density cache is per-close only (no strategy reset).

Research extension reuses **`phase_bt_d7_config_driven_short_pnl_integration_v1/execution_v1.py:simulate_short_exit`** without modifying runners or BT-D46.

## D — Known case regressions

```json
[
  {
    "case": "sep20_fill_sl",
    "executable_isolated_baseline": "SL at 2026-09-20T14:05:00Z (SL checked after fill on subsequent bars in isolated runner)",
    "d46_research_exit": {
      "entry_bar_utc": "2026-09-20T14:00:00Z",
      "entry_price": 0.085,
      "sl_price": 0.08518002000000001,
      "tp_price": 0.08428,
      "exit_bar_utc": "2026-09-20T14:00:00Z",
      "exit_price": 0.08518002000000001,
      "exit_reason": "SL",
      "ambiguous_intrabar": false,
      "bars_held": 1,
      "gross_return_pct_short": -0.211788,
      "fees_slippage": "none_configured",
      "contract": "phase_bt_d7_config_driven_short_pnl_integration_v1/execution_v1.py:simulate_short_exit"
    },
    "ohlc_evidence": {
      "from_bar": "2026-09-20T14:00:00Z",
      "to_bar": "2026-09-20T14:30:00Z",
      "first_sl_only_touch_bar": "2026-09-20T14:00:00Z",
      "first_tp_only_touch_bar": null,
      "bars_with_both_touched": [],
      "note": "OHLC cannot prove intrabar order when both touched; use intrabar_ambiguity_policy."
    },
    "ordering_note": "D46 simulate_short_exit evaluates SL on the entry bar; isolated runner fills first then checks SL on later bars only \u2014 same-bar SL touch on fill bar yields D46@14:00 vs isolated@14:05.",
    "pass": true
  },
  {
    "case": "sep23_fill_tp_touch_3day_pilot",
    "executable_isolated_baseline": {
      "fills": 1,
      "sl_events": 0,
      "open_at_horizon": true
    },
    "hypothetical_d46_research_exit": {
      "entry_bar_utc": "2026-09-23T12:30:00Z",
      "entry_price": 0.09969,
      "sl_price": 0.10009378800000002,
      "tp_price": 0.09875,
      "exit_bar_utc": "2026-09-23T13:05:00Z",
      "exit_price": 0.09875,
      "exit_reason": "TP",
      "ambiguous_intrabar": false,
      "bars_held": 8,
      "gross_return_pct_short": 0.942923,
      "fees_slippage": "none_configured",
      "contract": "phase_bt_d7_config_driven_short_pnl_integration_v1/execution_v1.py:simulate_short_exit"
    },
    "ohlc_evidence": {
      "from_bar": "2026-09-23T12:30:00Z",
      "to_bar": "2026-09-25T23:55:00Z",
      "first_sl_only_touch_bar": null,
      "first_tp_only_touch_bar": "2026-09-23T13:05:00Z",
      "bars_with_both_touched": [],
      "note": "OHLC cannot prove intrabar order when both touched; use intrabar_ambiguity_policy."
    },
    "pass": true
  }
]
```

- **Sep-20:** executable isolated SL @ **14:05** (fill @ 14:00). D46 `simulate_short_exit` records SL @ **14:00** because it scans the entry bar before isolated runner’s post-fill SL pass — documented ordering delta, not a policy gap.  
- **Sep-23 (3-day pilot window):** executable baseline leaves position **open** (no TP in runner); D46 research path records **TP @ 13:05** (hypothetical vs executable).

## E — SHORT_PAUSED shadow (not executable)

Shadow rows: **15** (`shadow_pause_candidates.csv`).  
Criteria: `pause_state=SHORT_PAUSED`, pool present, `ema_decision=SHORT_OK`, `PAUSE_*` in gate reasons.

## F — Seven-day comparison (no density rebuild)

```json
{
  "verdict": "TRADE_ACCOUNTING_PASS",
  "replay_wall_s": 10.48,
  "pool_candidates": 187,
  "authorized": 8,
  "blocked": 179,
  "executable_fills": 1,
  "shadow_pause_pool_ema_ok": 15,
  "pause_state_final": "SHORT_PAUSED",
  "intrabar_policy": "no_trade_ambiguous",
  "chart_review_events": [
    {
      "decision_close_utc": "2026-09-20T14:00:00Z",
      "chart_review_reason": "EXECUTABLE_SIMULATED_FILL_v3",
      "pool_key": "0.085000000000000006|0.085010000000000002"
    },
    {
      "decision_close_utc": "2026-09-20T14:05:00Z",
      "chart_review_reason": "EXECUTABLE_SIMULATED_SL_v3",
      "pool_key": ""
    },
    {
      "decision_close_utc": "2026-09-20T14:00:00Z",
      "chart_review_reason": "D46_RESEARCH_SL",
      "pool_key": "0.085000000000000006|0.085010000000000002"
    },
    {
      "decision_close_utc": "2026-09-23T10:40:00Z",
      "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
      "pool_key": "0.099970000000000003|0.10006"
    },
    {
      "decision_close_utc": "2026-09-23T10:45:00Z",
      "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
      "pool_key": "0.099970000000000003|0.10005"
    },
    {
      "decision_close_utc": "2026-09-23T11:00:00Z",
      "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
      "pool_key": "0.10019|0.10019400000000001"
    },
    {
      "decision_close_utc": "2026-09-23T11:10:00Z",
      "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
      "pool_key": "0.10006|0.10007000000000001"
    },
    {
      "decision_close_utc": "2026-09-23T12:10:00Z",
      "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
      "pool_key": "0.099610000000000004|0.099690000000000001"
    }
  ]
}
```

**Executable chronology:** see `research_exit_ledger.json` / `.csv`.  
**Pause:** Sep-20 SL → `SHORT_PAUSED` for remainder; no auto-unlock.

### Chart-review timestamps (sample)

```json
[
  {
    "decision_close_utc": "2026-09-20T14:00:00Z",
    "chart_review_reason": "EXECUTABLE_SIMULATED_FILL_v3",
    "pool_key": "0.085000000000000006|0.085010000000000002"
  },
  {
    "decision_close_utc": "2026-09-20T14:05:00Z",
    "chart_review_reason": "EXECUTABLE_SIMULATED_SL_v3",
    "pool_key": ""
  },
  {
    "decision_close_utc": "2026-09-20T14:00:00Z",
    "chart_review_reason": "D46_RESEARCH_SL",
    "pool_key": "0.085000000000000006|0.085010000000000002"
  },
  {
    "decision_close_utc": "2026-09-23T10:40:00Z",
    "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
    "pool_key": "0.099970000000000003|0.10006"
  },
  {
    "decision_close_utc": "2026-09-23T10:45:00Z",
    "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
    "pool_key": "0.099970000000000003|0.10005"
  },
  {
    "decision_close_utc": "2026-09-23T11:00:00Z",
    "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
    "pool_key": "0.10019|0.10019400000000001"
  },
  {
    "decision_close_utc": "2026-09-23T11:10:00Z",
    "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
    "pool_key": "0.10006|0.10007000000000001"
  },
  {
    "decision_close_utc": "2026-09-23T12:10:00Z",
    "chart_review_reason": "SHADOW_NOT_EXECUTABLE",
    "pool_key": "0.099610000000000004|0.099690000000000001"
  }
]
```

## G — Before 30-day / profitability

- Executable PnL still **SL-only** in isolated runner; use **D46 research ledger** for TP/OPEN_AT_END semantics.  
- No fees, slippage, or leverage configured.  
- Shadow signals are **not** trades.  
- Intrabar ordering when **both** SL and TP touched: **`no_trade_ambiguous`** → no price exit.

## Appendix — D81 bundle check

```json
{
  "ok": true,
  "entry_count": 2016,
  "expected": 2016,
  "pipeline": "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1",
  "config_hash": "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9",
  "read_only": true
}
```

**STOP.**
