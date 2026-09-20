# TEST_REPORT — anchor_retention_v2

## Suite
```
88 passed in 4.49s
```

## Coverage highlights
- Checkpoint at pre-roll start
- Worst-case ~59.999s phase offset vs equal/longer retention
- Multi-hour eviction simulation
- Startup incomplete
- Exact eviction boundary
- Initial checkpoint before first delta
- Reconnect without / with resync
- Multi-epoch replay
- True u-gap
- Parallel updates + MAX_CONCURRENT=1
- BTC+DOGE isolation
- Payload limit / SHA tamper / book hash mismatch
- Exact parity >=60k levels
- FR / sync / resync / socket / lock-offload regression

## Offline proofs
- retention_boundary_test.json
- multi_epoch_replay_test.json (REPLAY_PARITY=True)
- exact_replay_parity.json (REPLAY_PARITY=True, levels=60000)
- lock_and_memory_benchmark.json (build_ms=20.302, ser_ms=1.839)
