# Signal Generator — Stochastic / Wave (data foundation)

ClickHouse-backed store for closed 1m Bybit candles, generated signals, and
signal outcomes. The signal generator itself is **not** implemented yet.

## Architecture

```text
Bybit History
      ↓
candles_1m
      ↑
Bybit Live Collector

candles_1m
      ↓
Stochastic/Wave Generator
      ↓
signals
      ↓
Dashboard

signals
      ↓
Outcome Evaluation
      ↓
signal_outcomes
```

## Project layout

```text
signal_generator_stoch_waves/
├── config/                  # reserved
├── docs/DATABASE.md         # schema & HTF design decisions
├── migrations/              # idempotent SQL
├── scripts/
│   ├── setup_clickhouse.py
│   ├── smoke_clickhouse.py
│   └── backfill_bybit_history.py
├── src/signal_generator/
│   ├── config.py
│   ├── timeframes.py        # deterministic 1m → HTF aggregation
│   ├── bybit/               # historical kline fetch + quality audit
│   └── db/                  # thin ClickHouse repositories
├── results/                 # smoke / audit artifacts
└── tests/
    ├── unit/
    └── integration/
```

## ClickHouse

- **Instance:** same local Docker ClickHouse as other projects (`127.0.0.1:8123`)
- **Database:** dedicated `signal_generator` (not mixed into `orderbook_analysis`)
- **Client:** `clickhouse-connect` (HTTP)
- **Config:** ENV vars — see `.env.example`

```bash
cp .env.example .env
# set CLICKHOUSE_USER / CLICKHOUSE_PASSWORD to the shared instance credentials
```

### Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
python scripts/setup_clickhouse.py
```

Safe to re-run: only `CREATE IF NOT EXISTS` (no DROP/TRUNCATE).

### Smoke tests

```bash
python scripts/smoke_clickhouse.py

# Historical 1m backfill smoke (linear USDT perps)
python scripts/backfill_bybit_history.py \
  --symbols DOGEUSDT APTUSDT BTCUSDT \
  --start 2026-08-01T00:00:00Z \
  --end 2026-08-03T00:00:00Z \
  --repeat-for-idempotency
```

### Universe history backfill (~100 coins)

```bash
python scripts/build_universe_100.py
python scripts/backfill_bybit_universe.py \
  --universe config/universe_100.json \
  --start 2026-01-01T00:00:00Z \
  --end 2026-08-10T00:00:00Z \
  --resume
```

### Live 1m collector (+ shadow signals)

```bash
# Supervised service + localhost control API
python scripts/run_live_collector_service.py \
  --live-universe config/live_universe.json

# Or collector only (defaults to live_universe.json)
python scripts/run_bybit_live_1m_collector.py
```

Architecture: [docs/LIVE_COLLECTOR.md](docs/LIVE_COLLECTOR.md).  
Control contract: [docs/LIVE_COLLECTOR_DASHBOARD_CONTRACT.md](docs/LIVE_COLLECTOR_DASHBOARD_CONTRACT.md).  
Always `STARTING → RECOVERING → signal catch-up → SUBSCRIBING → LIVE`.  
systemd unit: `deploy/systemd/bybit-live-1m-collector.service` (desired_state aware).

## Tables (summary)

| Table | Engine | Partition | Order By | Dedup |
| ----- | ------ | --------- | -------- | ----- |
| `candles_1m` | `ReplacingMergeTree(ingested_at)` | `toYYYYMM(open_time)` | `(exchange, symbol, interval, open_time)` | async replace; reads use `FINAL` |
| `signals` | `ReplacingMergeTree(ingested_at)` | `toYYYYMM(generated_at)` | `(symbol, timeframe, candle_open_time, signal_id)` | async; reads use `FINAL` |
| `signal_outcomes` | `ReplacingMergeTree(ingested_at)` | `toYYYYMM(evaluated_at)` | `(signal_id, horizon)` | multi-horizon key; reads use `FINAL` |

Details: [docs/DATABASE.md](docs/DATABASE.md).

## Higher timeframes

Only **1m** candles are stored. Higher TFs are derived on demand from
`signal_generator.timeframes` (shared history/live core).

Validated wave-fade strategy TFs: **15m / 30m / 1h / 4h**
(`STRATEGY_TIMEFRAMES`). Incomplete buckets are never treated as closed.
See [docs/TIMEFRAMES.md](docs/TIMEFRAMES.md).

## Frozen Wave-Fade strategy

Logic-preserving port of the validated BE50 baseline lives under
`signal_generator.strategy.wave_fade` (freeze commit `f16ae32…`).
Provenance: `src/signal_generator/strategy/wave_fade/FROZEN_BASELINE.md`.

## Shadow signal pipeline

`scripts/run_wave_fade_shadow_pipeline.py` runs the frozen core on closed
`candles_1m` → HTF → candidates (all + Tier-A) into `signals`, with persistent
watermarks in `signal_processing_state`. Default mode is **shadow** (no trades).

Policy: `GLOBAL_FROZEN_TIER_A` — same rules/edges for every symbol, no refit.

## Tests

```bash
# unit (no ClickHouse required)
pytest tests/unit -q

# integration (needs live ClickHouse + credentials in env/.env)
pytest tests/integration -q -m integration
```

## Explicitly not yet

100-coin history scale-out, live collector, stochastic/wave logic, ranking,
dashboard, trading bot.
