# MySQL Candle Coverage Audit

## Primärentscheidung

**MYSQL_CANDLE_DATA_READY**

Reason: schema clear; focus symbols present; causality pass; no critical gaps

## Connection / Schema

- Config: `research/regime_scanner/.env.regime_db (gitignored) + REGIME_DB_*`
- Connector: `research.regime_scanner.mysql_candle_store.store_mysql.MySQLCandleStore`
- Database: `regime_scanner_research`
- Canonical table: `market_candles`
- Identity: `(exchange, symbol, timeframe, open_time)`
- Timestamp: `open_time` = candle open UTC; `close_time` = open + TF
- Market-Type column: **none** (No market_type column in market_candles; exchange+symbol encode identity. Docs/bootstrap describe Bybit futures feathers.)

## Inventory

- Exchanges: ['bybit']
- Symbols (15): ['ADAUSDT', 'APTUSDT', 'ARBUSDT', 'AVAXUSDT', 'BNBUSDT', 'BTCUSDT', 'DOGEUSDT', 'ENAUSDT', 'ETHUSDT', 'LINKUSDT', 'OPUSDT', 'SEIUSDT', 'SOLUSDT', 'SUIUSDT', 'XRPUSDT']
- Timeframes: ['15m', '30m', '5m']

## Focus symbols

| Requested | Mapped | Found |
|---|---|---|
| APTUSDT | APTUSDT | True |
| DOGEUSDT | DOGEUSDT | True |
| BTCUSDT | BTCUSDT | True |

| Symbol | Timeframe | Beginn UTC | Ende UTC | Candles | Coverage % | Größte Lücke (s) |
|---|---|---|---|---:|---:|---:|
| APTUSDT | 15m | 2025-12-27T00:00:00+00:00 | 2026-07-02T11:45:00+00:00 | 17999 | 100.0 | None |
| APTUSDT | 30m | 2025-12-27T00:00:00+00:00 | 2026-07-02T11:30:00+00:00 | 8999 | 100.0 | None |
| APTUSDT | 5m | 2025-12-27T00:00:00+00:00 | 2026-07-26T17:05:00+00:00 | 60973 | 100.0 | None |
| DOGEUSDT | 5m | 2025-12-27T00:00:00+00:00 | 2026-06-27T13:00:00+00:00 | 52572 | 100.0 | None |
| BTCUSDT | 5m | 2025-01-01T00:00:00+00:00 | 2026-06-27T12:05:00+00:00 | 156241 | 100.0 | None |

## Warm-up

| Symbol | TF | 7d | 30d | 60d | 90d | Span days |
|---|---|---:|---:|---:|---:|---:|
| ADAUSDT | 5m | True | True | True | True | 182.5 |
| APTUSDT | 15m | True | True | True | True | 187.5 |
| APTUSDT | 30m | True | True | True | True | 187.5 |
| APTUSDT | 5m | True | True | True | True | 211.7 |
| ARBUSDT | 5m | True | True | True | True | 182.5 |
| AVAXUSDT | 5m | True | True | True | True | 182.5 |
| BNBUSDT | 5m | True | True | True | True | 182.5 |
| BTCUSDT | 5m | True | True | True | True | 542.5 |
| DOGEUSDT | 5m | True | True | True | True | 182.5 |
| ENAUSDT | 5m | True | True | True | True | 182.5 |
| ETHUSDT | 5m | True | True | True | True | 542.5 |
| LINKUSDT | 5m | True | True | True | True | 182.6 |
| OPUSDT | 5m | True | True | True | True | 182.6 |
| SEIUSDT | 5m | True | True | True | True | 182.6 |
| SOLUSDT | 5m | True | True | True | True | 542.5 |
| SUIUSDT | 5m | True | True | True | True | 182.6 |
| XRPUSDT | 5m | True | True | True | True | 182.6 |

## Causality

Checks: 85, failures: 0

Rule: `close_time <= query_timestamp` (no running candle).

## Data quality

- Duplicate open keys logged: 0
- Invalid OHLCV rows logged: 0
- Gap events logged: 0

## Answers

1. Symbols: see Inventory.
2. Timeframes: see Inventory.
3. Ranges: see Focus table / `symbol_timeframe_coverage.csv`.
4. Use `market_candles` with columns listed in `schema_inventory.json`.
5. Stored identity timestamp is **candle open** (`open_time`); close is stored and derived.
6. Historical query: `WHERE close_time <= :decision_time` (also via `load_candles(..., decision_time=T)`).
7. Gaps/dupes: see CSVs.
8. Warm-up: see table above.
9. Direction runner: yes if primary is READY or PARTIAL with known TF limits.
10. Next: implement timestamp→closed candles→structure direction using existing MySQL read path.

**Hinweis für den späteren MTF-Runner:** In MySQL liegen aktuell **keine 1h/4h**-Reihen. Nur `5m` (alle Symbole) sowie `15m`/`30m` (nur APTUSDT). HTF für andere Coins muss aus 5m kausal aggregiert werden.
