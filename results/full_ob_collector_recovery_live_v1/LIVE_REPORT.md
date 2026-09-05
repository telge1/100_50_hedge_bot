# LIVE_REPORT — Full OB Collector Recovery

**UTC:** 2026-09-04T20:05:33Z

## Verdict

```text
FULL_OB_LIVE_VALIDATION_FAILED
```

**Failure reason:** `['FLIGHT_RECORDER_DISABLED_BY_START_SCRIPT_ENV']`

All listed hard live gates passed for Full-Book socket path. Capture path failed because
`OB_V3_FULL_OB_FLIGHT_RECORDER_ENABLE=false` in PID 1810262 (start-script env override).
No second restart was performed (task constraint). Start script patched offline for next start.

## Proofs

```text
FULL_TOPIC_PROVEN=true
NOT_OB200_PROVEN=true
NOT_OB1000_PROVEN=true
```

## Live Gates

```text
SINGLE_COLLECTOR_INSTANCE=true\nBTC_FULL_TOPIC_ACTIVE=true\nDOGE_FULL_TOPIC_ACTIVE=true\nBTC_BOOK_READY=true\nDOGE_BOOK_READY=true\nU_PROGRESS=true\nSEQ_MONOTONIC=true\nSOURCE_GAPS=0\nQUEUE_DROPS=0\nWRITER_ERRORS=0\nSOCKET_DEPTH0_UNCAPPED=true\nSOCKET_TIMEOUTS=0\nBOOK_NOT_CROSSED=true\nBOOTSTRAP_FILE_CREATED=false\nOB1000_REGRESSION_PASS=true\nOB200_REGRESSION_PASS=true\nOI_LIQ_PROCESS_UNCHANGED=true\nPUBLIC_TRADES_PROCESS_UNCHANGED=true\nDASHBOARD_PROCESS_UNCHANGED=true\n```

## Smoke summary (T+0 … T+20)

| Metric | BTCUSDT | DOGEUSDT |
|--------|---------|----------|
| Topic | orderbook.full.BTCUSDT | orderbook.full.DOGEUSDT |
| Book ready | true | true |
| Levels (last) | 38232/27082 | 5056/14201 |
| u first→last | [4557367, 4563372] | [4557054, 4563059] |
| seq first→last | [805402305088, 805408688642] | [346155202007, 346159002868] |
| Source gaps | 0 | 0 |
| levels_capped_at_1000 | false | false |

- Queue drops: **0**
- Writer errors: **0**
- Reconnects (health): 0 across samples
- OB200 permanent levels: 200/200 both symbols
- OB1000 regression acquire: pass
- Socket depth=0 uncapped: pass
- Bootstrap FR files created: **false** (FR disabled)
- Watcher / Ringbuffer / Signal-Registry: **inactive** (FR disabled)

## Processes

| Role | PID | Status |
|------|-----|--------|
| Full OB (old) | 1692334 | dead (crash) |
| Full OB (new) | 1810262 | alive |
| OI/Liq | 1795773 | unchanged alive |
| Public Trades | 1661773 | unchanged alive |
| Dashboard | 1780509 | unchanged alive |

## Importer

Full-OB ClickHouse importer: **not running** (left disabled). Stale `.tmp` not imported.
