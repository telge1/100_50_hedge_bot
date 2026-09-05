# Public Trades Backfill Audit (Phase A)

**Verdict: `PUBLIC_TRADES_BACKFILL_PARTIALLY_READY`**

## Scope

Prove whether the **live** public-trades collector automatically backfills gaps on restart. No production restart performed. Evidence: code path + existing unit tests location + live status metrics + DB freshness.

## Entry points

| Role | Path |
|------|------|
| Live service | `Signal_Generator_Ralf/signal_generator_stoch_waves/scripts/run_live_collector_service.py` (PID **1661773**, `--enable-public-trades`) |
| Live collector | `src/signal_generator/bybit/live/collector.py` |
| PT buffer/writer | `.../live/trade_buffer.py` → `CanonicalPublicTradeRepository` |
| Historical backfill CLI | `scripts/run_public_trades_7d_backfill.py` (+ dirty/untracked `run_public_trades_30d_pipeline.py`) |
| Archive source | `https://public.bybit.com/trading/{SYM}/{SYM}{YYYY-MM-DD}.csv.gz` |

## Canonical table

`orderbook_analysis.public_trades_canonical`

- Columns include `trade_ts`, `ingest_timestamp`, `symbol`, `trade_id`, `side`, `source`, …
- Dedup key: **`(symbol, trade_id)`** (ReplacingMergeTree / repository skip-existing semantics per prior audits + code comments).
- Live inserts use `source="live"` (`trade_buffer.py`).

## Startup / recovery path (proven by code)

1. On start/reconnect, `run_recovery(reason="startup_or_reconnect")` runs **candle** gap recovery via REST (`recover_symbols_full` / `recover_symbols`).
2. Public trades then attach to **live WebSocket** only (`on_public_trade` → buffer → insert `source=live`).
3. There is **no** call from startup recovery into archive downloader / 7d backfill runner.

## Gap-fill stubs

- `PublicTradeHealthMetrics.gap_recovered` exists but is **never incremented** in src (only defined/serialized).
- `ALLOWED_SOURCES` includes `"gap_fill"` in `public_trades/guards.py`, but **no writer path** sets `source=gap_fill` in live collector.
- Historical fill is a **separate CLI** (`run_public_trades_7d_backfill.py`) with locks, manifests, pagination over daily gz files.

## Live seam / race

- Backfill CLI and live collector can both write canonical with trade_id dedup — safe against duplicates **if** both use same key.
- 30d pipeline explicitly **refuses** to run while live collector is running (hard fail) — documents intentional separation.
- Restart does **not** freeze a backfill cutoff or crawl archive for missed WS time.

## Crash / writer behavior

- Live status (2026-09-04T17:18Z): `insert_failures=0`, but `dropped_events=493019` with `last_error=queue_full_dropped_event`.
- Dropped WS events are **not** automatically repaired by archive backfill on restart.
- Process can remain LIVE while having permanently lost trades unless CLI backfill covers the window.

## Watermark

- No durable public-trade watermark driving auto gap fill.
- Candle recovery has its own gap accounting (`recovery_gap_count=51` on last start 2026-09-03) — candles only.

## Max history

- Archive CSV by calendar day; practical limit = Bybit public archive retention (multi-month; prior audits ~from 2026-07-19 present in CH for 51).
- Not unbounded REST trade history.

## Restart automatic backfill?

| Claim | Result |
|-------|--------|
| Restart fills candle gaps | **YES** (code + recovery_status) |
| Restart fills public trade gaps via archive | **NO** |
| Restart resumes live WS trades only | **YES** |
| Separate CLI can idempotently backfill | **YES** (pipeline exists; SG worktree dirty on those files) |

## Tests (not re-executed in this Phase A batch; locations)

- `tests/unit/test_public_trades_7d.py` — archive parse, dedup, runner.
- Dirty/untracked: `test_public_trades_30d_cli.py`, window helpers.

Phase C must re-run these offline before claiming READY.

## Classification

`PUBLIC_TRADES_BACKFILL_PARTIALLY_READY`

Reasons:

1. Production-quality **manual** archive→canonical pipeline exists.
2. Live restart **does not** auto-backfill trades.
3. `gap_fill` source is allowlisted but unused in live path.
4. Queue drops prove silent loss without auto repair.
5. Dirty overlap on SG backfill/live files blocks safe in-place completion without hunk isolation.
