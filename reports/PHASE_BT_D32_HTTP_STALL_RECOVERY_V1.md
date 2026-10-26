# PHASE BT-D32 — Live HTTP Server Stall Root Cause and Minimal Recovery

**Date:** 2026-10-26  
**Dashboard:** `spread_recovery_hedge_short_dev/dashboard`  
**Artifacts:** `results/phase_bt_d32_http_stall_recovery_v1/`

---

## VERDICT

**`HTTP_STALL_FIXED_SERVER_VERIFIED`**

The **total HTTP freeze** (TCP connect OK, **0 response bytes**, localhost timeouts) was caused by **synchronous `build_landscape_window()` on the Uvicorn asyncio event loop**. Minimal fix: **`asyncio.to_thread` offload** in `pd1_landscape_viewer_v1/api.py`. After controlled restart (**PID 2942408**), light endpoints respond in **&lt;10 ms**; window API returns **200** with **262 pools** (~162 s); BT-D26 **load** returns **200** in **~17 ms**.

**Not verified in browser** (`BROWSER_VERIFIED` = no).

---

## A. Runtime before (PID 2936957)

| Item | Value |
|------|--------|
| PID | 2936957 (stopped for recovery) |
| Uptime at capture | ~16 min |
| Listen | `0.0.0.0:3000`, backlog connections `Recv-Q=607` |
| RSS | ~687 MB |
| Threads | 14 |
| PYTHONPATH | spread parent + dashboard + `pools+ob+delta_bot` |
| Symptom | `curl` to `/` and static JS: **timeout 0 bytes** |

See `runtime_before.json`, log tail in results dir.

---

## B. Stack forensics

- **gdb attach:** denied (`yama/ptrace_scope`).
- **Log + code evidence:** ~**197 s** duration shared across **window**, **BT-D26 load**, and **drawings/event** at the **same timestamp** → classic **event-loop starvation**.

See `stack_evidence.md`.

---

## C. Handler analysis

| Path | Issue |
|------|--------|
| `POST /live-charts/viewer/api/window` | **Sync** `build_landscape_window` inside `async def` (~172–198 s) |
| BT-D26 load | Fast (~2 ms in-process); appeared “slow” only while loop was blocked |
| Research overlays / pane | Can be slow (seconds) but not root of total stall |

`research_charts/api.py` already uses `asyncio.to_thread` for heavy routes; PD1 viewer did not.

See `handler_timing.csv`, `root_cause.md`.

---

## D. Safe reproduction

- In-process: `build_landscape_window` (8h 5m PD1) **172.1 s**, **262 pools**.
- Live (before fix): all probes **timeout**.
- Live (after fix, pass 2): root **302**, static **200**, window **200/262 pools/161.9 s**, load **200/True/0.017 s**.

See `http_verification.json` (`pass2_after_warmup`).

---

## E. Minimal fix

**File:** `dashboard/pd1_landscape_viewer_v1/api.py`

- `await asyncio.to_thread(build_landscape_window, …)`
- Same pattern for `landscape_capabilities` and `membership_at_utc`.

**Test:** `test_pd1_viewer_window_thread_offload_v1.py` (1 passed).

No changes to density SSOT, BT-D26 data, entry/SL/TP, or scanner logic.

See `minimal_fix_summary.md`.

---

## F. Controlled recovery

1. Saved logs / `runtime_before.json`.
2. `kill -TERM` **2936957** (dashboard only).
3. Restart:

```bash
cd .../dashboard
PYTHONPATH=".../spread_recovery_hedge_short_dev:.../dashboard:.../pools+ob+delta_bot" \
  nohup .venv/bin/python app.py >> /tmp/dashboard_app_3000.log 2>&1 &
```

4. **New server PID:** **2942408** (listener on `:3000`).

---

## G. Post-fix HTTP proof (SERVER_RECOVERED)

| # | Check | Result |
|---|--------|--------|
| 1 | Light endpoint | **302** in &lt;1 ms |
| 2 | `research_charts.js` | **200**, 371229 bytes |
| 3 | 5m PD1 window | **200**, **262 pools**, ~162 s |
| 4 | BT-D26 `/runs` | **200** (pass 1) |
| 5 | BT-D26 `/load` | **200**, success (pass 2) |
| 6 | `/sync-viewport` | **200** (pass 1, ~28 s) |
| 7–9 | Stability | Static **3× &lt;2 ms** while other routes work (pass 1) |
| 10 | Density compute path | Unchanged (`build_landscape_window` only threaded) |

---

## H. As-of drawings

Stale **PLANNED** box at **11:34:51** with **no** candidate before **12:00** is explained by **persisted drawings** + **failed/stalled sync** during BT-D31 stall. Server-side sync logic **does** prune `btd26-*` IDs not in the active view when sync runs. **No separate UI patch** in BT-D32.

---

## I. Root cause matrix (summary)

| Symptom | Cause | Status |
|---------|--------|--------|
| 0-byte HTTP timeout on all routes | Sync window on asyncio loop | **Fixed** (thread offload) |
| “lade PLANNED setups …” stuck | `/load` never completed during stall | **Expected recovered** with HTTP |
| No density bands | `/window` never returned | **Expected recovered** (slow window remains) |
| Box at wrong as-of | Persisted drawings + no sync | **Documented**; not HTTP-fix scope |

---

## Stop

Repair and server verification complete. Browser confirmation left to operator.
