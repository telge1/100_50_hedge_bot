# BATCH_WRITER_LIVE_RESTART_REPORT (RETROSPECTIVE)

**Status:** `RETROSPECTIVE_SMOKE_REPORT_INCOMPLETE`

This file is a **retrospective** account of the controlled restart that produced PID **1530387** on 2026-09-03. It does **not** invent a completed 15-minute Phase-D smoke.

---

## What was observed

| Item | Fact |
|---|---|
| Prior PID | 1481866 stopped (SIGTERM) after queue-drop failure |
| New PID | **1530387** started ~2026-09-03T23:04:45Z |
| writer_mode | `BATCH_STREAMING_ZSTD` |
| queue_item_contract | `ONE_ITEM_PER_BYBIT_DELTA` |
| bootstrap_persistent_capture | false |
| Phase D 15-min smoke | **Interrupted / never finalized** — no `PHASE_D_SMOKE.json` |
| Overnight | Two real CROSS_IN events; both `research_eligible=false` due to queue drops (see ROOT_CAUSE_REPORT) |
| Live now | PID 1530387 still running; currently COOLDOWN; health may show `queue_drop_count=0` because counters were sink-scoped (bug, now fixed offline) |

---

## What must not be claimed

- No claim that a full 15-minute drop-free smoke passed after restart.
- No fabricated ingress/writer rate tables for an unobserved smoke window.
- Overnight events are **not** a successful validation of the batch writer.

---

## Follow-up after next restart

After loading `NIGHT_DROP_ROOT_CAUSE` fixes, run a real smoke and replace this marker with a complete report containing:

- process_lifetime_queue_drops (monotonic)
- current_writer_alive
- persisted_capture_u_gap_count
- dual-symbol overlap if it occurs
- explicit PASS/FAIL vs enqueued==written and zero drops

Until then, keep this file marked:

```text
RETROSPECTIVE_SMOKE_REPORT_INCOMPLETE
```
