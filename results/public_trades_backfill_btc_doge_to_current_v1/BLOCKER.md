# BLOCKER.md

## Verdict

`PUBLIC_TRADES_BACKFILL_BLOCKED`

## Exact blocker

Hard Freigabebedingung requires audit verdict:

```text
PUBLIC_TRADES_BACKFILL_EXISTING_PIPELINE_READY
```

Audit artifact states:

```text
PUBLIC_TRADES_BACKFILL_PIPELINE_PARTIALLY_READY
```

Path: `results/public_trades_backfill_existing_code_audit_v1/ABSCHLUSSBERICHT.md`

## Why PARTIALLY_READY is not enough under this order

This follow-up order forbids import unless the audit is **exactly** READY. PARTIALLY_READY left open:

1. Dual-path risk (OA `public_trades_archive` vs SG `public_trades_canonical`)
2. Exact-count semantics require FINAL/uniqExact (not all consumers)
3. 51-coin storage-gate limits (less relevant for BTC/DOGE alone, but part of overall readiness label)
4. Listing-calendar only via HTTP 404, not a static calendar
5. Audit recommended waiting for **explicit approval** before any run

## What was NOT done

- No pilot table created
- No Bybit day files downloaded
- No productive or isolated imports
- No collector/OI/dashboard/CH service changes

## Required next human step

Either:

A) Re-issue an explicit override that accepts PARTIALLY_READY **for BTCUSDT+DOGEUSDT only** and authorizes Phase B pilot + productive backfill, or  
B) Close the PARTIALLY_READY gaps in a new audit and produce verdict `PUBLIC_TRADES_BACKFILL_EXISTING_PIPELINE_READY`, then re-run this order.

cutoff_utc captured at block time: `2026-09-04T14:15:38Z`
