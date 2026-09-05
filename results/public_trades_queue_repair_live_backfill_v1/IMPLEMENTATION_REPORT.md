# IMPLEMENTATION_REPORT — Public Trades queue repair

## Changes

| File | Change |
|------|--------|
| `signal_generator/bybit/live/public_trade_spool.py` | **New** durable batch WAL (JSON array lines, checksum, atomic ack, segment GC) |
| `signal_generator/bybit/live/trade_buffer.py` | Overflow→spool, retry/backoff, writer_fatal fail-closed, HWMark, larger defaults |
| `signal_generator/bybit/live/collector.py` | Dedicated `_ch_public`, spool_dir wiring, fail-closed stop on writer_fatal |
| `signal_generator/bybit/live/health.py` | Spool/writer metrics in API payload |
| `signal_generator/db/client.py` | Per-client RLock (pre-existing dirty) |
| `scripts/run_live_collector_service.py` | Defaults queue=100000, batch=2000, spool-dir flag |
| `dashboard/stoch_collector_control.py` | Production argv passes new queue/batch/spool |

## Pipeline after fix

```text
WebSocket → compact asyncio queue → batch writer → ClickHouse (source=live)
                 ↘ overflow batch → spool/WAL → replay → Ack
```

- No silent discard when spool is healthy
- Spool/disk exhaustion or writer death → `writer_fatal` → collector ERROR + stop
- Idempotency: `ReplacingMergeTree(symbol, trade_id)` + archive/live overlap safe

## Preserved dirty hunks

Dedicated CH client + direct `insert_trades` live path retained and extended (not overwritten).
