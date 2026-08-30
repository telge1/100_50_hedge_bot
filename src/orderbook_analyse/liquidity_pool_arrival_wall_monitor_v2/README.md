# Liquidity Pool Arrival Wall Monitor V2

Research-only revision of pool-arrival / Raw-OB200 wall monitoring.

## Scope

- Pool-ID arrivals vs independent market clusters (overlapping pool intervals)
- Exact-arrival snapshot: only `ts <= arrival` with age ≤ 1s
- First-seen: `PRE_EXISTING` / `FIRST_SEEN_AT_ARRIVAL` / `APPEARED_STRICTLY_AFTER`
- Pool components + cluster continuity + cluster-wall dedup
- No public trades, no strategy, no outcomes / PnL

## Foundation

Uses committed `liquidity_pool_signal` (chart-identical pool engine) and the
minimal OB200 raw loader (`ob200_v3_raw_discovery` + `raw_archive.events`).
