# Historical OB-1s Producer Lineage

Status: producer sampling semantics proven empirically and by source code; exact process
binary hash not stored.

- Table: `orderbook_analysis.orderbook_features_1s_v2`.
- Overlap import-manifest rows: 0 (therefore no evidence
  that these rows came from the batch importer).
- `created_at` follows each bucket by about one second, consistent with the live writer.
- Path: WebSocket payload → collector `_ingest_ready` → `LiveSecondClock` →
  `build_event_feature_row`/`compute_features` → `FeatureWriter` → `insert_features`.
- A second producer exists for older history: `pilot.run_pilot` → downloader →
  `parse_day_zip` → the same `insert_features`; its proven bulk window is
  2026-07-19 through 2026-08-17.
- The overlap live writer stopped at about `2026-08-28T16:26:23Z` after a fail-closed
  `queue_full`; this agrees with the table maximum.
- Raw archive receives the same payload before clock ingestion.
- `ts` is exchange event time; `local_receive_ts` is local receive time.
- Book updates apply in source/receive order using `data.u`; snapshot replaces state.
- Static features use the last valid book state processed at wall-clock finalization.
- Mid = `(best_bid+best_ask)/2`; depth L50 is the sum of the first 50 levels.
- The collector-manifest revision is `3f2f18f`; `clock.py`, `dynamics.py`, `features.py`,
  and the relevant collector path have no semantic diff to the inspected current path.
- CH `last_source_ts`/`last_update_seq` identifies an exact Raw terminal event for
  1800/1800 buckets.
- Stored raw `local_receive_ts` alone reconstructs 1796/1800 buckets.
  Four events straddle the timer/callback scheduling boundary; that ordering was not stored.

The earlier `BTC_RAW_AGG_PARITY_DIFFERENT_VALID_SEMANTICS` audit correctly rejected a
whole-second offset and recognized different semantics, but did not test
`local_receive_ts`. Its "continuous vs isolated segment" explanation was incomplete:
segment-chain-vs-alone already passed. Phase 1B closes that gap.
