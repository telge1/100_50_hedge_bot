# PREFLIGHT.md

cutoff_utc = `2026-09-04T14:15:38Z`

## Hard gate

Required audit verdict: `PUBLIC_TRADES_BACKFILL_EXISTING_PIPELINE_READY`

Actual audit verdict (`results/public_trades_backfill_existing_code_audit_v1/ABSCHLUSSBERICHT.md`):

**`PUBLIC_TRADES_BACKFILL_PIPELINE_PARTIALLY_READY`**

→ **Import must not start.**

## Gate checklist

| Condition | Pass | Notes |
|-----------|------|-------|
| audit_verdict_exact_READY | NO | Audit was PUBLIC_TRADES_BACKFILL_PIPELINE_PARTIALLY_READY, not PUBLIC_TRADES_BACKFILL_EXISTING_PIPELINE_READY |
| historical_source_confirmed | YES | SOURCE_CONFIRMED public.bybit.com/trading |
| existing_downloader_found | YES | SG PublicTradeDayDownloader + OA twin |
| existing_backfill_path_found | YES | run_public_trades_7d_backfill.py |
| schema_and_side_semantics_confirmed | YES | side=taker Buy/Sell; trade_ts UTC |
| stable_trade_id_or_dedup_key | YES | trade_id=trdMatchID; ORDER BY (symbol, trade_id) |
| resume_supported | YES | BackfillManifestStore AUDITED skip |
| target_tables_and_canonical_known | YES | orderbook_analysis.public_trades_canonical (no separate view) |
| repeated_import_logically_idempotent | YES | ReplacingMergeTree + existing-ID skip; exact counts need FINAL/uniqExact |
| no_unexplained_timezone_shift | YES | DateTime64 UTC throughout |
| sufficient_free_disk | YES | df showed ~352G free; BTC+DOGE within audit storage gate; write not started |
| no_corrupt_source_files | N/A | N/A — no download started |
| no_production_schema_change_required | YES | CREATE IF NOT EXISTS already deployed |

## Live protection snapshot

- Full-OB collector PID 1692334: present (not touched)
- OI/liquidation PID 147111: present (not touched)
- Public-trades live collector PID 1661773: present (not touched)
- Competing public-trades backfill process: none observed
- Free disk (~root): ~352G available at preflight

## Coverage snapshot (from prior audit, not re-imported)

- BTCUSDT / DOGEUSDT canonical span: 2026-07-19 → live (as of audit)
- Missing relative to last 12 months: entire calendar history before 2026-07-19
- Sep-4 BTC window 11:17–12:57 UTC: 389723 logical trades verified in audit

## Actions taken

- No download
- No ClickHouse INSERT/CREATE/ALTER/DROP/OPTIMIZE
- No code changes
- No process stop/restart
- No commit/push
