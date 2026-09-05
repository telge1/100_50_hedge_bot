# LIVE_REPORT — Full OB Flight Recorder Enabled Restart

**UTC:** 2026-09-04T20:28:44Z

## Verdict

```text
FULL_OB_COLLECTOR_LIVE_HEALTHY_CAPTURE_READY
```

## Effective FR proof

```text
FLIGHT_RECORDER_ENV_TRUE=true
FLIGHT_RECORDER_RUNTIME_ENABLED=true
FLIGHT_RECORDER_SYMBOLS=BTCUSDT,DOGEUSDT
```

## Live Gates

```text
SINGLE_COLLECTOR_INSTANCE=true\nFLIGHT_RECORDER_RUNTIME_ENABLED=true\nBTC_FULL_TOPIC_ACTIVE=true\nDOGE_FULL_TOPIC_ACTIVE=true\nBTC_BOOK_READY=true\nDOGE_BOOK_READY=true\nU_PROGRESS=true\nSEQ_MONOTONIC=true\nSOURCE_GAPS=0\nQUEUE_DROPS=0\nWRITER_ERRORS=0\nOBSERVER_ERRORS=0\nRINGBUFFER_GROWING=true\nWATCHER_ACTIVE=true\nSIGNAL_REGISTRY_ACTIVE=true\nBOOTSTRAP_FILE_CREATED=false\nSOCKET_DEPTH0_UNCAPPED=true\nSOCKET_TIMEOUTS=0\nBOOK_NOT_CROSSED=true\nPROFILE_EVICTION_CRASH=0
```

## Socket / Full-Book Proofs

```text
FULL_TOPIC_PROVEN=true
NOT_OB200_PROVEN=true
NOT_OB1000_PROVEN=true
DEPTH0_UNCAPPED=true
SOCKET_TIMEOUTS=0
BOOK_NOT_CROSSED=true
OB1000_REGRESSION_PASS=true
OB200_REGRESSION_PASS=true
```

## Prebuffer at T+10m

```text
BTC_PREBUFFER_COVERAGE_SECONDS={g['proofs']['BTC_PREBUFFER_COVERAGE_SECONDS']:.3f}
DOGE_PREBUFFER_COVERAGE_SECONDS={g['proofs']['DOGE_PREBUFFER_COVERAGE_SECONDS']:.3f}
```

(Target >=590; measured ~600s — full ringbuffer window.)

## Smoke summary

| Metric | BTCUSDT | DOGEUSDT |
|--------|---------|----------|
| Topic | orderbook.full.BTCUSDT | orderbook.full.DOGEUSDT |
| Levels (T+12) | {last['btc_bids']}/{last['btc_asks']} | {last['doge_bids']}/{last['doge_asks']} |
| u | {g['metrics']['btc_u'][0]}→{g['metrics']['btc_u'][1]} | {g['metrics']['doge_u'][0]}→{g['metrics']['doge_u'][1]} |
| Coverage T+12 | {last['btc_cov']} | {last['doge_cov']} |
| Lifecycle | {boot.get('lifecycle',{}).get('BTCUSDT')} | {boot.get('lifecycle',{}).get('DOGEUSDT')} |
| Bootstrap | {boot.get('bootstrap_status',{}).get('BTCUSDT')} | {boot.get('bootstrap_status',{}).get('DOGEUSDT')} |

- Queue drops / writer errors / observer errors / eviction crashes: **0**
- Genuine signal during smoke: **{boot.get('genuine_signal_observed')}** (not required)
- `BOOTSTRAP_FILE_CREATED=false`
- Active profile watches: **{w.get('ACTIVE_PROFILE_WATCH_COUNT')}** (registry enabled; no live profile edges required)

## Processes

| Role | PID | Status |
|------|-----|--------|
| Full OB (pre) | 1810262 | stopped via SIGTERM |
| Full OB (post) | 1817696 | alive, FR enabled |
| OI/Liq | 1795773 | unchanged |
| Public Trades | 1661773 | unchanged |
| Dashboard | 1780509 | unchanged |

Importer: still **disabled**. `DESTRUCTIVE_ACTIONS_EXECUTED=false`
