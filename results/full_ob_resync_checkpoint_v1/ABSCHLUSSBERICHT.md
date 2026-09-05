# ABSCHLUSSBERICHT — Full-OB Resync Checkpoint v1

## 1. Verdict

**`FULL_OB_RESYNC_CHECKPOINT_READY_RESTART_REQUIRED`**

## 2. Root Cause (addressed)

Reconnect after `stale_market_data` cleared the RAM book and REST-resynced, but the Flight Recorder did not persist a mid-event full-book seed. Offline replay remained tied to the initial snapshot → event not self-contained across the gap.

## 3. Changed files / functions

See `IMPLEMENTATION_REPORT.md`. Core: `FullObEdgeFlightRecorder` reconnect/resync gate; `OnDemandFullBookManager.on_reconnect` / `resync_ready`; multi-epoch `replay_event_directory`.

## 4. Record / epoch contract

`full_ob_resync_checkpoint_v1` with `INITIAL_CHECKPOINT` / `RESYNC_BOUNDARY` / `RESYNC_CHECKPOINT` / `BOOK_DELTA` / `EVENT_MARKER` — details in `RESYNC_CHECKPOINT_CONTRACT.md`.

## 5. Queue order

`BOUNDARY → CHECKPOINT → held deltas → live deltas` on the same long-lived sink. No post-resync delta persisted before checkpoint.

## 6. Checkpoint failure

Fail-closed: `checkpoint_persist_failed`, gate stays closed, `research_eligible=false`, `replayable_by_epochs=false`.

## 7. Health metrics

Plan/process fields include `transport_reconnect_count`, `resync_*`, `continuity_epoch_count`, `continuous_capture`, `replayable_by_epochs`, `apply_epoch_u_gap_count`, `persisted_capture_u_gap_count`, writer queue drops (lifetime counters preserved across segment rotate).

## 8. Historical replay

`historical_gap_regression.json`: 2 epochs, `continuous_capture=false`, `replayable_by_epochs=true`, `research_eligible=false`, no invented `u+1`.

## 9. ClickHouse parity

Isolated multi-epoch table: logical idempotency without OPTIMIZE; epoch filter required — `clickhouse_multi_epoch_parity.json`.

## 10. Lock / socket performance

`checkpoint_lock_performance.json`: 30k×2 levels copy/serialize measured; contract keeps JSON/zstd off `_book_lock`.

## 11. Research eligibility

Reconnect with successful checkpoint ⇒ `replayable_by_epochs=true` but **`research_eligible=false`** (`continuous_capture=false`). Matrix: `research_eligibility_matrix.json`.

## 12. Tests

**78 passed** across FR / night-drop / sync / full-book / new resync suite.

## 13–14. Live PIDs

Collector **1565672** and OI **147111** unchanged (not restarted).

## 15. Live risk / next step

Offline code is ready but **not loaded** in the running process. Next step requires **explicit approval** for exactly one controlled collector restart. Do not restart until approved.
