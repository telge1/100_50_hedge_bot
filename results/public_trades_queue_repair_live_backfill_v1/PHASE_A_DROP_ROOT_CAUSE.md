# PHASE_A_DROP_ROOT_CAUSE — Public Trades queue_full

**UTC:** 2026-09-04T20:50:00Z

## Classification

```text
HISTORICAL_QUEUE_FULL_DROPS_UNDER_INSERT_BACKPRESSURE
CURRENTLY_NOT_DROPPING
```

## Evidence (two samples, 120s apart)

| Field | T0 | T0+120s | Delta |
|-------|----|---------|-------|
| `dropped_events` | 493019 | 493019 | **0** |
| `rows_received` | 29188582 | 29197303 | +8721 |
| `rows_inserted` | 28695180 | 28703919 | +8739 |
| `queue_depth` | 5 | 1 | drained |
| `lag_seconds` | ~0.09 | ~0.09 | OK |
| `insert_failures` | 0 | 0 | OK |

**Conclusion:** `493019` is a **lifetime** counter of real discarded WS trades (not a counter bug). New drops are **not** accruing in the current calm window. Last `PUBLIC_TRADE_DROPPED` log burst: **2026-09-04 14h–18h UTC** (queue_depth=5000).

## Process

| Field | Value |
|-------|-------|
| PID | 1661773 |
| Instances | 1 |
| CWD | `Signal_Generator_Ralf/signal_generator_stoch_waves` |
| Start | `nohup` via `scripts/run_live_collector_service.py --enable-public-trades` (systemd unit **not** installed) |
| Symbols | 51 (`universe_tradeable_51.json`) |
| Topics | `publicTrade.{symbol}` |

## Architecture at drop time

| Knob | Value |
|------|-------|
| `queue_maxsize` | **5000** |
| `batch_size` | **500** |
| `flush_interval_s` | 0.5 |
| Drop path | `put_nowait` → `QueueFull` → increment + **discard trade** (no spool) |
| Writer | single asyncio worker → `asyncio.to_thread` CH insert |
| Live code age | process since ~2026-08-21; dirty dedicated-CH / direct-insert hunks **not loaded** |

## Root cause (proven)

1. **Silent discard on `asyncio.QueueFull`** in `trade_buffer.enqueue` — no durable spill.
2. **Queue capacity 5000** too small for 51-symbol publicTrade bursts when ClickHouse insert egress lags.
3. Contributing (historical, addressed in undeployed dirty hunks): shared CH session contention + `skip_existing` SELECTs on live path.

Not OOM. Not a false counter. Not “only three missing calendar days” (see Phase B).

## Fix direction (implemented offline)

- Larger defaults (queue 100k, batch 2k)
- Dedicated CH client + RLock (already dirty)
- Direct live insert (ReplacingMergeTree idempotency)
- Durable overflow spool WAL (batch JSON lines, ack cursor)
- Fail-closed on spool/disk exhaustion / writer death
- Retry+backoff; never silent-drop after fix
