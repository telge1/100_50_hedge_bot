# Live 1m Collector Architecture

## Reused (no second REST pipeline)

| Component | Role |
| --------- | ---- |
| `bybit/history.py` | `BybitHistoryClient.fetch_closed_1m`, `normalize_kline`, UTC/`[start,end)` |
| `db/candles.py` | `Candle1m`, batch `insert_candles`, FINAL reads |
| `bybit/universe.py` | optional symbol list from `config/universe_100.json` |
| `candles_1m` ReplacingMergeTree | analytical uniqueness `(exchange,symbol,interval,open_time)` |

## New

| Path | Role |
| ---- | ---- |
| `bybit/live/recovery.py` | CH last-candle → REST gap fill via **same** HistoryClient |
| `bybit/live/ws_kline.py` | Bybit public linear WS `kline.1.{symbol}` + ping/pong |
| `bybit/live/health.py` | Collector state + metrics |
| `bybit/live/collector.py` | STARTING→RECOVERING→LIVE loop, reconnect, stale symbols |
| `scripts/run_bybit_live_1m_collector.py` | CLI |
| `scripts/smoke_bybit_live_collector.py` | recovery smoke |
| `deploy/systemd/bybit-live-1m-collector.service` | process supervisor (layer 2) |

## Flows

```text
START / RECONNECT
  → RECOVERING (ClickHouse SoT)
  → REST gap backfill (history.py)
  → validate FINAL
  → LIVE (WebSocket closed candles only)
```

WebSocket `confirm=true` → insert `source=bybit_live`.
In-progress ticks (`confirm=false`) stay in memory only.

## Dashboard / control preflight

See [LIVE_COLLECTOR_DASHBOARD_CONTRACT.md](LIVE_COLLECTOR_DASHBOARD_CONTRACT.md).

Editable live symbols: `config/live_universe.json` (no hardcoded coin list).

## Integrated service (shadow)

```bash
python scripts/run_live_collector_service.py \
  --live-universe config/live_universe.json \
  --api-host 127.0.0.1 \
  --api-port 8787
```

Control API (localhost only):

- `GET /api/collector/status`
- `GET|POST /api/collector/desired_state`
- `GET /api/signals?symbol=&start=&end=&timeframe=`

Desired state file: `results/live_collector/desired_state.json`  
`RUNNING` → collector runs / restarts on crash  
`STOPPED` → intentional stop (no auto-restart)

Startup order: `STARTING → RECOVERING (tail + FILL_MISSING_RANGES) → signal catch-up → CONNECTING → SUBSCRIBING → LIVE`

## Candle universe vs signal demand (not live-activated)

Two sets:

| Set | Source | BTCUSDT |
| --- | ------ | ------- |
| `candle_universe` | optional `--candle-universe` JSON (`config/universe_tradeable_51.json`) | allowed (candles only) |
| `signal_demand` | `results/live_collector/demand_symbols.json` | still blocked |

Until rollout, **do not** pass `--candle-universe`. The running service stays demand-only (currently ADAUSDT). `live_universe.json` still rejects BTC.

WS subscribe uses chunks of 10 topics; reconnect resubscribes every candle symbol.

### Rollout (do not run yet)

```bash
# after process stop/start window
python scripts/run_live_collector_service.py \
  --live-universe config/live_universe.json \
  --candle-universe config/universe_tradeable_51.json \
  --api-host 127.0.0.1 --api-port 8787 --default-desired RUNNING
```

Rollback: omit `--candle-universe` (demand-only ingest). Do not rewrite `demand_symbols.json`.

### Resource sketch (51 candles, demand still 1 signal)

- REST restart: ~51 × trailing+120m; with 0.05s pause ≈ few seconds if CH is current; 30d internal repair is the worst case.
- WS: 51 topics, 6 subscribe chunks, one public linear connection (under typical 200-topic cap).
- Inserts: 51 closed bars/minute + 0.5s buffer.
- RAM/CPU: forming ticks negligible; signal workers stay on demand symbols only.

