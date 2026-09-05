# LIVE_DAMAGE_AUDIT — Spool meta.json.tmp race

**UTC:** 2026-09-04T18:41:23Z  
**Live PID (untouched):** `1786328`  
**Restart by this work package:** none

## Process

| Check | Result |
|-------|--------|
| PID 1786328 alive | yes |
| Instance count | 1 |
| Systemd NRestarts | 0 (no restart loop) |
| Writer alive | yes |
| Queue drops | 0 |
| writer_error_count | rising during race (observed 7→14); stable in short windows when reconnect pauses |
| OI DB timestamps | events continue; 5s buckets intermittently stall during WS reconnect storms, then resume |
| Orphan `meta.json.tmp` | none at audit time (race leaves ENOENT, not leftover tmp) |
| Spool segments | 3 JSONL files; sequential seqs present; unacked residual small |
| Spool replayable | yes (`iter_unacked` / restart replay path) |

## Seven-plus writer errors — operations

All post-restart `insert attempt … failed for open_interest_events` warnings share:

```text
[Errno 2] No such file or directory: '.../spool/meta.json.tmp' -> '.../spool/meta.json'
```

No separate Traceback bodies for these warnings (logged as `exc` string from `os.replace`).  
Pre-restart `SESSION_IS_LOCKED` tracebacks exist in the same log file but are **not** post-cutover.

Mechanism: ClickHouse insert often **succeeded**, then `ack_through()` → `_persist_meta()` raced with concurrent `append()` on the **shared** `meta.json.tmp` path. Loser hit ENOENT; writer counted `insert_errors` and **retried the whole insert**, producing physical OI event duplicates.

WS path: same ENOENT raised from `enqueue()`→`append()` and was treated as reconnectable `OSError`, causing disconnect/reconnect churn.

## Spool ↔ DB

See `spool_db_reconciliation.csv` and `integrity_classification.json`.

- Segment records contiguous; ack cursor near tip; small unacked residual.
- OI events since restart: physical extras vs `uniqExact(event_key)`.
- Liquidations: exact event_key parity (no extras).
- OI 5s: no extras on `(symbol, bucket_time)`.

## Live originals

**Not modified** (read-only audit). Offline fix is in worktree code only; live process still runs pre-fix bytecode in memory.
