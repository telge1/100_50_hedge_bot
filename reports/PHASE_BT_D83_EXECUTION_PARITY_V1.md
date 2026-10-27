# BT-D83 — Isolated Short TP/SL Execution Parity V1

**VERDICT:** `EXECUTION_PARITY_PASS`

**Baseline:** BT-D82 @ `0818dead`  
**Git:** `0818dead6ac2c25c747c91d5c94c15e18c090a8a`  
**Window:** `2026-09-20T00:00:00Z` … `2026-09-26T23:55:00Z` (D81 cache read-only)

## B — Execution contract reconciliation

```json
{
  "bar_timestamp": "5m decision CLOSE UTC; OHLC is the completed bucket for that key.",
  "fill_model": "D45 MODEL_1 / causal_sell_limit_v2 via model_entry_decision (D45 execution_contract_v1).",
  "pending_active_from": "earliest_eligible_execution_utc on pending (typically next 5m open after signal close).",
  "isolated_per_bar_order": [
    "1) If open position: SL-only exit (high >= sl); no TP; no intrabar_ambiguity_policy.",
    "2) If pending and flat: attempt fill (MODEL_1).",
    "3) EMA cancel, D41 pending review, new pool signals / pending create."
  ],
  "d46_integrated_per_bar_order": [
    "1) If open position: full TP+SL+ambiguity exit (_try_exit_on_bar).",
    "2) If pending and flat: fill; then immediate full exit on same bar if still applicable.",
    "3) D41 pending review, pool-first signal logic, ..."
  ],
  "simulate_short_exit_timing": "Scans bars t >= entry_utc inclusive; D46 TP/SL/ambiguous rules per bar.",
  "sep20_sl_discrepancy": {
    "isolated_baseline_sl": "2026-09-20T14:05:00Z",
    "d46_simulate_short_exit_sl": "2026-09-20T14:00:00Z",
    "cause": "Isolated checks SL before fill on the fill bar (14:00), so the new position cannot stop out until 14:05. simulate_short_exit includes the entry bar in the exit scan, so an SL touch on 14:00 counts even though the fill also occurs on 14:00. D46 integrated calls _try_exit_on_bar immediately after fill on the same bar \u2014 compatible with D46 simulate_short_exit, not with current isolated SL-only pre-fill ordering."
  },
  "research_adapter_resolution": "Opt-in research_execution_runner_v1 keeps isolated fill path unchanged and applies D46 per-bar exit (TP+SL+no_trade_ambiguous) after fill on the fill bar, matching integrated_backtester_v1 post-fill exit call. Baseline isolated runner is untouched.",
  "conflict_status": "RESOLVED_BY_ADAPTER_TIMING \u2014 no change to production D46 or isolated files."
}
```

## D — Mandatory cases

```json
[
  {
    "case": "sep20_fill_sl_timing",
    "baseline_sl_utc": "2026-09-20T14:05:00Z",
    "research_exit": {
      "decision_close_utc": "2026-09-20T14:00:00Z",
      "position_id": "f9d818a966fdf715",
      "order_id": "b9e88ec7f1c6503c857d",
      "signal_close_utc": "2026-09-20T13:55:00Z",
      "entry_price": 0.085,
      "exit_price": 0.08518002000000001,
      "gross_return_pct_short": -0.211788,
      "ambiguous_intrabar": false,
      "event": "SIMULATED_SL"
    },
    "pass": true
  },
  {
    "case": "sep23_three_day_tp_research",
    "baseline_open_at_end": true,
    "research_tp": {
      "decision_close_utc": "2026-09-23T13:05:00Z",
      "position_id": "4e12874066344424",
      "order_id": "517aad3af63cf9e6248b",
      "signal_close_utc": "2026-09-23T12:25:00Z",
      "entry_price": 0.09969,
      "exit_price": 0.09875,
      "gross_return_pct_short": 0.942923,
      "ambiguous_intrabar": false,
      "event": "SIMULATED_TP"
    },
    "pass": true
  },
  {
    "case": "both_hit_same_candle",
    "policy": "no_trade_ambiguous",
    "result": {
      "reason": "AMBIGUOUS_INTRABAR",
      "price": null,
      "ambiguous": true
    },
    "pass": true
  },
  {
    "case": "state_across_midnight",
    "pause_state_first_bar_sep21": "SHORT_PAUSED",
    "pass": true
  },
  {
    "case": "seven_day_summary",
    "baseline": {
      "fills": 1,
      "exit_events": {
        "SIMULATED_SL": 1
      },
      "open_at_end": false,
      "pause_final": "SHORT_PAUSED"
    },
    "research": {
      "fills": 1,
      "exit_events": {
        "SIMULATED_SL": 1
      },
      "open_at_end": false,
      "pause_final": "SHORT_PAUSED"
    },
    "pass": true
  }
]
```

**Sep-20:** baseline SL `2026-09-20T14:05:00Z` vs research exit `{'decision_close_utc': '2026-09-20T14:00:00Z', 'position_id': 'f9d818a966fdf715', 'order_id': 'b9e88ec7f1c6503c857d', 'signal_close_utc': '2026-09-20T13:55:00Z', 'entry_price': 0.085, 'exit_price': 0.08518002000000001, 'gross_return_pct_short': -0.211788, 'ambiguous_intrabar': False, 'event': 'SIMULATED_SL'}`.

**Sep-23 (3-day):** research TP `{'decision_close_utc': '2026-09-23T13:05:00Z', 'position_id': '4e12874066344424', 'order_id': '517aad3af63cf9e6248b', 'signal_close_utc': '2026-09-23T12:25:00Z', 'entry_price': 0.09969, 'exit_price': 0.09875, 'gross_return_pct_short': 0.942923, 'ambiguous_intrabar': False, 'event': 'SIMULATED_TP'}`; baseline still open `True`.

## E — Seven-day comparison

```json
{
  "baseline": {
    "fills": 1,
    "exit_events": {
      "SIMULATED_SL": 1
    },
    "open_at_end": false,
    "pause_final": "SHORT_PAUSED"
  },
  "research": {
    "fills": 1,
    "exit_events": {
      "SIMULATED_SL": 1
    },
    "open_at_end": false,
    "pause_final": "SHORT_PAUSED"
  },
  "baseline_positions": [
    {
      "event": "SIMULATED_FILL",
      "decision_close_utc": "2026-09-20T14:00:00Z",
      "position_id": "f9d818a966fdf715",
      "order_id": "b9e88ec7f1c6503c857d",
      "entry_price": 0.085,
      "sl_price": 0.08518002000000001,
      "tp_price": 0.08428,
      "signal_close_utc": "2026-09-20T13:55:00Z",
      "pool_key": "0.085000000000000006|0.085010000000000002"
    },
    {
      "event": "SIMULATED_SL",
      "decision_close_utc": "2026-09-20T14:05:00Z",
      "position_id": "f9d818a966fdf715",
      "order_id": "b9e88ec7f1c6503c857d",
      "exit_price": 0.08518002000000001,
      "signal_close_utc": "2026-09-20T13:55:00Z"
    }
  ],
  "research_positions": [
    {
      "event": "SIMULATED_FILL",
      "decision_close_utc": "2026-09-20T14:00:00Z",
      "position_id": "f9d818a966fdf715",
      "order_id": "b9e88ec7f1c6503c857d",
      "entry_price": 0.085,
      "sl_price": 0.08518002000000001,
      "tp_price": 0.08428,
      "signal_close_utc": "2026-09-20T13:55:00Z",
      "pool_key": "0.085000000000000006|0.085010000000000002"
    },
    {
      "decision_close_utc": "2026-09-20T14:00:00Z",
      "position_id": "f9d818a966fdf715",
      "order_id": "b9e88ec7f1c6503c857d",
      "signal_close_utc": "2026-09-20T13:55:00Z",
      "entry_price": 0.085,
      "exit_price": 0.08518002000000001,
      "gross_return_pct_short": -0.211788,
      "ambiguous_intrabar": false,
      "event": "SIMULATED_SL"
    }
  ],
  "shadow_pause_count": 15,
  "timing_s": {
    "baseline": 10.3811,
    "research": 10.4231
  }
}
```

**Shadow during pause (baseline replay):** `15` — not in PnL.

## F — 30-day profitability readiness

Technically meaningful **for research ledger** (TP/SL/ambiguous on opt-in runner). Executable baseline chronology remains SL-only until a deliberate runner change is approved. Density/cache path validated at 2016 closes.

**STOP.**
