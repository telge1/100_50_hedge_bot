# NIGHT DROP ROOT CAUSE REPORT v1

**Verdict:** `NIGHT_DROP_ROOT_CAUSE_FIXED_READY_RESTART_REQUIRED`

**Live process:** PID **1530387** left running (untouched). OI PID **147111** untouched. No commit/push. No further symbols. No DB writes.

**Artifacts:** `results/btc_doge_full_ob_edge_flight_recorder_v1/edge_capture_1h_v1/night_drop_root_cause_v1/`

---

## Phase A — Frozen artifacts

Read-only hardlink/copy + SHA256 of:

- `events/DOGEUSDT_20260903T233008Z_f0df8d9b04/` (63 files)
- `events/BTCUSDT_20260904T004200Z_0c13abdcf9/` (63 files)
- `logs/health_tail_32MB.ndjson`, `logs/nohup_tail_16MB.log`
- `analysis/PROCESS_CONFIG_SNAPSHOT.json` (PID 1530387 cmdline + FR env)
- hashes under `hashes/`

Originals were not modified.

---

## Phase B — Temporal reconstruction (proof)

Process start inferred from first DOGE CAPTURING health sample: **2026-09-03T23:04:37.7Z**.

### DOGEUSDT `…233008Z…` (CROSS_IN @ 23:30:08Z, hard-cap 02:30:08Z)

| Milestone | Wall clock (UTC) | Evidence |
|---|---|---|
| Capture open, cont=0, drops=0, werr=0, backlog=1788 | 23:30:08 | health; prebuffer flush into new sink |
| **First drops + writer errors** | **23:30:13** (~T+5s) | drops=486, **werr=299**, backlog=0 |
| drops≥1000 | 23:30:33 | werr=828 |
| drops≥10000 | 23:34:58 | werr=7975 |
| First 30-min segment rollover → cont=1 | ~00:00:11 | sink replaced → **per-sink drops reset** (health shows d_drops=65) |
| Normal end / extension flood starts | **00:30:01** | d_ext=36 already; then **~29 extensions/s** |
| d_ext≥1000 | 00:30:36 | drops climb again with werr |
| Hard cap / finalize | 02:30:08 | queue_drop_count=**112683**, extension_count=**208769**, retouch_count=**97471** |

### BTCUSDT `…004200Z…` (CROSS_IN @ ~00:42)

| Milestone | Wall clock (UTC) | Evidence |
|---|---|---|
| BTC capture starts while DOGE already failing | 00:41:57 | process drops already ~45k; BTC sink opens with immediate drops |
| BTC extension flood | ~01:42 | after BTC min-post |
| Finalize | ~03:42 | queue_drop_count=**109268** |

### Correlation answers (checklist)

1. **First segmentwechsel** — secondary. Drops start **~5s after CROSS_IN**, long before first 30-min rollover.
2. **30-Minuten-Rollover** — amplifies loss (sink/queue swap; health counters reset) but is **not** the first-drop cause.
3. **256-MiB-Rollover** — not implicated (segments closed on time limit; small DOGE files).
4. **Start of second concurrent event** — BTC starts into an already broken DOGE writer/error storm; worsens dual load but DOGE was already dropping alone.
5. **Event-Extension** — **primary amplifier after 00:30**: `normal_end_ts` reset each tick → extension+marker every loop (~29/s).
6. **Result-Tail** — RESULT at 23:30:40; does not by itself cause T+5s failures.
7. **Hard-Cap** — only ends the event; drops already huge.
8. **zstd close/open** — happens at rollover; secondary gap window when old sink was popped (pre-fix).
9. **Writer-Thread restart/death** — thread stayed “alive”; **batch exceptions** counted as drops (`writer_error_count` ≈ 0.8× drops early).
10. **Queue/Sink swap** — yes, every segment (pre-fix); health `queue_drop_count` returned to 0 after event end.
11. **Reconnect/Resync** — overnight reconnects occurred; manifests `u_gap_count=0` (source feed). Not the drop mechanism.
12. **Socket depth=0** — out of scope for this failure mode; prior Phase E was clean.

**First drop time (exact):** **2026-09-03T23:30:13.368Z** (DOGE), simultaneous with first `writer_error_count>0`, **queue backlog=0** → not classic “slow writer fill”, but **writer-batch exceptions**.

---

## Proven root cause (causal chain)

### Primary (necessary and sufficient for early collapse)

**Marker records used ISO-string `ts`. `ActiveEventWriter._note_continuity` did `int(ts)` assuming Bybit epoch-ms.**

```text
EDGE_RETOUCH / EXTENSION / RESULT markers
  → try_put into same queue as deltas
  → append_delta_batch → _note_continuity
  → int("2026-09-03T23:30:08.359952Z") → ValueError
  → entire batch counted as drops + writer_error_count++
```

Reproduction (offline): `NOTE_CONTINUITY_FAIL ValueError invalid literal for int()...`

Night evidence:

- Health: drops and `writer_error_count` rise together from T+5s with **empty backlog**.
- **Zero markers** in any persisted segment (all marker writes failed inside batches; mixed batches also killed real deltas).
- Surviving records are only deltas with numeric `ts`.

Watcher emitted **`EDGE_RETOUCH` on every capture tick** (~8–10/s via WS recv loop + 0.25s timeout), so poisoned markers entered the queue continuously from the first seconds of capture.

### Secondary amplifier (after minimum capture end)

`_maybe_end_event` **reassigned** `plan.normal_end_ts = compute_normal_end(...)` **every tick**, wiping prior extensions. Once `now >= base_normal_end` and fight still open:

- every tick: extension_count++, EXTENSION marker enqueue
- DOGE: **208769** extensions (~29/s after 00:30)
- Each extension marker re-triggered the ValueError path → drop rate roughly doubled

### Tertiary architecture defect (rollover)

`_rotate_segment` **popped the sink, stopped the writer thread, finalized on the asyncio thread, then created a new queue+thread**.

Effects:

- Producer had no sink during finalize → ringbuffer / loss window
- Per-sink drop counters reset → health looked “healthy” after segment/event end
- Negative `writer_messages_per_second` after rollover (rate math vs new sink counters)

### Quaternary (counter semantics)

`health_dict()` summed **only current sinks**. After both events ended → live `queue_drop_count=0` despite **>220k** night drops. Lifetime counters were missing.

---

## Phase D — Source vs persisted continuity

| Event | manifest `u_gap_count` (source) | persisted records | persisted_u_gap_count | missing_u estimate | completeness |
|---|---|---|---|---|---|
| DOGE | **0** | 3279 | **333** | **53379** | **5.8%** |
| BTC | **0** | 22506 | **6594** | **34410** | **39.5%** |

Later DOGE cont segments: ~50–60 records spanning ~9000 u → must not report persisted continuity as 0.

`research_eligible` must depend on **persisted** continuity, not only source-feed gaps.

---

## Phase C+E — Code lifecycle answers

| Question | Answer (pre-fix) |
|---|---|
| Same producer queue across segments? | **No** — new `NonBlockingDeltaSink` per segment |
| Queue/sink replaced on rollover? | **Yes** |
| Deltas discarded during rollover? | **Yes** (no sink → ringbuffer, not flushed into next segment) |
| One symbol waits on the other? | Separate threads, but shared asyncio blocked on sync finalize |
| Writer still consuming after seg0? | Thread restarted; often drowning in exception batches |
| `task_done`/backlog correct? | Backlog often 0 while errors accumulate (fail-fast batches) |
| Drops double-counted? | try_put Full + writer exception path both increment sink.drops; plan also += on Full |

---

## Phase F — Fix implemented (offline; requires restart)

Code under `orderbook_analyse/.../full_ob_edge_flight_recorder/`:

1. **`event_writer._note_continuity`** — skip markers; only parse numeric `ts`; track `persisted_u_gap_count` / missing intervals.
2. **Watcher** — `EDGE_RETOUCH` only on actual reclaim edge; not every tick.
3. **Extension** — `normal_end_ts = max(normal_end_ts, base_end)` so extensions never rewind.
4. **Long-lived SymbolWriter** — `NonBlockingDeltaSink.rotate_writer()` keeps **same queue + same thread**; segment finalize/open runs on the writer thread; producer uninterrupted.
5. **Lifetime health counters** — `process_lifetime_queue_drops`, `symbol_lifetime_queue_drops`, `last_event_queue_drops`, `total_research_ineligible_events`, `current_writer_alive`; process lifetime **never decreases**.
6. **Manifest** — `source_feed_u_gap_count` vs `persisted_capture_u_gap_count`; research gate on `PERSISTED_U_GAP` / `WRITER_ERROR`.
7. **Exception logging** — `fr_writer_batch_failed` with exception type/message (no more silent batch death).

**Not applied to PID 1530387** (still running old code).

---

## Phase G — Tests

`29 passed` in 45.29s including:

- `test_marker_iso_ts_does_not_fail_writer_batch`
- `test_extension_normal_end_is_monotonic`
- `test_segment_rotate_keeps_same_queue`
- `test_writer_failure_marks_errors_and_drops`
- `test_long_dual_symbol_segmented_load` @ **1× / 2× / 3×** peak (compressed dual-symbol multi-rotate + ISO markers)
- prior throughput/bootstrap suite

Bench lines: `analysis/LONG_DUAL_LOAD_BENCH.jsonl`, `analysis/PYTEST_OUT.txt`.

Writer-failure test: errors visible, drops increment, thread remains joinable, metrics expose `writer_error_count` (fail-closed via counters + research_eligible path on finalize).

---

## Phase H — Constraints honored

- PID 1530387 not stopped / no env change / no live trigger
- No open `.tmp` mutated
- No extra symbols / no DB / no ClickHouse / no dashboard restart
- OI 147111 unchanged
- No git commit / push

---

## END VERDICT

```text
NIGHT_DROP_ROOT_CAUSE_FIXED_READY_RESTART_REQUIRED
```

A controlled collector restart is required to load the fix. Do **not** treat the still-running PID 1530387 as validated.
