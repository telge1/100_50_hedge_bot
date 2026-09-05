# SOURCE_LINEAGE_CONTRACT

## Source of truth (immutable)

1. Flight Recorder `full_ob_raw_deltas.jsonl.zst` (finalized only)
2. REST / initial snapshot + resync checkpoints in-stream
3. `event_manifest.json` segment SHA chain (`segment_sha256`, `previous_segment_sha256`)
4. Segment / event manifests under cont_* dirs

Importer **never** modifies or deletes sources. Open `*.tmp` are never read for import.

## Lineage columns on every record

- `source_path`, `source_sha256`
- `segment_id` = sha256(fight_event_id|continuation_index|source_sha256)
- `record_id` = sha256(source_sha256|ordinal|kind|symbol|event|epoch|u|seq)
- `raw_payload_hash`, `canonical_payload_hash`
- `continuity_epoch_id`, `record_ordinal`

## Checkpoint / epoch rules

- Epoch begins with `INITIAL_CHECKPOINT` or `RESYNC_CHECKPOINT`
- Markers / boundaries do not participate in `u+1` delta chaining
- Deltas must not cross a resync boundary without new checkpoint seed
- Missing checkpoint ⇒ event not independently replayable
- Reconnect ⇒ `continuous_capture=false`; resync restores epoch replayability, not missing orderflow

## External context (read-only)

Joins only:

- `orderbook_analysis.public_trades_canonical`
- OI / liquidation sources when present

Missing OI/liq ⇒ `context_coverage=PARTIAL` (never claim Full-OB event “complete context”).
