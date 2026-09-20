# NIGHT DROP FIX — LIVE RESTART REPORT

**ENDVERDICT:** `NIGHT_DROP_FIX_LIVE_RUNNING_WAITING_FINAL_EVENT`

**Generated:** 2026-09-04T08:23Z (approx)

---

## Summary

Exactly one controlled restart applied the night-drop fix. The former failure mode (ISO-marker `int(ts)` ValueError → batch drops within ~T+5s, extension spam, sink/queue swap) is **live-disproven** on two concurrent real CROSS_IN captures. Full research finalization, 30-minute segment rollover proof, and ≥600s prebuffer are **still in progress** via a detached read-only watchdog (events remain open).

| Item | Value |
|---|---|
| Old PID | 1530387 (SIGTERM 2026-09-04T08:02:59Z, exited ~2s) |
| New PID | **1565672** |
| Collector count | **1** |
| OI PID | **147111** untouched |
| Symbols | BTCUSDT, DOGEUSDT only |
| Phase-A tests | **66 passed** |
| 15-min smoke | **PASS** (`process_lifetime_queue_drops=0`, `writer_error_count=0`) |
| Live CROSS_IN | BTC `…080534Z…`, DOGE `…080551Z…` — both capturing, drops=0 past T+5s |

---

## Phase A — Regression suite

Ran jointly:

- `test_full_ob_sync_contract.py`
- `test_full_ob_socket_lock_offload.py`
- `test_full_ob_edge_flight_recorder_v1.py`
- `test_full_ob_edge_capture_timing_v1.py`
- `test_full_ob_writer_throughput_bootstrap_v1.py`
- `test_night_drop_root_cause_v1.py` (+ mandatory marker/ts/extension/lifetime cases)

**Result:** `66 passed in 46.78s`  
Artifact: `analysis/PHASE_A_PYTEST.txt`

Mandatory cases covered offline: EDGE_RETOUCH/EXTENSION ISO-ts, int-ms marker, timezone-aware datetime marker, invalid-ts fail-closed without silent batch loss, monotonic `normal_end_ts`, no idle-tick extension spam, rollover keeps queue/writer, lifetime counters never decrease, 1×/2×/3× dual load, slow-disk fail-closed.

---

## Phase B — Pre-restart

Read-only inventory: `pre_restart/PRE_RESTART_INVENTORY.json`

- States: BTC/DOGE **COOLDOWN**, `capturing=false`
- No open FR event FDs; stale legacy `.tmp` from 184212 untouched
- `restart_safe=true`
- Old process lacked lifetime counter fields (pre-fix binary)

---

## Phase C — Exactly one restart

1. SIGTERM 1530387 @ 08:02:59Z → exit ~2s  
2. Confirmed no `orderbook_v2_live` collector  
3. Started one collector with identical FR/raw-archive env  
4. New PID **1565672**; OI 147111 unchanged  
5. No second restart  

Artifact: `analysis/PHASE_C_RESTART.json`

New health immediately exposes fix fields: `process_lifetime_queue_drops=0`, `current_writer_alive=true`, per-symbol `symbol_lifetime_queue_drops`, `source_feed_u_gap_count` / `persisted_capture_u_gap_count`.

---

## Phase D — Live smoke (≥15 min) — PASS

Artifact: `smoke/SMOKE15_RESULT.json` (45 samples, 0 failures)

| Gate | Result |
|---|---|
| Exactly one collector | yes |
| `book_ready=true` | yes (both) |
| `full_book_active_topics=2` | yes |
| Source gaps (`full_book` gap_count / seq) | 0 |
| depth=0 uncapped socket | **ok**, 0 timeouts (`smoke/SOCKET_DEPTH0.json`) |
| `writer_error_count` | **0** |
| `process_lifetime_queue_drops` | **0** |
| `symbol_lifetime_queue_drops` | **0/0** |
| `current_event_queue_drops` | **0** |
| Queue backlog | controlled (hwm 801 at prebuffer flush; then 0) |
| Bootstrap → no spurious capture dirs | only real CROSS_IN dirs created |
| Ringbuffer → ~600s | reached for idle path earlier; live events started ~2–3 min after process start → **pre_trigger ≈143–161s** (not 600s) |

Lifetime counters stayed at 0 for the full smoke window (not the old sink-scoped “looks like 0 after event end”).

---

## Phase E — Marker / extension live (real CROSS_IN)

| Event | Trigger | Past T+5s | ISO markers persisted | `writer_error_count` | lifetime drops | `extension_count` |
|---|---|---|---|---|---|---|
| BTCUSDT_20260904T080534Z_1fd9a66d36 | 08:05:34Z CROSS_IN | yes | RESULT, EDGE_RETOUCH, PROFILE_UPDATE (str ts) | 0 | 0 | **0** |
| DOGEUSDT_20260904T080551Z_2c38905508 | 08:05:51Z CROSS_IN | yes | same | 0 | 0 | **0** |

Decoded open `.tmp` (read-only): **persisted_u_gaps=0**, markers with ISO-string `ts` present — the night ValueError path no longer kills batches.

Extension flood absent: not ~29/s; count remains 0 while still before `normal_end`.

Artifact: `smoke/LIVE_EVENTS_Tplus_CHECK.json`

---

## Phase F — Segment rollover

Not yet observed (30-minute segment due ~08:35Z). Detached watchdog continues:

- PID `event_watchdog.py` (nohup), status `RUNNING`
- Will record continuation_index++, lifetime counters, drops at rollover
- Timeout 4h or first finalized event
- Does not stop/restart collector or mutate collector files

State: `monitor/WATCHDOG_STATE.json`

---

## Phase G — Research eligibility (current)

Both live events currently show `research_eligible=true` / `data_quality=OK` / drops=0 / source+persisted gaps=0 **so far**, but:

- `pre_trigger_seconds_actual` ≈ **143s / 161s** (process restarted minutes before trigger) → **not** ≈600s
- Post-trigger minimum 3600s **not yet elapsed**
- Segment SHA chain / final offline replay **pending finalize**

→ Events must not be treated as research-complete until finalize + full contract checks.

---

## Phase H — Reconnects

During smoke window: `reconnects_total` rose **0→4**, `sequence_gaps_total=0`, `book_ready` stayed true, capturing continued, **lifetime drops remained 0**.

Watchdog `reconnect_samples` records each delta. Prior night process had ~49 reconnects (documented in pre-restart health); those are historical to PID 1530387.

---

## Constraints honored

- No extra symbols  
- One collector restart only  
- No dashboard restart  
- OI 147111 unchanged  
- No DB / ClickHouse / artificial triggers / trading  
- No auto-delete / no commit / no push  

---

## ENDVERDICT

```text
NIGHT_DROP_FIX_LIVE_RUNNING_WAITING_FINAL_EVENT
```

**Meaning:** Night-drop root-cause fix is live and active (T+5s marker path + zero lifetime drops on dual CROSS_IN). Waiting on detached monitor for first 30-min segment rollover proof and event finalization before promoting to `NIGHT_DROP_FIX_LIVE_PROVEN` under full Phase G research contract (especially 600s prebuffer on a later event after warm ringbuffer).

### Paths

- Report: `results/.../night_drop_live_fix_v1/LIVE_REPORT.md`
- Smoke: `.../smoke/SMOKE15_RESULT.json`
- Watchdog: `.../monitor/WATCHDOG_STATE.json` / `event_watchdog.py`
- Restart: `.../analysis/PHASE_C_RESTART.json`
