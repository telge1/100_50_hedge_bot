# PHASE BT-D34 — Canonical 5m PD1 Window Cache and Fast Density Response

**Date:** 2026-10-26  
**Dashboard:** `spread_recovery_hedge_short_dev/dashboard`  
**Artifacts:** `results/phase_bt_d34_density_cache_performance_v1/`

---

## VERDICT

**`DENSITY_CACHE_SERVER_READY_BROWSER_NOT_VERIFIED`**

Server-side **versioned LRU cache**, **inflight dedup**, **`exact_origin_v1` compute mode** (skips `_build_clusters_by_dc`), **240 s** middleware timeout for `/live-charts/viewer/api/window`, and B-layer `response_mode` are implemented and measured in-process. **Warm cache hit ~2 ms** (262 pools). **Cold** remains **~171 s** (dominated by `_build_lifetimes` over 168 h context). Browser UI not re-verified.

---

## A. Server cache

| Item | Implementation |
|------|----------------|
| Module | `pd1_landscape_viewer_v1/window_response_cache_v1.py` |
| Contract | `bt_d34_pd1_window_response_cache_v1` |
| Service | `window_service_v1.get_landscape_window_response()` |
| Key | symbol, from/to UTC, TF, PD, `pipeline_line_id`, `config_hash`, `compute_mode`, builder version |
| Eviction | LRU, default **16** entries (`PD1_WINDOW_RESPONSE_CACHE_MAX`) |
| Disable | `PD1_WINDOW_RESPONSE_CACHE=0` |
| Dedup | `begin_inflight` / `wait_inflight` per digest |
| Publish | Only if `payload_cache_eligible()` (provenance complete, `remaining=false`, pools list) |
| Response meta | `response_cache: { hit, key_digest, compute_mode, contract }` |

---

## B. B-layer response cost

- **`compute_mode=exact_origin_v1`** (`response_mode` in POST body): **skips** `_build_clusters_by_dc` in the builder (not merely omitting JSON).
- **`pools[]` / segments** path unchanged (same `_build_lifetimes`, 168 h warmup).
- **Default** `full` mode: unchanged full API including `clusters_by_dc`.
- B-layer client sends `response_mode: "exact_origin_v1"` (`pd1_exact_origin_density_research_v1.js`).

Measured cold `timing_ms.build_clusters_by_dc`: **0.0** (exact_origin); total cold **~170.6 s** still dominated by **`build_lifetimes`**.

---

## C. Cold start and timeout

| Layer | Change |
|--------|--------|
| Middleware | `/live-charts/viewer/api/window` **240 s** (was 150 s) |
| Cold 8 h window | **170.67 s** in-process (under 240 s) |
| Warm hit | **~0.002 s** in-process |
| Client `fetchWindow` | No hard timeout (unchanged) |

---

## D. Historical parity

- Automated **pools** equality full vs `exact_origin_v1`: `test_bt_d34_window_parity_slow_v1.py` (run with `BT_D34_SLOW_PARITY=1`, Sep anchors 12:00 / 15:35 / 17:00 UTC).
- D34 run: **262 pools**, `compute_mode` provenance, clusters skipped — full byte parity deferred to slow pytest to limit CH load.

---

## E. Measurements

See `timing_comparison.json`, `deployment_validation.json`.

| Scenario | Result |
|----------|--------|
| Cold exact_origin (in-process) | **170.67 s** |
| Warm identical (in-process) | **0.002 s**, `response_cache.hit=true` |
| BT-D26 `/load` HTTP | **200**, **~28 ms** |
| Static JS (cache bust bt-d34) | **200**, **~3 ms** |

No invented speedup factors.

---

## F. Backtester / chart

- B-layer independent of backtester (unchanged).
- **GO TO + BT-D26:** immediate `syncBtD26ViewportRange` when catalog visible (`research_charts.js`) so stale `btd26-*` drawings drop when as-of has no candidate (UI fix, separate from density math).

---

## G. Deployment

- Dashboard restarted (**PID 2950678** after final `pkill`).
- Cache bust: **`phase6xt-bt-d34-density-cache-v1`** (template + `ASSET_V`).
- **Browser:** NOT_VERIFIED.

---

## H. Files touched

- `pd1_landscape_viewer_v1/window_response_cache_v1.py` (new)
- `pd1_landscape_viewer_v1/window_service_v1.py` (new)
- `pd1_landscape_viewer_v1/historical_window_v1.py` (`compute_mode`)
- `pd1_landscape_viewer_v1/api.py` (cache service)
- `app.py` (240 s window timeout)
- `pd1_exact_origin_density_research_v1.js`, `research_charts.js`, `research_charts.html`
- Tests: `test_bt_d34_window_cache_unit_v1.py`, `test_bt_d34_window_parity_slow_v1.py`

---

## Stop

Implementation and server-side verification complete. Run `BT_D34_SLOW_PARITY=1 pytest test_bt_d34_window_parity_slow_v1.py` when CH parity sign-off is required.
