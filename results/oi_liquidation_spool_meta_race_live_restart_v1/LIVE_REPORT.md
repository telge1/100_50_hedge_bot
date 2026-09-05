# LIVE_REPORT — Spool Meta Fix Restart

**UTC:** 2026-09-04T19:25:49Z  
**Verdict:** `OI_LIQ_RESILIENT_COLLECTOR_LIVE_HEALTHY_EXACTLY_ONCE`

## Restart

| Item | Value |
|------|-------|
| Method | `systemctl --user stop` + `start` (SIGTERM) |
| Old PID | 1786328 |
| New PID | 1795773 |
| Instances | 1 |
| Systemd NRestarts | 0 |

## Spool recovery

- Meta generation present (new code loaded): yes (`generation` advancing 2402 → 33221)
- Ack monotonic: yes (34815 → 64508)
- Orphan tmp collisions: 0
- Meta ENOENT / old shared tmp race: 0
- Ack-failure reinserts: 0
- Writer errors during smoke: 0

## Hard gates

```
SINGLE_INSTANCE=true
AUTOMATIC_RESTARTS_DURING_SMOKE=0
WRITER_ALIVE=true
WEBSOCKET_ALIVE=true
OI_SOURCE_PROGRESS=true
OI_DB_PROGRESS=true
QUEUE_DROPS=0
WRITER_ERRORS=0
META_RACE_ERRORS=0
SPOOL_ACK_MONOTONIC=true
ACK_FAILURE_REINSERTS=0
LOGICAL_DUPLICATES=0
SESSION_IS_LOCKED_ERRORS=0
PERSISTENCE_LAG_WITHIN_CONTRACT=true
LIQUIDATION_SUBSCRIPTION_ACTIVE=true
```

## OI end-to-end (20 min)

- oi5s max: 2026-09-04 18:46:55 → 2026-09-04 19:06:55 (Δ rows 12240)
- Symbols fresh: BTC/DOGE/ETH/SOL/XRP
- Path proven: receive → spool append (generation↑) → CH insert (rowcounts↑) → ack (last_acked↑)

## Liquidations

- Status: `LIVE_EVENTS_OBSERVED`
- Max event: 2026-09-04 19:24:41.865000

## Duplicate audit

| Period | Extra physical vs event_key |
|--------|------------------------------|
| Historical pre meta-fix | 217 (known) |
| Since this restart cutoff | 0 |

```
NEW_UNINTENDED_PHYSICAL_DUPLICATES=0
LOGICAL_DUPLICATES=0
ACK_FAILURE_REINSERTS=0
```

## OI-5m catch-up

- Window: 2026-09-04T17:40:00Z → 2026-09-04T19:20:00Z
- Inserted: 42 (21 BTC + 21 DOGE)
- Final: BTC_MISSING=0 DOGE_MISSING=0 WOULD_INSERT=0
- Hist max now: 2026-09-04 19:20:00 / 2026-09-04 19:20:00

## Protected

- Dashboard 1780509: True
- Public trades 1661773: True
- Full OB stopped: true
- `DESTRUCTIVE_ACTIONS_EXECUTED=false`
