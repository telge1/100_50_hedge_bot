# BT-D85 — July 2026 EMA V3 No Post-SL Pause Research V1

**VERDICT:** `JULY_MONTH_RESEARCH_PASS`

**Baselines:** BT-D83 f5fe267f; BT-D84 28cbc5c9 de5098b9; BT-D83 `EXECUTION_PARITY_PASS`  
**Git HEAD:** `de5098b951c7d5ccc94a59ca7823f90c663e3e8f`  
**Window:** `2026-07-01T00:00:00Z` … `2026-07-31T23:55:00Z` (**8928** closes)  
**Required causal coverage through:** `2026-08-01T00:00:00Z`

## Research mode (BT-D84 reuse)

| Setting | Value |
|---------|-------|
| `post_sl_pause_enabled` | **false** |
| Pause contract | `research_pause_v1_post_sl_opt_out` |
| Execution | BT-D83 D46 research runner (TP/SL, post-fill same-bar, ambiguous policy) |
| Production | `SHORT_PAUSED` after SL **unchanged** |

## Provenance & cache

| Field | Value |
|-------|-------|
| Manifest entries | **8928** / **8928** |
| Pipeline | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |
| Config hash | `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9` |
| Donor bundles (provenance only) | D81 → D80 → D73 (no July overlap) |
| Bundle | `results/phase_bt_d85_july_2026_no_pause_research_v1/cache_bundle/` |

## Parity evidence (bounded)

- Day-boundary gates: **30** × 24 closes — **PASS**
- Interior noon: **31** closes
- Boundary wall: **7032.2** s; interior: **309.1** s
- File: `results/phase_bt_d85_july_2026_no_pause_research_v1/parity_evidence.json`

## Wall-clock stages

| Stage | Seconds |
|-------|--------:|
| Preflight canonical spot | **25.6862** |
| Multichunk session build | **303.6522** |
| Parity (total) | **7341.2425** |
| Cache build | **968.2092** |
| Cache validation | **8.7835** |
| EMA gate (first replay) | **0.0161** |
| Dual replay wall | **148.8168** |
| Donor reuse | **0** |
| New chunk writes | **8928** |

## July performance — no post-SL pause (primary)

| Metric | Value |
|--------|------:|
| Pool candidates | **959** |
| EMA V3 authorized | **85** |
| EMA blocked | **846** |
| Pending created | **81** |
| Pending EMA cancels | **2** |
| Modeled fills | **25** |
| TP / SL / ambiguous | **10** / **15** / **0** |
| Still open | **0** |
| Gross win rate (resolved) | **40.0%** |
| Avg win / avg loss (gross %) | **0.760811** / **-0.220115** |
| Cumulative gross % | **4.306393** |
| Max win / loss streak | **3** / **5** |
| Max drawdown (unit stack) | **1.160235** |
| Final pause state | **READY** |
| Coverage gaps | **0** |

**Not net profitability** — no fees, slippage, or execution uncertainty modeled.

## SHORT_PAUSED reference (separate replay)

| Metric | Value |
|--------|------:|
| Pending created | **5** |
| Modeled fills | **2** |
| TP / SL | **1** / **1** |
| Final pause state | **SHORT_PAUSED** |

### Trade ledger

Full JSON: `results/phase_bt_d85_july_2026_no_pause_research_v1/trade_ledger_no_pause.json`

```json
[
  {
    "position_id": "61bde5d29769f835",
    "signal_close_utc": "2026-07-01T07:15:00Z",
    "fill_time_utc": "2026-07-01T07:20:00Z",
    "entry_price": 0.07144,
    "sl_price": 0.071616948,
    "tp_price": 0.07104,
    "exit_time_utc": "2026-07-01T08:20:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.07104,
    "gross_return_pct_short": 0.55991,
    "ambiguous_intrabar": false,
    "pool_key": "0.071440000000000003|0.071473999999999996",
    "order_id": "abfc5e112cadb819e526"
  },
  {
    "position_id": "31bc75cf636723eb",
    "signal_close_utc": "2026-07-07T09:05:00Z",
    "fill_time_utc": "2026-07-07T09:10:00Z",
    "entry_price": 0.07497,
    "sl_price": 0.07512996000000001,
    "tp_price": 0.0743,
    "exit_time_utc": "2026-07-07T10:00:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.07512996000000001,
    "gross_return_pct_short": -0.213365,
    "ambiguous_intrabar": false,
    "pool_key": "0.074969999999999995|0.074980000000000005",
    "order_id": "35712705549e7baac249"
  },
  {
    "position_id": "a77ca533ace97011",
    "signal_close_utc": "2026-07-07T19:40:00Z",
    "fill_time_utc": "2026-07-07T19:45:00Z",
    "entry_price": 0.07479,
    "sl_price": 0.07498767599999999,
    "tp_price": 0.07371,
    "exit_time_utc": "2026-07-07T22:00:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.07371,
    "gross_return_pct_short": 1.444043,
    "ambiguous_intrabar": false,
    "pool_key": "0.074789999999999995|0.074837999999999988",
    "order_id": "4f8d2f9f4c2e1493c91e"
  },
  {
    "position_id": "c072ddbcdfaabea7",
    "signal_close_utc": "2026-07-08T00:30:00Z",
    "fill_time_utc": "2026-07-08T00:35:00Z",
    "entry_price": 0.07434,
    "sl_price": 0.07450471199999999,
    "tp_price": 0.07353,
    "exit_time_utc": "2026-07-08T01:35:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.07353,
    "gross_return_pct_short": 1.089588,
    "ambiguous_intrabar": false,
    "pool_key": "0.074340000000000003|0.074355999999999992",
    "order_id": "f033c7195034585114b7"
  },
  {
    "position_id": "3eb0c5784e945b7b",
    "signal_close_utc": "2026-07-08T04:25:00Z",
    "fill_time_utc": "2026-07-08T04:30:00Z",
    "entry_price": 0.07265,
    "sl_price": 0.07280532,
    "tp_price": 0.07228,
    "exit_time_utc": "2026-07-08T06:10:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.07228,
    "gross_return_pct_short": 0.509291,
    "ambiguous_intrabar": false,
    "pool_key": "0.072650000000000006|0.072660000000000002",
    "order_id": "e3814d2a734336335c2e"
  },
  {
    "position_id": "60b23abd90287328",
    "signal_close_utc": "2026-07-08T07:15:00Z",
    "fill_time_utc": "2026-07-08T07:20:00Z",
    "entry_price": 0.0722,
    "sl_price": 0.072366444,
    "tp_price": 0.07163,
    "exit_time_utc": "2026-07-08T07:30:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.072366444,
    "gross_return_pct_short": -0.230532,
    "ambiguous_intrabar": false,
    "pool_key": "0.0722|0.072222000000000008",
    "order_id": "dba00fdf1f5dada81c44"
  },
  {
    "position_id": "e88fa3b1224eba8e",
    "signal_close_utc": "2026-07-09T03:00:00Z",
    "fill_time_utc": "2026-07-09T03:05:00Z",
    "entry_price": 0.07218,
    "sl_price": 0.07235241599999999,
    "tp_price": 0.07117,
    "exit_time_utc": "2026-07-09T03:30:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.07235241599999999,
    "gross_return_pct_short": -0.238869,
    "ambiguous_intrabar": false,
    "pool_key": "0.072179999999999994|0.072207999999999994",
    "order_id": "b566592c4d592815cc4e"
  },
  {
    "position_id": "ed11586b3e875ffb",
    "signal_close_utc": "2026-07-12T01:10:00Z",
    "fill_time_utc": "2026-07-12T01:15:00Z",
    "entry_price": 0.07279,
    "sl_price": 0.072979668,
    "tp_price": 0.07214,
    "exit_time_utc": "2026-07-12T01:15:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.072979668,
    "gross_return_pct_short": -0.260569,
    "ambiguous_intrabar": false,
    "pool_key": "0.072789999999999994|0.072833999999999996",
    "order_id": "5bd7d99db289ee32073f"
  },
  {
    "position_id": "f41e3c419b7473f0",
    "signal_close_utc": "2026-07-12T06:55:00Z",
    "fill_time_utc": "2026-07-12T07:00:00Z",
    "entry_price": 0.07285,
    "sl_price": 0.07301574000000001,
    "tp_price": 0.07214,
    "exit_time_utc": "2026-07-12T07:20:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.07301574000000001,
    "gross_return_pct_short": -0.227509,
    "ambiguous_intrabar": false,
    "pool_key": "0.072849999999999998|0.072870000000000004",
    "order_id": "08ea6bfe975262a0ed6c"
  },
  {
    "position_id": "9b6b4a3224b3f1ff",
    "signal_close_utc": "2026-07-12T23:00:00Z",
    "fill_time_utc": "2026-07-12T23:05:00Z",
    "entry_price": 0.07271,
    "sl_price": 0.072857424,
    "tp_price": 0.072208,
    "exit_time_utc": "2026-07-13T00:00:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.072857424,
    "gross_return_pct_short": -0.202756,
    "ambiguous_intrabar": false,
    "pool_key": "0.072709999999999997|0.072711999999999999",
    "order_id": "7ecf4e9017aa03df89bb"
  },
  {
    "position_id": "d4f9f1f874a424db",
    "signal_close_utc": "2026-07-13T02:30:00Z",
    "fill_time_utc": "2026-07-13T02:35:00Z",
    "entry_price": 0.07282,
    "sl_price": 0.07302776400000001,
    "tp_price": 0.072208,
    "exit_time_utc": "2026-07-13T03:05:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.072208,
    "gross_return_pct_short": 0.840428,
    "ambiguous_intrabar": false,
    "pool_key": "0.072819999999999996|0.072882000000000002",
    "order_id": "24967235d875be95d4ff"
  },
  {
    "position_id": "5583dfc5c4655309",
    "signal_close_utc": "2026-07-13T04:20:00Z",
    "fill_time_utc": "2026-07-13T04:25:00Z",
    "entry_price": 0.07216,
    "sl_price": 0.072312336,
    "tp_price": 0.07117,
    "exit_time_utc": "2026-07-13T05:10:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.072312336,
    "gross_return_pct_short": -0.211109,
    "ambiguous_intrabar": false,
    "pool_key": "0.072160000000000002|0.07216800000000001",
    "order_id": "5b80418c0e907d6a492b"
  },
  {
    "position_id": "78a1e81b6c7b8322",
    "signal_close_utc": "2026-07-13T13:25:00Z",
    "fill_time_utc": "2026-07-13T13:30:00Z",
    "entry_price": 0.072164,
    "sl_price": 0.072318348,
    "tp_price": 0.071794,
    "exit_time_utc": "2026-07-13T13:35:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.072318348,
    "gross_return_pct_short": -0.213885,
    "ambiguous_intrabar": false,
    "pool_key": "0.072164000000000006|0.072174000000000002",
    "order_id": "96f4c5e5f07d9bb3b008"
  },
  {
    "position_id": "51ad02d895612697",
    "signal_close_utc": "2026-07-15T11:45:00Z",
    "fill_time_utc": "2026-07-15T11:50:00Z",
    "entry_price": 0.07378,
    "sl_price": 0.073929564,
    "tp_price": 0.07307,
    "exit_time_utc": "2026-07-15T12:30:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.073929564,
    "gross_return_pct_short": -0.202716,
    "ambiguous_intrabar": false,
    "pool_key": "0.073779999999999998|0.073782",
    "order_id": "35139128fc4476b3e515"
  },
  {
    "position_id": "b22fc9c6e91aee3b",
    "signal_close_utc": "2026-07-17T09:15:00Z",
    "fill_time_utc": "2026-07-17T09:20:00Z",
    "entry_price": 0.07157799999999999,
    "sl_price": 0.071725164,
    "tp_price": 0.0712,
    "exit_time_utc": "2026-07-17T10:00:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.071725164,
    "gross_return_pct_short": -0.205599,
    "ambiguous_intrabar": false,
    "pool_key": "0.071577999999999989|0.071581999999999993",
    "order_id": "25dd7ad62bde86a34327"
  },
  {
    "position_id": "8f5248718ded07f4",
    "signal_close_utc": "2026-07-23T06:20:00Z",
    "fill_time_utc": "2026-07-23T06:25:00Z",
    "entry_price": 0.07248,
    "sl_price": 0.072642996,
    "tp_price": 0.07206199999999999,
    "exit_time_utc": "2026-07-23T07:10:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.07206199999999999,
    "gross_return_pct_short": 0.576711,
    "ambiguous_intrabar": false,
    "pool_key": "0.072480000000000003|0.072498000000000007",
    "order_id": "d05221020a26cd0d74b3"
  },
  {
    "position_id": "30e48c34f9568b8b",
    "signal_close_utc": "2026-07-23T14:30:00Z",
    "fill_time_utc": "2026-07-23T14:35:00Z",
    "entry_price": 0.07057,
    "sl_price": 0.070735188,
    "tp_price": 0.07003,
    "exit_time_utc": "2026-07-23T15:00:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.07003,
    "gross_return_pct_short": 0.765198,
    "ambiguous_intrabar": false,
    "pool_key": "0.070569999999999994|0.070594000000000004",
    "order_id": "6a48e4f961295e7bfa1f"
  },
  {
    "position_id": "53b3e5b924712b4c",
    "signal_close_utc": "2026-07-23T16:30:00Z",
    "fill_time_utc": "2026-07-23T16:35:00Z",
    "entry_price": 0.06971,
    "sl_price": 0.06987346800000001,
    "tp_price": 0.0692,
    "exit_time_utc": "2026-07-23T18:05:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.0692,
    "gross_return_pct_short": 0.731602,
    "ambiguous_intrabar": false,
    "pool_key": "0.069709999999999994|0.069734000000000004",
    "order_id": "0e5759b2ef071789db25"
  },
  {
    "position_id": "57ad7b3b60f80157",
    "signal_close_utc": "2026-07-24T00:50:00Z",
    "fill_time_utc": "2026-07-24T00:55:00Z",
    "entry_price": 0.06916,
    "sl_price": 0.06930433200000001,
    "tp_price": 0.0687,
    "exit_time_utc": "2026-07-24T01:05:00Z",
    "exit_reason": "SIMULATED_SL",
    "exit_price": 0.06930433200000001,
    "gross_return_pct_short": -0.208693,
    "ambiguous_intrabar": false,
    "pool_key": "0.069159999999999999|0.069166000000000005",
    "order_id": "8f0d613d6c0cf01fe24b"
  },
  {
    "position_id": "3bc77e6407de5107",
    "signal_close_utc": "2026-07-24T12:00:00Z",
    "fill_time_utc": "2026-07-24T12:05:00Z",
    "entry_price": 0.06946,
    "sl_price": 0.06964701599999999,
    "tp_price": 0.069106,
    "exit_time_utc": "2026-07-24T12:45:00Z",
    "exit_reason": "SIMULATED_TP",
    "exit_price": 0.069106,
    "gross_return_pct_short": 0.509646,
    "ambiguous_intrabar": false,
    "pool_key": "0.069459999999999994|0.069507999999999986",
    "order_id": "6aae35aaf9d704c9a4c7"
  }
]
```

*(truncated to 20 trades in report; full ledger in results)*

### Chart review timestamps

Full JSON: `results/phase_bt_d85_july_2026_no_pause_research_v1/chart_review_timestamps.json`

```json
[
  {
    "utc": "2026-07-01T07:15:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.071440000000000003|0.071473999999999996"
  },
  {
    "utc": "2026-07-01T07:15:00Z",
    "kind": "PENDING_CREATE",
    "detail": "abfc5e112cadb819e526"
  },
  {
    "utc": "2026-07-01T07:20:00Z",
    "kind": "PENDING_FILLED",
    "detail": "abfc5e112cadb819e526"
  },
  {
    "utc": "2026-07-01T07:20:00Z",
    "kind": "SIMULATED_FILL",
    "detail": "0.071440000000000003|0.071473999999999996"
  },
  {
    "utc": "2026-07-01T08:20:00Z",
    "kind": "SIMULATED_TP",
    "detail": "0.07104"
  },
  {
    "utc": "2026-07-05T06:20:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.075875999999999999|0.075882000000000005"
  },
  {
    "utc": "2026-07-05T06:20:00Z",
    "kind": "PENDING_CREATE",
    "detail": "fc6c745878dd5708ab24"
  },
  {
    "utc": "2026-07-05T06:25:00Z",
    "kind": "PENDING_CANCEL",
    "detail": "fc6c745878dd5708ab24"
  },
  {
    "utc": "2026-07-06T06:45:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.076712000000000002|0.076727999999999991"
  },
  {
    "utc": "2026-07-06T06:45:00Z",
    "kind": "PENDING_CREATE",
    "detail": "da73d40ad682a4a2e055"
  },
  {
    "utc": "2026-07-07T08:25:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.074980000000000005|0.075024000000000007"
  },
  {
    "utc": "2026-07-07T08:25:00Z",
    "kind": "PENDING_CREATE",
    "detail": "998f9510ebbe490b94bc"
  },
  {
    "utc": "2026-07-07T09:05:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.074969999999999995|0.074980000000000005"
  },
  {
    "utc": "2026-07-07T09:05:00Z",
    "kind": "PENDING_CREATE",
    "detail": "35712705549e7baac249"
  },
  {
    "utc": "2026-07-07T09:10:00Z",
    "kind": "PENDING_FILLED",
    "detail": "35712705549e7baac249"
  },
  {
    "utc": "2026-07-07T09:10:00Z",
    "kind": "SIMULATED_FILL",
    "detail": "0.074969999999999995|0.074980000000000005"
  },
  {
    "utc": "2026-07-07T10:00:00Z",
    "kind": "SIMULATED_SL",
    "detail": "0.07512996000000001"
  },
  {
    "utc": "2026-07-07T19:40:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.074789999999999995|0.074837999999999988"
  },
  {
    "utc": "2026-07-07T19:40:00Z",
    "kind": "PENDING_CREATE",
    "detail": "4f8d2f9f4c2e1493c91e"
  },
  {
    "utc": "2026-07-07T19:45:00Z",
    "kind": "PENDING_FILLED",
    "detail": "4f8d2f9f4c2e1493c91e"
  },
  {
    "utc": "2026-07-07T19:45:00Z",
    "kind": "SIMULATED_FILL",
    "detail": "0.074789999999999995|0.074837999999999988"
  },
  {
    "utc": "2026-07-07T22:00:00Z",
    "kind": "SIMULATED_TP",
    "detail": "0.07371"
  },
  {
    "utc": "2026-07-07T22:20:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.07417|0.074200000000000002"
  },
  {
    "utc": "2026-07-07T22:20:00Z",
    "kind": "PENDING_CREATE",
    "detail": "e8a42e420e331f96f690"
  },
  {
    "utc": "2026-07-08T00:30:00Z",
    "kind": "EMA_V3_AUTHORIZED",
    "pool_key": "0.074340000000000003|0.074355999999999992"
  }
]
```

## Safety

Research-only. No production activation, no CH writes/imports, no dashboard or BT-D46 changes.

**STOP** after July research.

## Appendix — preflight

```json
{
  "baseline_commits": "BT-D83 f5fe267f; BT-D84 28cbc5c9 de5098b9",
  "git_head": "de5098b951c7d5ccc94a59ca7823f90c663e3e8f",
  "closes": 8928,
  "chunk_plan_days": 31,
  "disk_free_gib": 190.77,
  "d81_bundle_present": true,
  "d81_donor_closes": 2016,
  "d80_bundle_present": true,
  "d73_bundle_present": true,
  "ohlc_meta": {
    "symbol": "DOGEUSDT",
    "source": "ClickHouseResearchCandleSource.get_candles(5m strict_complete_buckets)",
    "resample": "pd1_landscape_viewer_v1._resample_5m_from_candles",
    "data_bounds_lo": "2026-05-01T00:00:00Z",
    "data_bounds_hi": "2026-09-30T23:45:00Z",
    "requested_start": "2026-05-30T00:00:00Z",
    "requested_end": "2026-07-31T23:55:00Z",
    "bars_loaded": 18144,
    "grid_steps": 18144,
    "window_closes_missing": 0,
    "window_ohlc_ok": true
  },
  "research_post_sl_pause_enabled": false,
  "execution": "research_execution_runner_v1_d46_exit_parity",
  "density_gate": {
    "ok": true,
    "expected_closes": 8928,
    "actual_closes": 8928,
    "published_data_lo_utc": "2026-05-01T00:00:00Z",
    "published_data_hi_utc": "2026-09-30T23:45:00Z",
    "density_scope_violations": [],
    "density_scope_violation_count": 0,
    "last_supported_close_rule": "causal visible_to must be <= published_data_hi; typically limits last September close to 2026-09-30T23:40:00Z (8637 closes)"
  },
  "canonical_density_spot": {
    "ok": true,
    "samples": [
      {
        "decision_close_utc": "2026-07-01T00:00:00Z",
        "ok": true,
        "band_count": 364,
        "wall_s": 8.459
      },
      {
        "decision_close_utc": "2026-07-16T12:00:00Z",
        "ok": true,
        "band_count": 368,
        "wall_s": 8.856
      },
      {
        "decision_close_utc": "2026-07-31T23:55:00Z",
        "ok": true,
        "band_count": 342,
        "wall_s": 8.371
      }
    ],
    "last_close_visible_to_utc": "2026-08-01T00:00:00Z",
    "required_visible_to_coverage_utc": "2026-08-01T00:00:00Z",
    "visible_to_coverage_ok": true,
    "wall_s": 25.6862
  }
}
```
