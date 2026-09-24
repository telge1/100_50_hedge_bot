# POOL_ORDER_PLAN_V1

Isolated research overlay. Default dashboard strategy remains `wave_fade_no_be50_v1`.

Enable only with `ENABLE_POOL_ORDER_PLAN_V1=true` **and** a published ClickHouse artifact under `results/pool_order_plan_v1/latest`.

`POOL_CANDLE_SOURCE = CLICKHOUSE_ONLY`. CSV is `TEST_FIXTURE_ONLY` and never publishes `latest`.

## Batch (offline, read-only ClickHouse)

```bash
cd dashboard
ENABLE_POOL_ORDER_PLAN_V1=true python -m pool_order_plan_v1.batch --symbol HYPEUSDT --limit 20
```

Does not write `signals` / `signal_outcomes`. Does not start services.

Planner pin: `c6c960a82e9a0c538dbe24b03f481893e722072f`. Override: `POOL_ORDER_PLAN_ALLOW_DIRTY_PLANNER=1`.

## Causal 5m

Last allowed bar: `five_minute_close_time <= entry_time`.
Entry `01:17` → last bar `01:10–01:15`. Missing that bucket → `LAST_5M_INCOMPLETE` (no older fallback).
