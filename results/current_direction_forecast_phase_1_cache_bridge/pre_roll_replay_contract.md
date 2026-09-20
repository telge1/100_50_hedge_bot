# Pre-Roll Replay Contract

**Generated (UTC):** `2026-09-05T15:12:26Z`

## Proof: deltas alone + live T0 book are NOT historically replayable

1. `BoundedRawRingBuffer` stores compact **delta envelopes only** (no full snapshot).
2. FR capture uses `last_rest_snapshot` + flushed deltas; REST seed is typically from subscribe/resync, **not** from ringbuffer start.
3. After hours of uptime, REST.`u` and first ringbuffer delta.`u` are discontinuous → `DeltaOutcome.GAP` under enforced continuity.
4. Therefore: **raw deltas + only current T0 book ≠ causal pre-roll history**.

## Chosen solution: B — periodic RAM book checkpoints

- While bridge is enabled, store compact full-book checkpoints about every 60s (configurable), capped by count/bytes.
- Freeze selects latest checkpoint with `receive_time_ns <= pre_roll_start`.
- Replay = `apply_snapshot(anchor)` then contiguous `apply_delta` for deltas with `receive_time_ns <= T0` and `u > anchor.u`.
- `data_complete=true` only if replay succeeds and gates pass.
- `replay_capability=RAW_FULL_BOOK_REPLAY` when complete.

## Not chosen

- **A** reuse FR REST-only without new checkpoints — not causal for arbitrary `--now`.
- **C** feature-history-only — would set `RAW_FULL_BOOK_REPLAY=false`; deferred.

## Fail-closed gates

`BOOK_ANCHOR_MISSING`, `SEQUENCE_GAP`, `BUFFER_OVERFLOW`, `RECONNECT_IN_WINDOW`, `DATA_STALE`, `PRE_ROLL_TOO_SHORT`, `BUFFER_NOT_WARM`.
