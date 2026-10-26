# PHASE BT-D37 — Live Window Request Stall and Batch-Path Convergence

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Pool repo:** `/home/telgenbuescher/projects/pools+ob+delta_bot`

---

## VERDICT

**`LIVE_WINDOW_PARTIAL`**

| Item | Status |
|------|--------|
| Root cause (HTTP ~200s vs CLI ~5s) | **Identified** — legacy non-batch lifetime path |
| Code fixes (inflight + trace + provenance) | **Applied** |
| Controlled live HTTP JSON (cold + warm) | **Not obtained** — slots saturated by live traffic |
| Post-fix restart | **Yes** — PID **2979403** (12:43+ local) |
| Browser density | **NOT_VERIFIED** |

---

## 1. Live process

| Field | Value |
|-------|--------|
| PID (end of phase) | `2979403` |
| CWD | `.../spread_recovery_hedge_short_dev/dashboard` |
| PYTHONPATH | `spread_recovery_hedge_short_dev` + `dashboard` + `pools+ob+delta_bot` |
| Batch env on PID | **unset** (no `PD1_LANDSCAPE_BATCH_READER` / `HISTORICAL_LMV2_BATCH_READER`) |

---

## 2. HTTP vs CLI — proven divergence

### Canonical 8h payload (Sep-11 2026, 5m PD1, `exact_origin_v1`)

| Path | Wall | `phase5s_prefill` | `build_lifetimes` | `total` | `phase5s_batch_reader_active` |
|------|------|-------------------|-------------------|---------|-------------------------------|
| CLI batch **on** (default) | ~5.0 s | ~4500 ms | ~290 ms | ~5000 ms | **true** |
| CLI batch **off** (`PD1_LANDSCAPE_BATCH_READER=0`) | **~181 s** | 0 | **~180887 ms** | **~181039 ms** | **false** |
| Live HTTP (pre-D37 logs, middleware) | **~165–215 s** | (body not captured) | matches legacy shape in BT-D36A JSON | — | **false** in saved `/tmp/window_d36.json` |

**Conclusion:** Live multi-minute window times are **not** a separate algorithm; they match the **legacy per-close loader** when Phase-5S batch prefill is **not active**. Fast CLI path is batch prefill + short `build_lifetimes`.

**Not caused by:** `response_mode` alone — CLI `full` vs `exact_origin_v1` both ~5 s with batch on.

**Parallel cold builds (3 different digests):** ~12–13 s each (batch on), not 200 s.

---

## 3. HTTP 429 and concurrency

| Mechanism | Location | Behavior |
|-----------|----------|----------|
| `MAX_CONCURRENT_REQUESTS = 3` | `app.py` `timeout_and_error_handler` | Per `"{METHOD} {path}"` |
| Window endpoint | `POST /live-charts/viewer/api/window` | **Not** exempt |
| On exceed | Immediate **429** HTML/JSON | Log: `Request-Limit erreicht … (3/3)` |
| Slot release | `finally: active_requests[endpoint] -= 1` | On handler completion / timeout |

**Observed pattern:** Three concurrent **long** window POSTs (often `197.186.0.249` via proxy) occupy all slots for **minutes**; further window POSTs get **429** or wait behind thread pool; light endpoints intermittently **0-byte timeout** when the worker is saturated.

**Dedup:** BT-D34 inflight dedup only for **identical** cache digest; B-layer / multi-tile loads use **different** `from_utc`/`to_utc` → multiple independent ~200s leaders.

**Retries:** B-layer retries failed chunks; superseded `loadSerial` can still leave **in-flight HTTP** running (D36 supersede fix helps client cache, not server abort).

---

## 4. Confirmed bug — multi-follower inflight

**Symptom:** Second follower on same digest could raise `RuntimeError: inflight_wait_missing_result` after `_INFLIGHT_RESULT.pop` (reproduced in parallel test).

**Fix:** `wait_inflight()` now returns **`get_cached_window(digest)`** after the leader completes (before popping single-use inflight result).

**Test:** `test_bt_d37_inflight_multi_follower_v1.py` (passes).

---

## 5. Minimal code changes (dashboard only)

| File | Change |
|------|--------|
| `window_response_cache_v1.py` | Multi-follower safe `wait_inflight` |
| `historical_window_v1.py` | `batch_prefill_gate_enabled`, `builder_module_file` in provenance; `[landscape_window]` logs; `runtime` block on capabilities |
| `window_request_trace_v1.py` | Structured `[window_trace]` log lines |
| `window_service_v1.py` | Trace on cache hit/miss/build; warn on `build_lifetimes` > 30s |

No change to: 168h warmup, density math, trading, `MAX_CONCURRENT_REQUESTS`.

---

## 6. Controlled validation (post-fix)

| Step | Result |
|------|--------|
| Restart after fixes | PID `2979403` |
| `GET /live-charts/viewer/api/capabilities` | **200** in **~0.39 s** immediately after restart |
| Single cold window POST (45–90 s client timeout) | **0 bytes** — slots already refilled by external traffic |
| `landscape_window` / `window_trace` in `logs/dashboard.log` | **Not yet** (no completed build in quiet window during probes) |
| Warm cache HIT via HTTP | **Not verified** |
| BT-D26 `/runs` | **Timeout** under saturation |
| Research HTML `phase6xt-bt-d36-density-supersede-v1` | **Not fetched** during wedged period (verified on disk + D36A) |

**Operator action required for clean HTTP proof:** Close active **Research** tabs on `dash.immotel.de` (stops parallel window POSTs), then **one** cold window POST and confirm:

- `provenance.phase5s_batch_reader_active: true`
- `timing_ms.phase5s_prefill` ≈ 4–5 s
- `timing_ms.build_lifetimes` < 1 s
- `pools: 262`
- Second identical POST → `response_cache.hit: true`

---

## 7. Deployment

| Item | Detail |
|------|--------|
| Restarts performed | Stopped wedged PID `2969965`; started `2977970` then `2979403` with explicit PYTHONPATH |
| Log backup | `results/phase_bt_d37_window_stall_v1/pre_restart_log_tail.txt` |
| Frontend | Unchanged query string `phase6xt-bt-d36-density-supersede-v1` |
| `dashboard.service` (systemd) | `PYTHONPATH` = spread_recovery + dashboard only (**no** `pools+ob+delta_bot`); manual nohup used full triple path — align unit env if switching back to systemd |

---

## 8. Open issues

1. **Live traffic** keeps three legacy-duration window builds in flight → 429 and worker stalls without operator quiet period.
2. **Post-restart middleware** still logged **~160 s** window at 12:40 before landscape logging deployed — needs one **quiet** traced build to confirm batch active in-process.
3. **Browser** density still unverified.

---

## Stop condition

Evidence-backed root cause, minimal inflight + observability fixes, controlled restart, bounded validation attempted. No concurrency limit increase, no density/strategy changes.
