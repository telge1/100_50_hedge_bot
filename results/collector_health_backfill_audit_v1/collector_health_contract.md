# Collector Health Contract v1

## SoT

- Historical OI backfill SoT: ClickHouse `orderbook_analysis.open_interest_5m_history`
- `source=BYBIT_REST_5M_HISTORY`, `granularity=5m`
- Live `open_interest_5s` / `open_interest_events` remain separate; never fabricate 5s from 5m
- MySQL `research_open_interest_5m` is **not** written or equated

## Status rules

| Status | Meaning |
|--------|---------|
| HEALTHY | process (if applicable) + source/heartbeat + writer + freshness + coverage |
| DEGRADED | running with lag, drops, partial coverage, or gated incompleteness |
| STALE | process may run but DB/heartbeat frozen |
| STOPPED | process absent / controlled stop |
| BACKFILLING | controlled job active |
| UNKNOWN | probe timeout / insufficient evidence |

**PID alone never yields HEALTHY.**

## Public trades UI

Banner: `DEGRADED — LIVE CURRENT BUT DATA LOSS POSSIBLE`  
Backfill button disabled until re-audit proves drop root-cause, gap-fill, seam, dedup.

## Full OB

Always report STOPPED in this phase; no start/repair/replace.

## API

- `GET /api/collector-health`
- `GET /api/collector-health/{id}`
- `GET /api/collector-health/csrf`
- `POST /api/collector-backfill/detect` (CSRF + Origin + auth)
- `POST /api/collector-backfill/start` (dry-run default; execute fail-closed)
- `GET /api/collector-backfill/jobs/{job_id}`

## CLI

`scripts/oi_5m_history_backfill.py` — default dry-run; `--detect-gaps`; `--execute` required for writes.
