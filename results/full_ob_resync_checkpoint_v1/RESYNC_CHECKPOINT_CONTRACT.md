# RESYNC_CHECKPOINT_CONTRACT — `full_ob_resync_checkpoint_v1`

## Epochs

```
epoch 0 = INITIAL_CHECKPOINT + BOOK_DELTA*
epoch N = RESYNC_CHECKPOINT + BOOK_DELTA*   (after RESYNC_BOUNDARY)
```

## Record kinds

| Kind | Role | In u+1 check? |
|---|---|---|
| `INITIAL_CHECKPOINT` | Full book seed at event open | resets baseline |
| `BOOK_DELTA` | Bybit WS delta | yes (within epoch) |
| `RESYNC_BOUNDARY` | Documents unobserved transport gap | no |
| `RESYNC_CHECKPOINT` | Full book seed after resync | resets baseline |
| `EVENT_MARKER` | CROSS_IN / PROFILE / … | no |
| `EVENT_END` | Terminal marker | no |

## Required fields (logical)

`fight_event_id`, `continuity_epoch_id`, `record_kind`, `record_ordinal`, `symbol`, `topic` (deltas/checkpoints), `u`/`seq` (book records), exchange `ts`, `cts`, `receive_time_ns`, `resync_reason` (boundary/checkpoint), `segment_index`, `book_hash` (checkpoints).

## Queue order guarantee

During open capture after reconnect:

1. `RESYNC_BOUNDARY`
2. (deltas held in memory — not persisted)
3. `RESYNC_CHECKPOINT` (must succeed)
4. held deltas flushed as `BOOK_DELTA` of new epoch
5. subsequent live deltas

If checkpoint enqueue fails: `checkpoint_persist_failed=true`, gate stays closed, no fake epoch.

## Research flags

```
continuous_capture = no transport gap / no persisted intra-epoch gaps / no queue drops / checkpoint ok
replayable_by_epochs = every epoch has valid seed and exact intra-epoch replay
research_eligible = genuine CROSS_IN AND continuous_capture AND replayable_by_epochs AND no queue/apply/persisted gaps
```

Therefore after a successful reconnect checkpoint:

```
replayable_by_epochs=true
continuous_capture=false
research_eligible=false
```
