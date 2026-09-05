# PHASE0_REUSE_AUDIT — full_ob_finalized_segment_clickhouse_import_v1

## Verdict of reuse

Reuse existing Flight-Recorder / smoke / BTC-signal building blocks. No second parallel replay stack.

## Reused components (exact)

| Area | Path | Symbols |
|------|------|---------|
| JSONL.zst reader | `orderbook_analyse/.../full_ob_edge_flight_recorder/replay.py` | `_iter_zst_jsonl`, `sha256_file`, `replay_event_directory` |
| Continuity / kinds / book hash | `.../continuity_contract.py` | `CHECKPOINT_KINDS`, `NON_DELTA_KINDS`, `book_content_hash`, `levels_to_str_pairs`, record kind constants |
| Event writer finalization / SHA chain | `.../event_writer.py`, manifests | `segment_sha256`, `previous_segment_sha256`, `continuation_index` |
| Signal isolation contract | `.../signal_analysis_isolation.py` | profile / eligibility / overlap concepts |
| Smoke schema pattern | `results/full_ob_clickhouse_smoke_btc_v1/clickhouse_schema.sql` | `ReplacingMergeTree`, string price/qty tuples |
| Smoke importer patterns | `results/full_ob_clickhouse_smoke_btc_v1/run_smoke.py` | docker/CH insert, parity gates |
| BTC crash importer | `results/btc_full_ob_signal_to_crash_20260904_v1/_run_analysis.py` | `clickhouse_connect`, checkpoint-full levels, compact delta raw |
| Flow reader (read-only join) | `adapter/canonical_flow_reader.py` | trades FQN guidance |

## ID contract frozen for v1

Deterministic `record_id = sha256(source_sha256|ordinal|kind|symbol|fight_event_id|epoch|u|seq)`.

**Not** smoke location-only `packet_sha256` and **not** raw-line SHA alone — both prior approaches documented; v1 uses ordinal+lineage composite for stable cross-segment identity while remaining source-bound via `source_sha256`.

## Not reused / intentionally new

- Persistent import state machine + local JSON resume store
- Segment readiness gates (`OPEN_NOT_ELIGIBLE`, writer FD check)
- Pilot DB `research_full_ob_import_pilot_v1` + canonical views
- CLI `scripts/import_finalized_full_ob_segments.py`

## Forbidden mutations

- `orderbook_analysis`, `research_full_ob_smoke`, `research_full_ob_btc_20260904_signal_analysis`
- Live collector / OI processes
- Source JSONL.zst / open `*.tmp`
