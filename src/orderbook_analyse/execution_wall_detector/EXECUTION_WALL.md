# EXECUTION_WALL detector

Separate offline/read-only path for **near-market** liquidity walls.

STRUCTURE_WALL detection (`dynamic_wall_detector` / `wall_history`) is unchanged.

## Defaults

| Param | Default | Notes |
|---|---|---|
| `max_distance_bps` | 30 | Primary execution band; research up to 50 |
| `bucket_mode` | `ticks` | Prefer micro-structure; not `auto_10bps` |
| `bucket_ticks` | 1 | Symbol tick size inferred from book |
| `sample_interval_ms` | 500 | Configurable 100–5000 |
| Local dominance | multiple≥3 **or** local-neighbourhood pct≥95 **or** share≥0.10 | Plus min qty/notional |
| `near_touch_*` | ≤10 bps: multiple≥2 / pct≥80 / top-3 | Soft bar so BBO is not drowned by 20–30 bps size |
| `min_lifetime_ms` | 250 | Short-lived near walls kept if touched |
| Outcomes | touch/break 5 bps (or 2 ticks if tighter), accept 15s, fail-return 60s | Reuses `wall_toxicity_audit.outcomes` |

## IDs

`{SYMBOL}:EX:{SEGMENT}:{SIDE}:EW{NNNNNN}`

## CLI

```bash
PYTHONPATH=src python scripts/run_execution_wall_detector.py \
  --symbol APTUSDT \
  --start '2026-07-26 09:00:00' \
  --end '2026-07-26 15:00:00' \
  --structure-sequences-csv results/general_APTUSDT/full_history/wall_sequences.csv \
  --output-dir results/execution_walls_APTUSDT_6h \
  --overwrite
```

Validated on the 6h APTUSDT window above. Longer 23h/full runs are still pending (CH storage limits).

See `STRUCTURE_WALL.md` for the remote/global Structure path.
