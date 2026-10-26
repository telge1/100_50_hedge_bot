# PHASE BT-D36A — Live BT-D35 Batch Defaults & BT-D36 Deployment Verify

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Report repo:** `/home/telgenbuescher/projects/pools+ob+delta_bot`

---

## VERDICT

**`LIVE_BATCH_PARTIAL`**

| Layer | Status |
|-------|--------|
| BT-D35 batch code on disk | **Verified** |
| Pre-restart live HTTP window | **Legacy non-batch path** (~165s `build_lifetimes`, batch off) |
| Deployment correction | **Applied** — dashboard process restart (stale worker + Jinja template cache) |
| Post-restart bounded HTTP window | **Not obtained** (0-byte waits / HTTP 429 concurrent cap) |
| In-process window build (same code/venv as server) | **Verified** — ~5s, batch prefill active, 262 pools |
| BT-D36 client assets | **Verified on disk + served HTML after restart** |
| Authenticated browser density render | **NOT_VERIFIED** |

---

## 1. Process identity (at end of verify)

| Field | Value |
|-------|--------|
| **PID** | `2969965` (after second restart; was `2958090` pre-fix) |
| **Started** | 2026-10-26 12:04:59 (local) |
| **CWD** | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` |
| **Command** | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/.venv/bin/python app.py` |
| **Listen** | `0.0.0.0:3000` |
| **PYTHONPATH** | `spread_recovery_hedge_short_dev` + `dashboard` + `pools+ob+delta_bot` |
| **PD1_LANDSCAPE_BATCH_READER** | *(unset)* |
| **HISTORICAL_LMV2_BATCH_READER** | *(unset)* |

Unset env → `_landscape_phase5s_prefill_enabled()` returns **True** (opt-out only).

**Imported modules (venv, same paths as server):**

- `.../dashboard/pd1_landscape_viewer_v1/historical_window_v1.py`
- `.../dashboard/pd1_landscape_viewer_v1/historical_window_batch_v1.py`
- `LANDSCAPE_LIFETIME_BUILDER_VERSION` = `pd1_landscape_lifetime_v1_phase5s_batch_default`

---

## 2. BT-D35 code checks

| Check | Result |
|-------|--------|
| `_landscape_phase5s_prefill_enabled()` | Present; default **on** |
| `prefill_member_cache_phase5s()` | Called when enabled |
| `timing_ms.phase5s_prefill` | Emitted in current builder |

---

## 3. Pre-restart live HTTP evidence (stale worker)

Artifact: `/tmp/window_d36.json` from `POST /live-charts/viewer/api/window` (completed ~296s wall; server log **165s** handler time on PID `2958090`).

| Field | Value |
|-------|--------|
| `provenance.phase5s_batch_reader_active` | **false** |
| `provenance.membership_loader` | `load_historical_pool_state_at_close` |
| `provenance.lifetime_builder_version` | **missing** |
| `timing_ms.build_lifetimes` | **164895.8 ms** |
| `timing_ms.phase5s_prefill` | **missing** (non-batch timing shape) |
| `response_cache` | **missing** (pre-D34-style payload) |
| `pools` | 262 |

**Conclusion:** The long-running `:3000` process was **not** executing the current BT-D35/BT-D34 window stack, despite updated files on disk. ~297s background POST was **compute + queue**, not the ~5s batch path.

---

## 4. BT-D36 client deployment

| Asset | On disk | Served HTML (after restart) |
|-------|---------|-----------------------------|
| `pd1_exact_origin_density_research_v1.js` | `phase6xt-bt-d36-density-supersede-v1`, `scheduleSupersededDensityRefresh` | `?v=phase6xt-bt-d36-density-supersede-v1` |
| `research_charts.js` | `ASSET_V = phase6xt-bt-d36-density-supersede-v1` | same |
| Stale Jinja before restart | — | Served **old** query strings (`phase6xt-exact-origin-canvas-planned-hotfix-v1`, `research-fullscreen-tools-v3`) while disk already had D36 |

**Fix:** Kill stale PID `2958090` and restart `app.py` so Jinja reloads `templates/research_charts.html` and Python imports current `pd1_landscape_viewer_v1`.

---

## 5. Single bounded reference window (5m PD1, 8h)

**Window:** `2026-09-11T07:34:51.000Z` → `2026-09-11T15:34:51.000Z`, `pool_detail=1`, `response_mode=exact_origin_v1`.

### A) In-process build (authoritative for **current code**, no HTTP queue)

One cold call via `get_landscape_window_response()` (same venv + PYTHONPATH as server):

| Metric | Value |
|--------|--------|
| Wall (CLI) | **5.004 s** |
| `response_cache.hit` | **false** (MISS) |
| `timing_ms.phase5s_prefill` | **4498.4 ms** |
| `timing_ms.build_lifetimes` | **296.2 ms** |
| `timing_ms.total` | **4954.1 ms** |
| `provenance.phase5s_batch_reader_active` | **true** |
| `provenance.lifetime_builder_version` | `pd1_landscape_lifetime_v1_phase5s_batch_default` |
| Pools | **262** |

### B) Live HTTP (same window; **not** repeated after contention)

| Attempt | HTTP | Wall | Notes |
|---------|------|------|--------|
| Pre-restart | 200 | ~296.7 s | Batch **off**, see §3 |
| Post-restart | 429 | ~160.5 s | `Too many concurrent requests` (limit **3**/endpoint) |
| Post-restart | — | 45–120 s | **0 bytes**, TTFB 0 — event loop/thread pool saturated |

**Queue vs compute:** Pre-restart server log attributed **~165–198 s** to the window handler itself (matches non-batch `build_lifetimes`). Post-restart probes failed before JSON body → **wait/contention**, not a second cold timing sample.

---

## 6. Minimal correction applied

1. **Force-restart** dashboard on `:3000` (stale worker from before BT-D35/D36 reload).
2. **Explicit PYTHONPATH** on start (matches production layout).

No changes to density math, caching architecture, or trading logic.

---

## 7. Browser verification

**NOT_VERIFIED** — no authenticated browser session exercised for canvas bands in this phase.

---

## 8. Operational follow-up (manual)

1. Confirm single `:3000` listener after restart: `ss -tlnp | grep 3000`.
2. When load is quiet, one `POST /live-charts/viewer/api/window` should return **~5s** server `timing_ms.total` and `phase5s_batch_reader_active: true`.
3. Hard-refresh Research; script URLs must include `phase6xt-bt-d36-density-supersede-v1`.

---

## Stop condition

Verification and documented restart complete. No further code changes in this phase.
