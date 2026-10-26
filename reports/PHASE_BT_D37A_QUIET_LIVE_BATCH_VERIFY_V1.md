# PHASE BT-D37A — Quiet Live Batch Verification

**Date:** 2026-10-26  
**Dashboard:** `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard`  
**Pool repo:** `/home/telgenbuescher/projects/pools+ob+delta_bot`  
**Context:** BT-D37 `LIVE_WINDOW_PARTIAL`

---

## VERDICT

| Label | Result |
|-------|--------|
| **`LIVE_BATCH_ROOT_CAUSE_CONFIRMED`** | **Yes** |
| **`LIVE_BATCH_HTTP_VERIFIED`** | **No** — no complete live window JSON captured |
| **`LIVE_BATCH_STILL_BLOCKED`** | **Yes** — for conclusive HTTP body + warm-cache proof |

**Summary:** The live `:3000` worker is a **stale Python process** still executing the **committed** landscape builder gate (`historical_lmv2_batch_reader_enabled()` → **off** when env unset). On-disk **uncommitted** BT-D35 code uses `_landscape_phase5s_prefill_enabled()` → **on** by default. That mismatch explains **~172–179 s** live middleware wall times vs **~4.8 s** CLI on current files. A controlled live POST **completed on the server** (~172 s) but the client **timed out at 120 s** (0 bytes). **No restart** was performed in this phase (bounded stop).

---

## 1. Quiet state (Step 1)

| Check | Evidence |
|-------|----------|
| **PID** | `2979403` (re-validated; not assumed from prior phase) |
| **Started** | Mon 2026-10-26 **12:43:08** local |
| **Elapsed (probe)** | ~17 min |
| **Listener** | `0.0.0.0:3000` → `python` pid=2979403 |
| **Supervisor** | **nohup** / interactive parent — **not** active `dashboard.service` (`systemctl is-active` → `inactive`) |
| **CWD** | `.../spread_recovery_hedge_short_dev/dashboard` |
| **Command** | `.venv/bin/python app.py` |
| **PYTHONPATH (live PID)** | `spread_recovery_hedge_short_dev` + `dashboard` + **`pools+ob+delta_bot`** |
| **Batch env on PID** | `PD1_LANDSCAPE_BATCH_READER` / `HISTORICAL_LMV2_BATCH_READER` **unset** |
| **Concurrency** | `MAX_CONCURRENT_REQUESTS = 3` for `POST /live-charts/viewer/api/window` (`app.py`) |
| **TCP :3000** | Low Recv-Q vs prior saturation; mostly `127.0.0.1` + proxy `197.186.0.249` on **`GET /api/logs/changed`** (not treated as a specific browser identity) |
| **Window slot pressure** | During verify, **three** slow window builds logged on current PID (12:46, 12:55, 12:59); no new `Request-Limit` window 429s in this window |

**Slow window middleware timings (current PID, `dashboard.log`):**

| Local time | Wall (s) |
|------------|----------|
| 12:46:21 | 173.93 |
| 12:55:05 | 172.38 |
| 12:59:00 | 178.72 |

These align with BT-D37 **legacy CLI** (`PD1_LANDSCAPE_BATCH_READER=0` / old gate) ~181 s, not batch ~5 s.

---

## 2. Runtime provenance (Step 2)

### Live HTTP

| Probe | Result |
|-------|--------|
| `GET /live-charts/viewer/api/capabilities` | **200**, ~0.3–0.5 s |
| `capabilities.runtime` | **`null`** (field absent in JSON) |
| `landscape_window` / `window_trace` in `dashboard.log` | **None** (D37 logging not active in loaded modules) |

### On-disk vs in-process (critical)

| Item | Fresh CLI import (same venv + PYTHONPATH as live) | Live worker (inferred) |
|------|---------------------------------------------------|-------------------------|
| `historical_window_v1.py` mtime | **2026-10-26 12:44:34** (after process start **12:43:08**) | Modules loaded at start **before** this save |
| Batch gate in **working tree** | `_landscape_phase5s_prefill_enabled()` → **True** when env unset | **Committed** code path: `historical_lmv2_batch_reader_enabled()` → **False** when env unset |
| `capabilities.runtime` | Present with `batch_prefill_gate_enabled: true` | **`null`** |
| `builder_module_file` (capabilities) | `.../dashboard/pd1_landscape_viewer_v1/historical_window_v1.py` | Not exposed (no `runtime` block) |

**Git working tree diff (dashboard repo, `historical_window_v1.py`):** replaces opt-**in** `historical_lmv2_batch_reader_enabled()` with opt-**out** `_landscape_phase5s_prefill_enabled()` and adds `runtime` / provenance fields (BT-D35/D37). **Live process never re-imported this file after edits.**

**`historical_lmv2_batch_reader_enabled()` definition:** returns true only if `HISTORICAL_LMV2_BATCH_READER` ∈ `{1,true,yes}` — **unset → false** (`research_charts/historical_pool_state_batch_reader_v1.py`).

### systemd (reference only — not serving traffic)

| Field | Value |
|-------|--------|
| `dashboard.service` | **inactive** |
| `Environment=PYTHONPATH` | spread_recovery + dashboard — **no** `pools+ob+delta_bot` (unchanged; no systemd edits in this phase) |

### CLI reference build (current on-disk code, not live HTTP)

Artifact: `results/phase_bt_d37a_quiet_verify_v1/cli_reference_d37a.json`

| Field | Value |
|-------|--------|
| Wall | **4.77 s** |
| `phase5s_batch_reader_active` | **true** |
| `batch_prefill_gate_enabled` | **true** |
| `membership_loader` | `phase5s_episode_walk_batch_cache` |
| `timing_ms.phase5s_prefill` | 4291.6 |
| `timing_ms.build_lifetimes` | 278.4 |
| `timing_ms.total` | 4716.3 |
| `pool_count` | **262** |
| Cache digest prefix | `e2fd55f290d21143` |

---

## 3. Controlled live POST (Step 3)

**Payload:** Sep-11 2026 8h window, 5m PD1, `exact_origin_v1` (canonical BT-D36A/D37).

| Attempt | Client timeout | HTTP body | Server evidence |
|---------|----------------|-----------|-----------------|
| Primary (D37A) | **120 s** | **0 bytes** (curl timeout) | `dashboard.log`: **172.38 s** middleware wall ending **12:55:05** |
| Follow-up cache probe | 30 s | 0 bytes | **178.72 s** at **12:59:00** (should not have been run under strict “one heavy POST”; noted for audit) |

**Not recorded from live HTTP:** `response_cache` HIT/MISS, `phase5s_batch_reader_active`, `batch_prefill_gate_enabled`, `builder_module_file`, `timing_ms` breakdown, `request_id`.

**Blocking reason for JSON sample:** (1) client timeout **&lt;** server legacy build (~172 s); (2) running worker on **pre–BT-D35 gate** regardless of unset batch env.

Per instructions: **no further heavy window POST** after failed primary capture.

---

## 4. Warm cache (Step 4)

**Skipped** — first POST did not return a valid client-side response (Step 4 precondition not met).

---

## 5. Root cause (Step 5)

| Cause | Status |
|-------|--------|
| Wrong batch gate in **loaded** `historical_window_v1` | **Confirmed** — opt-in `historical_lmv2_batch_reader_enabled()` vs on-disk opt-out `_landscape_phase5s_prefill_enabled()` |
| Stale import / restart before disk save | **Confirmed** — PID start **12:43:08**, module mtime **12:44:34**, `capabilities.runtime` **null** |
| systemd / wrong PYTHONPATH on live PID | **Ruled out** for current listener — nohup PID includes `pools+ob+delta_bot` |
| `PD1_LANDSCAPE_BATCH_READER=0` on PID | **Ruled out** — unset |
| Proxy IP as sole proof of user | **Not used** |
| Legacy loader as unexplained mystery | **Ruled out** — explained by old gate + ~172 s wall time |

**Measured slow stage (live):** full handler wall ~172 s matches **non-batch** `build_lifetimes` dominance (same as BT-D37 CLI batch-off), not a separate HTTP-only algorithm.

**Minimal safe correction (not applied in D37A):** Restart the **nohup** `app.py` worker **after** BT-D35/D37 dashboard changes are saved on disk (same PYTHONPATH as today), then repeat **one** cold + **one** warm window POST with timeout **≥ 200 s** on cold. **Do not** enable batch via `HISTORICAL_LMV2_BATCH_READER=1` as a long-term substitute for loading `_landscape_phase5s_prefill_enabled()` unless explicitly chosen.

---

## 6. Artifacts

| Path | Description |
|------|-------------|
| `results/phase_bt_d37a_quiet_verify_v1/cli_reference_d37a.json` | CLI metrics on current files |
| `results/phase_bt_d37a_quiet_verify_v1/window_cold.json` | **Not created** (live timeout) |
| `reports/PHASE_BT_D37_LIVE_WINDOW_STALL_RECOVERY_V1.md` | Prior phase context |

---

## 7. Scope compliance

- No strategy / Entry / SL / TP / density SSOT / backtest changes  
- No `MAX_CONCURRENT_REQUESTS` change  
- No systemd modifications  
- No mass HTTP probing  
- Browser density: **NOT_VERIFIED**

---

**End of BT-D37A bounded verification.**
