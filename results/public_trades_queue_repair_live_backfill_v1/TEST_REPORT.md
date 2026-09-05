# TEST_REPORT — Public Trades queue repair

```text
pytest tests/unit/test_public_trade_queue_spool.py \
       tests/unit/test_clickhouse_client_thread_safety.py \
       tests/unit/test_public_trades_7d.py
→ 24 passed
```

Covered (mapped to required gates):

1. Spool roundtrip + ack
2. Corrupt tail fail-closed
3. Queue overflow → spool (QUEUE_DROPS=0)
4. Insert retry then success
5. Crash between insert and ack → replay
6. Duplicate trade_id inserts (logical ReplacingMergeTree parity)
7. 51-symbol burst / peak multipliers
8. Exhausted retries → spool + writer_fatal
9. Dedicated CH client thread safety
10. Existing public-trades regressions (updated for no-silent-drop)

```text
QUEUE_DROPS=0
WRITER_ERRORS=0
SOURCE_DB_LOGICAL_PARITY=true  # via ReplacingMergeTree + trade_id tests
```
