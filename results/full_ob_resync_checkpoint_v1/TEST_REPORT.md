# TEST_REPORT — full_ob_resync_checkpoint_v1

## New tests

`tests/test_full_ob_resync_checkpoint_v1.py` — **9 passed**

- historical gap regression (4350204 → checkpoint → 4350353)
- checkpoint hash tamper fail-closed
- queue-full checkpoint fail-closed
- ISO marker excluded from u continuity
- research eligibility matrix basics
- book hash stability
- ~60k level copy budget
- reconnect without open event clears prebuffer
- multi-reconnect gate / held deltas / new epoch

## Regression suites

```
tests/test_full_ob_resync_checkpoint_v1.py
tests/test_full_ob_edge_flight_recorder_v1.py
tests/test_full_ob_edge_capture_timing_v1.py
tests/test_night_drop_root_cause_v1.py
tests/test_full_ob_socket_lock_offload.py
tests/test_full_ob_sync_contract.py
tests/test_full_ob_writer_throughput_bootstrap_v1.py
tests/test_orderbook_v3_full_book_on_demand.py
```

**Result: 78 passed** (final full run).

## ClickHouse smoke (isolated)

`research_full_ob_smoke.full_ob_multi_epoch_packets_v1`

- 2 imports → physical 10 / logical 5
- epochs=2
- analysis must filter `record_kind='BOOK_DELTA'` + `continuity_epoch_id`

See `clickhouse_multi_epoch_parity.json`.
