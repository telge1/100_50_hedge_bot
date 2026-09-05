# OI 5m BTC/DOGE Production Backfill — Preflight

**Time (UTC):** 2026-09-04T17:47Z (approx)  
**Frozen window:** `2026-08-18T15:10:00Z` → cutoff `2026-09-04T17:38:05Z`  
**Last closed bucket in window:** `2026-09-04T17:35:00Z`  
**Expected per symbol:** **4926** (total **9852**)

## PID / process protection (read-only verified)

| Role | Expected PID | Status |
|------|--------------|--------|
| Dashboard | 1780509 | alive, systemd MainPID match |
| OI/Liq | 147111 | alive, not restarted |
| Public trades | 1661773 | alive, not restarted |
| Full OB | — | ABSENT / STOPPED |
| ClickHouse | docker/service | reachable (`SELECT 1`) |

## Disk

`/` ≈ **352G free** (60% used) — sufficient.

## ClickHouse / schema

- Table: `orderbook_analysis.open_interest_5m_history`
- Columns unchanged: exchange, category, symbol, bucket_time, open_interest, open_interest_value, source, collector_instance_id, inserted_at
- Engine: MergeTree
- Logical rows before: **8640**, 1 symbol (BTC), max `2026-08-18 15:05:00`, source `BYBIT_REST_5M_HISTORY`

## Missing counts (re-verified)

| Symbol | present in window | missing |
|--------|-------------------|---------|
| BTCUSDT | 0 | **4926** |
| DOGEUSDT | 0 | **4926** |

`missing_ok=true` — matches smoke dry-run.

## Lock / concurrency

- Advisory lock `/tmp/oi_5m_history_backfill.lock` free
- No other OI backfill process

## REST

- Sample first missing BTC `2026-08-18T15:10:00Z` present in Bybit REST with `openInterest` (retCode 0)
- Client: short-lived connections per operation (no shared locked CH session)

## Gate

**PREFLIGHT_PASS=true** — proceed to isolated pilot then production chunks.
