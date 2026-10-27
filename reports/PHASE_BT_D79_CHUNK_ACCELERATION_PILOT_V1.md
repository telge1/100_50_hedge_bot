# BT-D79 — Chunk Lifetime Acceleration Pilot V1

**VERDICT:** `CHUNK_ACCELERATION_PASS`

**Git:** `d727e6a6163022337cf675df350ffddf18b47db6`  
**Pilot:** 24 closes `2026-09-23T09:30:00Z` … `2026-09-23T11:25:00Z` (inside BT-D73 validated window)

## Density parity (canonical vs chunk vs D8)

- **Band parity chunk:** PASS
- **First failure:** `null`

## Performance (measured)

| Workload | Total (s) | Mean / close (s) |
|----------|-----------|------------------|
| Canonical uncached (24× `get_density_bands`) | 133.7217 | 5.5717 |
| Chunk amortized (1× lifetime + 24× Node/clip) | 7.1288 | 0.297 |
| D8 validated read (24×) | 0.0137 | 0.000573 |

**Speedup chunk vs canonical:** **18.76×**

Chunk build lifetimes only: **5.5976** s; Node+clip phase mean **63.799** ms/close.

Lifetime builder `timing_ms` (from window): `{"clickhouse_candles": 0.0, "build_lifetimes": 345.2, "build_clusters_by_dc": 0.0, "json_serialize": 0.9, "total": 5528.9}`

## Chronological EMA V3 (after density parity)

{
  "match": true,
  "canon_wall_s": 0.1238,
  "chunk_wall_s": 0.1248
}

## Feasibility sketch

| Horizon | Closes (5m) | Canonical order-of-magnitude | With chunk amortization (if parity holds) | Replay with D8 |
|---------|-------------|------------------------------|-------------------------------------------|----------------|
| 3 days | ~864 | ~1.3 h | ~0.1 h compute | ~0.5 s read |
| 7 days | ~2016 | extrapolate | extrapolate | D8 bundle build still dominates first-time |
| 30 days | ~8640 | extrapolate | needs chunking ≤72h export cap per window slice | same |

**Bottleneck remaining:** per-close **Node** subprocess (~63.799 ms here); lifetime build once per chunk; canonical still repeats full window per close.

## Smallest next safe step

1. Multi-chunk driver for >72h visible span (tile by `MAX_EXPORT_HOURS`).
2. Optional batched Node (multiple `as_of` in one process) with parity harness.
3. Use **D8** for 30-day **replay** after resumable build; use **chunk** only to accelerate **first-time** bundle generation.

**STOP.**
