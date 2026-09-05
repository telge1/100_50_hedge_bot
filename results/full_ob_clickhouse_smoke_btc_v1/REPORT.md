# Full-OB ClickHouse Smoke — BTCUSDT

## Verdict

`FULL_OB_CLICKHOUSE_SMOKE_EXACT_PARITY`

## Source

- Event: `BTCUSDT_20260904T080534Z_1fd9a66d36`
- Segment: finalized primary (`continuation_index=0`)
- Source file: `/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder/BTCUSDT/2026-09-04/BTCUSDT_20260904T080534Z_1fd9a66d36/full_ob_raw_deltas.jsonl.zst`
- Snapshot seed: `/home/telgenbuescher/projects/orderbook_analyse/data/orderbook_raw_shadow/full_ob_edge_flight_recorder/BTCUSDT/2026-09-04/BTCUSDT_20260904T080534Z_1fd9a66d36/rest_full_snapshot.json.zst`
- Topic: `orderbook.full.BTCUSDT`
- Smoke window: ~300s applied deltas after REST seed (cut before later event u-gaps)

## Phase A — Topic proof

- FULL_TOPIC_PROVEN=True
- NOT_OB200_PROVEN=True
- NOT_OB1000_PROVEN=True
- Live confirmed_topics includes `orderbook.full.BTCUSDT`
- Stored packet topic exactly `orderbook.full.BTCUSDT`
- REST snapshot levels: bids=39557, asks=24214 (>>1000 ⇒ not OB1000)
- Live BTC runtime: book_ready=True, raw_bids=40333, raw_asks=23508
- Contract: depth=0, levels_capped_at_1000=false

## Import / Parity

- Source packet rows (incl. snapshot+markers): 1514
- ClickHouse logical packet rows: 1514
- Parsing rejects: 0
- Duplicate packet groups (physical pre-2nd-import): 0
- Source feed u-gaps (smoke replay): 0
- Persisted capture u-gaps (applied stream): 0
- Database replay u-gaps: 0
- Final u/seq source: 4349047 / 805049768223
- Final u/seq database: 4349047 / 805049768223
- Source book hash: `14f793b85f98caedee435b0f86bc5c6a44bb795852e83cfde571b87440bc2175`
- Database book hash: `14f793b85f98caedee435b0f86bc5c6a44bb795852e83cfde571b87440bc2175`
- Bid/Ask levels source: 39477 / 24393
- Bid/Ask levels database: 39477 / 24393
- Crossed (DB): False

## Idempotency

- Passed: True
- Logical unique packets after 2nd import+OPTIMIZE: 1514 (expected 1514)
- Physical rows after 2nd import (pre-optimize may be higher): 3028 → after OPTIMIZE FINAL: 1514
- Final hash unchanged: True

## Analysis fitness

- Runnable: True
- Deletes qty=0: 102417
- Refill candidates: 5
- See `example_analysis_queries.sql` and `analysis_results.json`

## Safety

- Collector PID 1565672 alive before/after: True/True (untouched)
- OI PID 147111 alive before/after: True/True (untouched)
- Isolated DB only: `research_full_ob_smoke` (no `orderbook_analysis` writes)
- No open `.tmp` files read
- No commit / push

## Gates

```json
{
  "source_packet_count == clickhouse_packet_count": true,
  "rejected_packet_count == 0": true,
  "duplicate_packet_count == 0": true,
  "source_feed_u_gap_count == 0": true,
  "persisted_capture_u_gap_count == 0": true,
  "database_replay_u_gap_count == 0": true,
  "source_final_u == database_final_u": true,
  "source_final_seq == database_final_seq": true,
  "source_book_hash == database_book_hash": true,
  "source_best_bid == database_best_bid": true,
  "source_best_ask == database_best_ask": true,
  "source_bid_level_count == database_bid_level_count": true,
  "source_ask_level_count == database_ask_level_count": true,
  "database_book_crossed == false": true,
  "checkpoint_exact": true,
  "ob_packet_counts_match": true
}
```

## Artifacts

All under `results/full_ob_clickhouse_smoke_btc_v1/`.
