# Historical Trade Download Smoke

**Primary Decision:** `HISTORICAL_TRADE_SMOKE_OK`

## Endpoint / params

- URL: `https://www.bybit.com/x-api/quote/public/support/download/list-files`
- bizType=`contract` productId=`trade` interval=`daily` periods=``
- No browser cookies/tokens hardcoded; Akamai warmup via `/data-download`.

## Code changes

- `research/orderbook/bybit_historical_download_common.py` (shared session/warmup/retry/.part/ZIP/gzip)
- `research/orderbook/bybit_historical_trades_download.py`
- `scripts/download_bybit_historical_trades.py`
- `scripts/run_historical_trade_download_smoke.py`
- OB downloader left intact (no behavior change required for this smoke).

## Downloads

- APTUSDT 2026-01-06: status=OK file=APTUSDT2026-01-06.csv.gz size=8963883 (~8.6MB gz, ~26MB csv) trades=213410 buy=109610 sell=103800
- DOGEUSDT 2026-01-06: status=OK file=DOGEUSDT2026-01-06.csv.gz size=40324628 (~39MB gz, ~126MB csv) trades=1071232 buy=532012 sell=539220

## Format (APTUSDT 2026-01-06)

- Archive from API: `.csv.gz` (kept) → decompressed `.csv` (kept)
- detected_format: `csv`
- columns: `['timestamp', 'symbol', 'side', 'size', 'price', 'tickDirection', 'trdMatchID', 'grossValue', 'homeNotional', 'foreignNotional', 'RPI']`
- timestamp_unit: `s` with fractional microseconds (Unix seconds, UTC)
- Join note vs Historical OB `ts`: OB uses milliseconds; convert trade_ts_s * 1000 ↔ OB ts_ms
- side values: `['Buy', 'Sell']`

## Buy/Sell semantics

- Buy = taker/aggressor buy (hits ask)
- Sell = taker/aggressor sell (hits bid)
- Confirmed via Bybit API v5 `market/recent-trade` docs (`side` = Side of taker) and existing project CH/public_trade_source convention — not guessed from filenames.

## Event coverage (±5m around first_break_ts)

- `APTUSDT_PROTECTED_LOW_BREAK_bearish_20260106_1p896_1h` → COVERED n=5560 (buy=2231 sell=3329)
- `DOGEUSDT_PROTECTED_LOW_BREAK_bearish_20260106_0p14909_1h` → COVERED n=103630 (buy=47971 sell=55659)

## Tests

- `research/orderbook/tests/test_bybit_historical_trades_download.py` + existing `test_historical_bybit_replay.py`: **22 passed**

Artifacts: `/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/historical_trade_download_smoke_20260808`

