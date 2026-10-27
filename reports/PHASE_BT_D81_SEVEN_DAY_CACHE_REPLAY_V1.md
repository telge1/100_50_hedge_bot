# BT-D81 — Seven-Day Multichunk Cache & EMA V3 Replay V1

**VERDICT:** `SEVEN_DAY_CACHE_REPLAY_PASS`

**Baseline:** BT-D80 `MULTICHUNK_CACHE_PASS` @ `5deb2a5e`  
**Git:** `5deb2a5e7e176e1ec082c40242bf2ca6cf03b477`  
**Window:** `2026-09-20T00:00:00Z` … `2026-09-26T23:55:00Z` (**2016** closes)

## A — BT-D80 fill verification (gate)

```json
{
  "ok": true,
  "discrepancy_resolution": "No Sep-24 12:30 fill in artifacts. pilot_id `selected_sep23_24` names the D73 window, not fill day. Sep-24 12:30 shows SHORT_OK in gate CSV but no pool signal/fill that bar. V1 classifier filled 2026-09-23T10:00:00Z (separate row in D73).",
  "modeled": {
    "fill_utc": "2026-09-23T12:30:00Z",
    "signal_utc": "2026-09-23T12:25:00Z",
    "entry": 0.09969,
    "sl": 0.10009378800000002,
    "tp": 0.09875,
    "order_id": "517aad3af63cf9e6248b",
    "fill_detail": "model_1_touch_fill_before_close_ema_review"
  },
  "ohlc_through_d80_window_end": {
    "bars": 714,
    "max_high": 0.09999,
    "min_low": 0.09101,
    "first_sl_trigger_bar": null,
    "first_tp_touch_bar": "2026-09-23T13:05:00Z",
    "isolated_runner_models_tp_exit": false,
    "position_open_at_horizon_per_runner": true
  },
  "meta": {
    "symbol": "DOGEUSDT",
    "source": "ClickHouseResearchCandleSource.get_candles(5m strict_complete_buckets)",
    "resample": "pd1_landscape_viewer_v1._resample_5m_from_candles",
    "data_bounds_lo": "2026-05-01T00:00:00Z",
    "data_bounds_hi": "2026-09-30T23:45:00Z",
    "requested_start": "2026-09-23T12:30:00Z",
    "requested_end": "2026-09-25T23:55:00Z",
    "bars_loaded": 714,
    "grid_steps": 714
  }
}
```

## Coverage / fingerprints

| Field | Value |
|-------|-------|
| Manifest entries | **2016** / **2016** |
| Pipeline | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |
| Config hash | `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9` |
| Chunks | 7 × 288 UTC days, span ≤ 72 h export cap |

## C — Parity evidence

- Boundary gates: **6** × 24 closes — **PASS**
- Interior noon samples: **7** closes — canonical vs multichunk
- Boundary canonical wall: **874.5** s; interior: **43.6** s
- Detail: `results/phase_bt_d81_seven_day_cache_replay_v1/parity_evidence.json`

## Build & replay timing

| Stage | Seconds |
|-------|--------:|
| Multichunk session build | **43.5852** |
| Cache build wall | **84.6403** |
| Donor reuse | **864** |
| New chunk writes | **1152** |
| Validation | **1.1401** |
| Replay (2 modes) | **23.5156** |

## D–E — EMA V3 replay (validated_cache)

| Metric | RESEARCH_READY |
|--------|---------------:|
| Pool candidates | **187** |
| EMA-V3 authorized | **8** |
| Blocked | **179** |
| Pending created | **8** |
| Pending EMA cancels | **1** |
| Modeled fills | **1** |
| Coverage gaps | **0** |

**DEFAULT_SHORT_PAUSED pending created:** **0**

**Candidate vs executable:** pool candidates include every baseline upper signal bar; only **SHORT_OK** at signal close creates Pending; UNCERTAIN/SHORT_NO blocks authorization (see `ema_v1_v3_gate_comparison.csv`).

### Trade ledger (V3)

```json
[
  {
    "event": "SIMULATED_FILL",
    "signal_utc": "2026-09-20T13:55:00Z",
    "fill_or_event_utc": "2026-09-20T14:00:00Z",
    "entry": "0.085",
    "exit": "",
    "sl": "0.08518002000000001",
    "tp": "0.08428",
    "pool_key": "0.085000000000000006|0.085010000000000002",
    "unresolved": "yes",
    "fees_slippage": "none_modeled_isolated_touch_fill"
  },
  {
    "event": "SIMULATED_SL",
    "signal_utc": "2026-09-20T13:55:00Z",
    "fill_or_event_utc": "2026-09-20T14:05:00Z",
    "entry": "",
    "exit": "0.08518002000000001",
    "sl": "",
    "tp": "",
    "pool_key": "",
    "unresolved": "no",
    "fees_slippage": "none_modeled_isolated_touch_fill"
  }
]
```

### Chart review events (sample)

```json
[
  {
    "decision_close_utc": "2026-09-20T14:00:00Z",
    "chart_review_reason": "MODELED_SIMULATED_FILL_v1",
    "pool_key": "0.085000000000000006|0.085010000000000002",
    "ema_v1": "",
    "ema_v3": ""
  },
  {
    "decision_close_utc": "2026-09-20T14:05:00Z",
    "chart_review_reason": "MODELED_SIMULATED_SL_v1",
    "pool_key": "",
    "ema_v1": "",
    "ema_v3": ""
  },
  {
    "decision_close_utc": "2026-09-20T14:00:00Z",
    "chart_review_reason": "MODELED_SIMULATED_FILL_v3",
    "pool_key": "0.085000000000000006|0.085010000000000002",
    "ema_v1": "",
    "ema_v3": ""
  },
  {
    "decision_close_utc": "2026-09-20T14:05:00Z",
    "chart_review_reason": "MODELED_SIMULATED_SL_v3",
    "pool_key": "",
    "ema_v1": "",
    "ema_v3": ""
  }
]
```

## Limitations before 30-day

- Parity is **bounded** (boundaries + noon samples), not full-window canonical per close.
- Isolated runner models **SL exit** only; **TP touches do not close** positions in current BT-D46 contract.
- Single continuous chrono replay — no per-day state reset.
- **Pause semantics:** RESEARCH_READY sets `READY` only at replay start; modeled SL on **2026-09-20T14:05:00Z** re-enters `SHORT_PAUSED` with no auto-unlock. That suppresses later Sep-23 activity seen in the narrower BT-D80 window (which did not include Sep-20).

**STOP.** No 30-day launch.

## Appendix — preflight

```json
{
  "closes": 2016,
  "chunk_plan": [
    {
      "chunk_index": 0,
      "closes": 288,
      "first": "2026-09-20T00:00:00Z",
      "last": "2026-09-20T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 1,
      "closes": 288,
      "first": "2026-09-21T00:00:00Z",
      "last": "2026-09-21T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 2,
      "closes": 288,
      "first": "2026-09-22T00:00:00Z",
      "last": "2026-09-22T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 3,
      "closes": 288,
      "first": "2026-09-23T00:00:00Z",
      "last": "2026-09-23T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 4,
      "closes": 288,
      "first": "2026-09-24T00:00:00Z",
      "last": "2026-09-24T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 5,
      "closes": 288,
      "first": "2026-09-25T00:00:00Z",
      "last": "2026-09-25T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 6,
      "closes": 288,
      "first": "2026-09-26T00:00:00Z",
      "last": "2026-09-26T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    }
  ],
  "d80_bundle_present": true,
  "fill_verification_ok": true
}
```
