# Implementation Report — anchor_retention_v2

## Contract
`full_ob_cache_bridge_anchor_retention_v2`

## Root cause (fixed)
Checkpoint retention equaled delta retention (600s) and freeze selected
latest_at_or_before(max(analysis_start, first_ring_recv)), requiring a checkpoint
at/before the oldest ring delta. Periodic checkpoints lag by up to ~interval, so
after warm-up the oldest checkpoint was younger than the oldest delta -> BOOK_ANCHOR_MISSING.

## Code changes (orderbook_analyse)
- full_ob_cache_bridge/config.py — retention formula, safety margin, derived max checkpoints
- full_ob_cache_bridge/protocol.py — v2 statuses, checkpoint kinds, CONTRACT_ID
- full_ob_cache_bridge/checkpoints.py — INITIAL/RESYNC/PERIODIC, epoch, book hash, longer window
- full_ob_cache_bridge/freeze.py — analysis_start vs payload_replay_start, fail-closed statuses, multi-epoch replay, hash parity
- full_ob_cache_bridge/service.py — retention wiring, reconnect->epoch/resync (no clear), anchor <= analysis_start
- full_ob_cache_bridge/dump.py — analysis/epoch fields in payload
- tests/test_full_ob_cache_bridge_v1.py — fixtures aligned to stricter anchor rule
- tests/test_full_ob_cache_bridge_anchor_retention_v2.py — new deterministic suite

## Retention
- delta_retention_sec: 600
- checkpoint_interval_sec: 60
- safety_margin_sec: 30 (worst-case phase offset ≈ interval (60s) + 30s recv/wall/store skew)
- checkpoint_retention_sec: 690.0
- max_checkpoints_floor: 14

## Anchor selection
Latest checkpoint with checkpoint_ts <= requested_pre_roll_start_ts (analysis_start).
Payload may start earlier at payload_replay_start_ts = anchor_ts.

## Initial / resync
- First book_ready -> INITIAL_CHECKPOINT (forced)
- Reconnect -> begin_resync_epoch() (keep prior checkpoints) -> next ready -> RESYNC_CHECKPOINT
- Missing resync in window -> RESYNC_CHECKPOINT_MISSING (early fail-closed)

## Live note
Running collector PID 1978678 still executes pre-fix code until an authorized restart.
No restart / live freeze / env change performed in this task.
