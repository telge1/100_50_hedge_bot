# IMPLEMENTATION_REPORT

## Verdict target

`FULL_OB_RESYNC_CHECKPOINT_READY_RESTART_REQUIRED`

## Changed files (orderbook_analyse)

| File | Change |
|---|---|
| `full_ob_edge_flight_recorder/continuity_contract.py` | **new** contract helpers |
| `full_ob_edge_flight_recorder/manager.py` | reconnect/resync gate, INITIAL/RESYNC records, metrics |
| `full_ob_edge_flight_recorder/capture_plan.py` | continuity metrics + eligibility recompute |
| `full_ob_edge_flight_recorder/event_writer.py` | epoch-aware continuity; `write_resync_checkpoint` |
| `full_ob_edge_flight_recorder/replay.py` | multi-epoch replay |
| `full_ob_edge_flight_recorder/ringbuffer.py` | `clear()` for prebuffer invalidate |
| `on_demand_full.py` | `on_reconnect(reason=…)`, `resync_ready` notify, flush after REST |
| `collector.py` | pass reconnect reason into `full_book.on_reconnect` |
| `tests/test_full_ob_resync_checkpoint_v1.py` | **new** contract tests |
| `tests/test_full_ob_writer_throughput_bootstrap_v1.py` | count only BOOK_DELTA for integrity |

## Not changed live

- Collector PID left running (old binary/code in memory)
- No env change, no prod CH migration, no commit/push

## Checkpoint failure behavior

`checkpoint_persist_failed=true` → `replayable_by_epochs=false` → `research_eligible=false` → awaiting gate remains closed (no silent new epoch).
