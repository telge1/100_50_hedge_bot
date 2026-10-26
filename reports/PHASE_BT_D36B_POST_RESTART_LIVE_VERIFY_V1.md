# PHASE BT-D36B — Post-Restart Live HTTP and Batch Verification

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Pool repo:** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Live URL:** `http://dash.immotel.de:8080/live-charts/research`

---

## VERDICT

**`LIVE_HTTP_BLOCKED`**

Live `:3000` accepted TCP but did **not** return HTTP bodies within probe timeouts (capabilities, research, window, BT-D26, even `GET /`). A controlled live window JSON sample was **not** obtained. Offline CLI (same codepath/venv) confirms BT-D35 batch metrics. **Browser density: NOT_VERIFIED.**

---

## 1. Live process (read-only)

| Field | Value |
|-------|--------|
| **Listener** | `0.0.0.0:3000` |
| **PID** | `2969965` (unchanged since BT-D36A restart) |
| **Started** | 2026-10-26 12:04:58 (local) |
| **CWD** | `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard` |
| **Command** | `.venv/bin/python app.py` |
| **PYTHONPATH** | `spread_recovery_hedge_short_dev` + `dashboard` + `pools+ob+delta_bot` |
| **Batch env** | `PD1_LANDSCAPE_BATCH_READER` / `HISTORICAL_LMV2_BATCH_READER` **unset** → default **on** |
| **Module (venv import)** | `dashboard/pd1_landscape_viewer_v1/historical_window_v1.py` |
| **`_landscape_phase5s_prefill_enabled()`** | `True` |
| **`LANDSCAPE_LIFETIME_BUILDER_VERSION`** | `pd1_landscape_lifetime_v1_phase5s_batch_default` |
| **Process at probe time** | ~27% CPU, ~15+ min elapsed, multiple `127.0.0.1` connections with **Recv-Q 607** (stalled responses) |

---

## 2. HTTP 429 — root cause (confirmed in code + logs)

**Source:** `app.py` → `timeout_and_error_handler` middleware (`@app.middleware("http")`).

| Mechanism | Detail |
|-----------|--------|
| Counter | Global `active_requests[endpoint]` per `"{METHOD} {path}"` |
| Limit | `MAX_CONCURRENT_REQUESTS = 3` (non-config endpoints) |
| Window endpoint | `POST /live-charts/viewer/api/window` **not** rate-limit exempt |
| On exceed | **HTTP 429** immediately (HTML or JSON), log: `Request-Limit erreicht für POST /live-charts/viewer/api/window (3/3)` |
| Slot release | `finally` decrements counter when handler completes (incl. timeout path) |

**Observed (BT-D36A / D36B logs):**

- Three concurrent window POSTs can run **~165–215 s** each (middleware wall time).
- A fourth window POST gets **429** without waiting in queue.
- Separate limit bucket for `GET /api/logs/changed` (also 3/3 during storms).

**Not the cause of 0-byte hangs:** 429 returns a body immediately. **0-byte / TTFB=0** indicates the worker is **not completing responses** for accepted requests (thread-pool / long builds / overload), not the 429 branch.

**Dedup:** BT-D34 `begin_inflight` / `wait_inflight` dedupes **identical cache digest** only. B-layer / multi-tile loads use **different** `from_utc`/`to_utc` → multiple slow leaders → fills all **3** middleware slots.

**Browser retries:** Research B-layer (`MAX_CONCURRENT_WINDOW_FETCH = 2`) plus GO TO / viewport replans plus external tab on `197.186.0.249` amplify parallel window POSTs. No change applied (per scope).

---

## 3. Controlled live window test

**Payload (8 h reference, unchanged):**

```json
{
  "from_utc": "2026-09-11T07:34:51.000Z",
  "to_utc": "2026-09-11T15:34:51.000Z",
  "timeframe": "5m",
  "pool_detail": 1,
  "response_mode": "exact_origin_v1"
}
```

| Attempt | HTTP | Client wall | TTFB | Body |
|---------|------|-------------|------|------|
| Window POST (200 s cap) | — | 200.0 s | 0 | none (client timeout) |
| Window POST (25 s cap, after pause) | — | 25.0 s | 0 | none |
| Capabilities (5–8 s) | — | timeout | 0 | none |
| `GET /` (2 s) | — | timeout | 0 | none |

**No retry after 429** (none received on last attempts; earlier D36A 429 at 160 s wall documented in BT-D36A).

**Server log (same PID, post-restart) — middleware duration only:**

| Time (local) | `POST …/api/window` wall |
|--------------|--------------------------|
| 12:08:29 | 205.75 s |
| 12:11:13 | 164.34 s |
| 12:14:22 | 184.19 s |
| 12:20:44 | 205.41 s |
| 12:24:19 | 215.10 s |

These complete with **200 OK** for `197.186.0.249` (proxied live user), but **~200 s per request** — not the **~5 s** reference from isolated CLI. Likely drivers: **parallel window builds** + **ClickHouse / thread contention** under live load (not proven from response JSON).

---

## 4. Offline reference build (same codepath, not live HTTP)

Artifact: `results/phase_bt_d36b_live_window_v1/cli_reference_build.json`

| Field | Value |
|-------|--------|
| Client wall | **5.381 s** |
| `response_cache.hit` | **false** (MISS) |
| `timing_ms.phase5s_prefill` | **4848.8 ms** |
| `timing_ms.build_lifetimes` | **286.1 ms** |
| `timing_ms.total` | **5329.7 ms** |
| `phase5s_batch_reader_active` | **true** |
| `membership_loader` | `phase5s_episode_walk_batch_cache` |
| Pools | **262** |
| `key_digest` | `e2fd55f290d21143` |

**Parity:** Matches BT-D35/D36A reference (262 pools, batch prefill dominant). This validates **deployed code**, not the wedged HTTP worker.

---

## 5. Event-loop / request stability (limited probes)

| Probe | During load |
|-------|-------------|
| `GET /live-charts/viewer/api/capabilities` | Timeout, 0 bytes |
| `GET /live-charts/research` | Timeout (D36B); BT-D36A had 200 + d36 tags when idle |
| Parallel cap during 200 s window wait | Timeout |
| HTTP 504 | Not observed on probes (client gave up first) |
| Server after probes | Still listening; still high CPU; still slow window completions in log |

**Conclusion:** Uvicorn worker effectively **unresponsive** for light endpoints while long window work and external traffic continue. `asyncio.to_thread` does not prevent **thread-pool exhaustion** when many slow builds overlap.

---

## 6. BT-D26 dynamic selector

Probes (4 s timeout, authenticated `session_id` only — **not logged**):

| Endpoint | Result |
|----------|--------|
| `GET …/bt-d26-dynamic-selector/runs` | Timeout, 0 bytes |
| `POST …/load` | Timeout, 0 bytes |
| `POST …/sync-viewport` | Timeout, 0 bytes |

**Not verified live** in this phase (server blocked). No changes to setups, entry, SL, TP, or as-of logic.

---

## 7. Frontend version (HTTP)

| Check | Result |
|-------|--------|
| Served HTML in D36B | **Could not fetch** (timeout) |
| BT-D36A after restart | `phase6xt-bt-d36-density-supersede-v1` in `/live-charts/research` |
| On disk | `research_charts.js`, `pd1_exact_origin_density_research_v1.js`, template query strings = **d36** |

**Browser density:** **NOT_VERIFIED** (no authenticated browser session in this run).

---

## 8. Open problems

1. **Live HTTP saturation:** ≥3 concurrent **~200 s** window POSTs fill middleware slots → **429** for additional window calls.
2. **Worker hang:** Even cache-eligible requests and light endpoints time out with **0 TTFB** under current load.
3. **Latency gap:** Middleware logs **~200 s** per window on live PID vs **~5 s** `timing_ms.total` in isolated CLI — needs a **quiet** single-fly window POST with JSON capture (not done here).
4. **Operational:** External research usage on `197.186.0.249` continues to drive window POSTs during verification.

---

## 9. Actions taken / not taken

| Action | Done? |
|--------|-------|
| Read-only process / env / log forensics | Yes |
| One heavy window POST (+ failed light probes) | Yes |
| Raise `MAX_CONCURRENT_REQUESTS` | **No** (forbidden) |
| Density / strategy / cache architecture changes | **No** |
| Pulsed restart | **No** (server already post-D36A restart; wedging is runtime load) |

**Recommended (ops, outside this phase):** Pause live research density reloads, wait until **no** in-flight window POSTs, then **one** authenticated window POST and confirm `timing_ms.phase5s_prefill` + `response_cache` in JSON. Restart only if a single quiet request still shows `phase5s_batch_reader_active: false`.

---

## Stop condition

Bounded verification complete. No code or config changes in this phase.
