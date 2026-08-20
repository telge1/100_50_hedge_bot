# STRUCTURE_WALL semantics (Phase-4 / existing path)

This document freezes the meaning of the existing wall detector outputs.
Detection thresholds and matching logic are **not** changed in the
Execution-Wall phase.

## Name

| Field | Value |
|---|---|
| `wall_scope` | `STRUCTURE` |
| `wall_type` | `STRUCTURE_WALL` |

Appended at the end of `wall_sequences.csv` rows for new runs. Older CSVs
without these columns remain readable (toxicity audit keeps `raw=dict(row)`).

## Producing modules

- `dynamic_wall_detector.py` — bucket aggregation, global-side percentile / depth share, wall flag
- `wall_history.py` — observe → match → sequences / transitions
- `full_history_analysis.py` / `general_research_runner.py` — write CSVs under `full_history/`

## Why remote walls dominate

1. Phase-4 default `distance_max_pct = 5.0` (~500 bps inclusion).
2. Percentile and depth_share are **side-global inside that window**, so large far resting size easily qualifies.
3. `was_near_price` / test use ~5 bps — almost nothing counts as near.
4. Preferred export resolution is `auto_10bps` only.

Empirical APTUSDT (`results/general_APTUSDT/full_history/wall_sequences.csv`):
~876/880 sequences with `min_distance_bps > 50`.

## Criteria (defaults)

| Param | Default |
|---|---|
| `wall_multiple_min` | 3.0 (vs local median radius 5) |
| `percentile_min` | 90 |
| `depth_share_min` | 0.01 |
| `distance_max_pct` | 5.0 (Phase-4) / 3.0 (detector standalone) |
| `resolutions_bps` | 5, 10, 20, 50 → `auto_Nbps` |
| `preferred_bps` | 10 |
| `match_distance_bps` | 10 |
| `sample_interval_sec` | 60 |

## Buckets

`choose_bucket_size(mid, tick, target_bps)` → nice ladder ≥ tick.
`assign_bucket_price`: bid=floor, ask=ceil.

## Role

STRUCTURE walls are longer-horizon liquidity / S-R **targets**, not
near-market microstructure interaction. Execution Walls are a separate path.
