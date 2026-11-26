# MC-LOAD-P1 Trace Schema

**schema_version:** `mc_load_p1_v1`

## Root object (export)

| Field | Type | Notes |
|-------|------|--------|
| `schema_version` | string | `mc_load_p1_v1` |
| `load_id` | string | `mcl-<ts>-<seq>` |
| `symbol` / `timeframe` / `view` | string | nullable |
| `settings` | object | flags only, no secrets |
| `frontend_timings_ms` | object | event → ms since `start()`, monotonic |
| `requests` | array | HTTP rows |
| `backend_timings_ms` | object | from `_diag` when `X-MC-Load-Diag: 1` |
| `candle_count` | number | nullable |
| `first_candle_visible_ms` | number | render_proxy only |
| `loading_hidden_ms` | number | nullable |
| `render_verification` | string | `render_proxy` \| null |
| `errors` | array | |
| `diagnosis` | object | |

## Frontend events (monotonic, relative ms)

1. `symbol_selected`
2. `load_started`
3. `candle_request_started`
4. `candle_response_received`
5. `response_parse_completed`
6. `candle_normalization_completed`
7. `chart_setdata_started`
8. `chart_setdata_completed`
9. `first_candles_rendered` (`render_proxy`: `double_raf_after_setData`)
10. `loading_indicator_hidden`
11. `all_optional_layers_scheduled`

Additional probes: `chart_iframe_ready_wait_begin/end`, `ensure_pane_caught_up_begin/end`, `exact_origin_refresh_begin/end`.

## Request row

`id`, `method`, `endpoint`, `duration_ms`, `http_status`, `candle_count`, `cache`, `backend_timings_ms`, `coalesced`, `error`.

## Browser export

```javascript
__MC_LOAD_DIAG__.enable(); // or localStorage mc.load.diag=1
__MC_LOAD_DIAG__.start({ symbol: 'BNBUSDT', timeframe: '5m', view: 'single', ...mcLoadDiagSettings });
// … load symbol …
__MC_LOAD_DIAG__.stop({ note: 'manual' });
__MC_LOAD_DIAG__.exportJSON();
```

File: `MC_LOAD_P1_<load_id>.json` (download).

**STOP**