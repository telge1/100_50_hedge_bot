# LIVE_REPORT — OI/Liq Resilient Cutover

**UTC:** 2026-09-04T18:28:35Z  
**Verdict:** `OI_LIQ_LIVE_VALIDATION_FAILED`

## Restart

| Item | Value |
|------|-------|
| Method | `systemctl --user` stop/start (SIGTERM, TimeoutStopSec=60) |
| Old PID | 147111 |
| New PID | 1786328 |
| Instances | 1 |
| Systemd NRestarts | 0 |
| Unit | `bybit-oi-liquidation-collector.service` (`Restart=on-failure`) |

## Hard gates

```
SINGLE_INSTANCE=true
WRITER_ALIVE=true
HEARTBEAT_ALIVE=true
OI_SOURCE_PROGRESS=true
OI_DB_PROGRESS=true
QUEUE_DROPS=0
WRITER_ERRORS=7
SESSION_IS_LOCKED_ERRORS=0
PERSISTENCE_LAG_WITHIN_CONTRACT=true
LIQUIDATION_SUBSCRIPTION_ACTIVE=true
SPOOL_HEALTHY=false
```

**FAILED_GATES:** WRITER_ERRORS, SPOOL_HEALTHY

## Smoke (15 min, 16 samples)

- OI 5s rowcount: 12361869 → 12367106 (Δ 5237)
- OI 5s max bucket: 2026-09-04 18:07:00 → 2026-09-04 18:21:15
- Persistence lag max: 0.833s
- WS reconnects (end): 28
- CLOSE_WAIT max during smoke: 1
- Spool meta.json.tmp race log hits post-restart: 85
- Symbols checked fresh: BTCUSDT, DOGEUSDT, ETHUSDT, SOLUSDT, XRPUSDT

## Root cause of gate failure

Concurrent spool `meta.json.tmp` → `meta.json` rename races raise `FileNotFoundError`. This increments `writer_error_count`, disconnects/reconnects the WebSocket, and intermittently shows CLOSE_WAIT / YELLOW health. OI→ClickHouse progress still occurred via retries, but hard gates `WRITER_ERRORS=0` and `SPOOL_HEALTHY` failed. **No second restart performed.**

## Liquidations

- Status: `LIVE_EVENTS_OBSERVED`
- Subscription active: true
- Shared writer proven via OI path
- No synthetic events written

## Phase F (OI 5m)

- **Not executed** (smoke failed)
- Hist still ends at 2026-09-04 17:35:00 / 2026-09-04 17:35:00
- Estimated missing closed buckets/symbol since 17:35Z: 9

## Fail-fast (code/tests, no live injection)

```json
{
  "WriterDeadError_defined": true,
  "autogenerate_session_id_false": true,
  "DurableSpool_used": true,
  "health_atomic_writer": true,
  "is_alive_on_writer": true,
  "SESSION_IS_LOCKED_handled": true,
  "systemd_Restart": true,
  "spool_ack_never_delete_unacked": true,
  "tests_passed": 33,
  "live_fault_injection": false
}
```

## Protected processes

- Dashboard 1780509 unchanged: True
- Public trades 1661773 unchanged: True
- Full OB stopped: True
- `DESTRUCTIVE_ACTIONS_EXECUTED=false`
