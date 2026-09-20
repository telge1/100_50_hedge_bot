# PHASE0 Audit — Full-OB Cache Bridge Anchor Retention

Generated: 2026-09-05 (read-only; no collector restart)

## Live PIDs (dynamic, read-only)

| Component | Value |
|-----------|-------|
| Service | `bybit-full-ob-raw-archive-btc-doge.service` |
| MainPID | `1978678` (active/running) |
| Process | `.venv/bin/python -m orderbook_analyse.orderbook_v2_live --mode raw-archive-only …` |

OI/Liquidation, Public Trades, Dashboard: not inspected beyond confirming this audit does not touch them.

## 1. Where deltas and checkpoints are produced

| Stream | Owner | Path |
|--------|-------|------|
| Full-OB WS deltas | `FullBookManager` + FR ring | `full_ob_edge_flight_recorder` `BoundedRawRingBuffer` (~600 s) |
| Bridge ring access | FR helpers | `bridge_ring_snapshot` / `bridge_ring_meta` |
| Checkpoints | `FullObCacheBridge.on_book_update` → `CheckpointRing.maybe_store` | Observer on full-book updates (copy under `_book_lock`, store outside) |
| Freeze T0 book | `get_book_snapshot` | `copy_consistent_snapshot()` under `_book_lock` |

## 2. Retention windows (v1 as shipped)

| Buffer | Configured window | Cap |
|--------|-------------------|-----|
| FR delta ring | ~600 s (`window_sec`) | message/byte caps in FR |
| Checkpoint ring | **`window_sec = max_pre_roll_seconds` = 600 s** | `max_checkpoints_per_symbol=12`, 48 MiB/symbol |

**Defect:** checkpoint retention equals delta retention. With periodic interval 60 s, the oldest surviving checkpoint is typically up to ~one interval *after* the oldest surviving delta.

## 3. Ordering / intake sequence

1. WS delta arrives → applied to live full book → appended to FR ring.
2. Observer fires → consistent snapshot copied → `maybe_store` (interval-gated).
3. First checkpoint occurs only after `book_ready` and interval eligibility — **not** forced at initial ready.
4. Freeze selects `latest_at_or_before(max(target_start, first_ring_recv))` then requires `anchor.recv <= first_kept_delta.recv`.

## 4. book_ready / reconnect / resync (v1)

- Checkpoints require `snap.book_ready` and non-null `update_id`.
- `mark_reconnect()` sets reconnect mark **and clears all checkpoints**.
- Freeze treats `reconnect_in_window` as hard fail (`RECONNECT_IN_WINDOW`).
- No `RESYNC_CHECKPOINT` / epoch model in the bridge (FR fight capture has its own INITIAL/RESYNC records; bridge does not reuse them).

## 5. Freeze anchor selection (v1)

```text
target_start = t0 - min(requested, max_pre_roll)
anchor_deadline = max(target_start, first_ring_recv)
anchor = latest_at_or_before(anchor_deadline)
# then build_freeze_bundle also requires anchor.recv <= first_kept_delta.recv
```

When the ring is full, `first_ring_recv ≈ target_start`, so the deadline sits at the oldest delta. No checkpoint exists at or before that time → `BOOK_ANCHOR_MISSING`.

## 6. Why Phase-1B BTC dump had no pre-first-delta checkpoint

Evidence (`btc_anchor_missing_diagnosis.json`):

- Dump `anchor = null`, status `BOOK_ANCHOR_MISSING`
- ~3000 deltas, ~599.8 s pre-roll span
- Live oldest checkpoint ≈ **24.4 s after** `buffer_start`

Root cause is retention/selection, not warm-up time. Waiting cannot create a checkpoint older than the eviction horizon of a 600 s checkpoint window.

## 7. Locks / work under full-book lock

| Work | Under `_book_lock`? |
|------|---------------------|
| `copy_consistent_snapshot` for observer / freeze T0 | Yes (brief) |
| Checkpoint list append / eviction | CheckpointRing `RLock` only |
| `orjson` dump / SHA / socket I/O | No (dump path after freeze copy) |
| Freeze serialization (`write_freeze_dump`) | No |

## 8. Payload / concurrency / SHA

- `MAX_PAYLOAD_BYTES` default 96 MiB (`100663296` in live env)
- `MAX_CONCURRENT=1` via non-blocking freeze lock + min interval
- Payload SHA256 + canonical manifest SHA256 verified by client
- Atomic dump write (`O_EXCL` tmp → replace), mode 0600

## Conclusion

Systematic gap: **checkpoint retention ≤ delta retention** plus **no initial checkpoint ordered before first ring delta** plus **anchor deadline tied to oldest ring delta**. Fix requires `full_ob_cache_bridge_anchor_retention_v2` (longer checkpoint retention, initial/resync checkpoints, analysis-window-based anchor selection, fail-closed statuses).
