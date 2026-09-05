# PHASE0_CODE_AUDIT — Full-OB reconnect → Flight Recorder

## Call chain (proven)

```
collector._session
  stale_data_sec without topic → DeadConnection("stale_market_data")
collector.run reconnect backoff
  full_book.on_reconnect(reason=last_error)
    book.clear + sync_buffer.clear + reconnect_count++
    FR observer phase="reconnect"  → RESYNC_BOUNDARY (+ gate)
  subscribe_all / full_book.tick
    subscribe_symbol → _apply_rest_snapshot (thread)
      align REST ↔ buffer → book_ready
      FR observer phase="resync_ready" → RESYNC_CHECKPOINT then flush held deltas
  WS handle_message
    !book_ready → phase=buffer (held by FR while awaiting checkpoint)
    book_ready → phase=live → BOOK_DELTA (annotated with continuity_epoch_id)
  NonBlockingDeltaSink.try_put (long-lived queue/thread)
  ActiveEventWriter.append_delta_batch (orjson+zstd off book lock)
```

## Key files

| Step | Location |
|---|---|
| Stale watchdog | `collector.py` `_session` (~708–711) |
| Reconnect notify | `on_demand_full.py` `on_reconnect` |
| REST align | `on_demand_full.py` `_apply_rest_snapshot` |
| Observer | `on_demand_full.py` `_notify_observers` |
| Epoch gate | `full_ob_edge_flight_recorder/manager.py` |
| Contract | `continuity_contract.py` |
| Writer continuity | `event_writer.py` `_note_continuity` |
| Replay | `replay.py` multi-epoch |

## Pre-fix gap

- `rest_full_snapshot.json.zst` only at event start
- Mid-event REST resync updated RAM only
- RESYNC/U_GAP markers were plan-only (not streamed)
- Replay always seeded from single initial snapshot → `PERSISTED_U_GAP` / non-self-contained

## Lock / queue invariants (preserved)

- No JSON/zstd/disk/CH under `_book_lock`
- Long-lived `NonBlockingDeltaSink` across segment rotate
- Checkpoint enqueue **before** post-resync deltas (hold gate)
