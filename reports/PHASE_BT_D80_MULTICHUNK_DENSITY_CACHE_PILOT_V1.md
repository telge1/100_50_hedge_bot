# BT-D80 — Multi-Chunk Density Cache Pilot V1

**VERDICT:** `MULTICHUNK_CACHE_PASS`

**Baseline:** BT-D79 `CHUNK_ACCELERATION_PASS` @ `9054c241`  
**Git (this run):** `9054c241431e2e37609a498bb947aca9f0053f1c`  
**Symbol / TF / PD / depth:** DOGEUSDT 5m PD1 Depth2  
**Window:** `2026-09-23T00:00:00Z` … `2026-09-25T23:55:00Z` (**864** completed 5m decision closes)

**Bundle:** `results/phase_bt_d80_multichunk_cache_pilot_v1/cache_bundle/`  
**Density transport:** `validated_cache` only during replay (fail-closed; no canonical fallback)

## Coverage and fingerprints

| Field | Value |
|-------|-------|
| Manifest `entry_count` | **864** |
| `required_pipeline_line_id` | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |
| `required_config_hash` | `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9` |
| `density_method` | `exact_unique_origin` |
| Donor (D73) | `/home/telgenbuescher/projects/pools+ob+delta_bot/results/phase_bt_d73_selected_sep23_24_v1/cache_bundle` |
| UTC calendar chunks | 3 × 288 closes, visible span **≤ 71.92 h** each (`MAX_EXPORT_HOURS` 72) |

## Data correctness verdict

- **Chunk-boundary parity gate:** **PASS** — 2×24 closes spanning Sep23→24 and Sep24→25 day boundaries; multichunk vs canonical band sets (`parity_compare_v1`).
- **D8 cross-check:** **24** closes also matched existing D73 donor entries where present (Sep23/24 overlap).
- **Final bundle validation:** **864/864 PASS** — shape + manifest read for every close in window.
- **No skipped decision closes:** contiguous 5m grid from window start through end inclusive.

Full parity rows: `results/phase_bt_d80_multichunk_cache_pilot_v1/boundary_parity.json`

## Runtime (measured)

| Stage | Seconds | Notes |
|-------|---------|-------|
| Boundary parity (48× canonical) | **318.3** | Gate only; not repeated for bulk |
| Cold cache build (first pass) | **35.92** | 386 D73 donor hits + **478** new chunk writes |
| Warm resume / manifest refresh | **0.83** | All **864** `resume_ok` |
| EMA V3 replay (2 modes) | **10.03** | RESEARCH_READY **4.6593** s; DEFAULT_SHORT_PAUSED **4.7592** s |
| D8 read sample | **0.000643** s/close | Warm replay path |

Cold-build detail: `results/phase_bt_d80_multichunk_cache_pilot_v1/cold_build_result.json`

## EMA V3 replay — RESEARCH_READY (`SHORT_OK` when authorized)

| Metric | Count |
|--------|------:|
| Pool candidates | **77** |
| EMA-V3 authorized | **6** |
| Blocked (UNCERTAIN / SHORT_NO / etc.) | **64** |
| Pending created | **6** |
| Pending cancels (EMA) | **1** |
| Modeled fills | **1** |
| Coverage gaps | **0** |

**DEFAULT_SHORT_PAUSED:** pending created = **0** (fail-closed baseline; separate from RESEARCH_READY).

**Modeled position (V3, window-limited):** one `SIMULATED_FILL` @ **2026-09-23T12:30:00Z** (signal **2026-09-23T12:25:00Z**); no TP/SL resolution inside evaluated window — treat as **open / unresolved** past window end.

Artifacts: `pool_candidates.csv`, `pending_chronology_v3.csv`, `modeled_positions_v3.csv`, `signal_chart_timestamps.csv` (**2** rows).

### Chart-review timestamps (sample)

```
[
  {
    "decision_close_utc": "2026-09-23T12:30:00Z",
    "chart_review_reason": "MODELED_SIMULATED_FILL_v1",
    "pool_key": "0.099690000000000001|0.099894000000000011",
    "ema_v1": "",
    "ema_v3": ""
  },
  {
    "decision_close_utc": "2026-09-23T12:30:00Z",
    "chart_review_reason": "MODELED_SIMULATED_FILL_v3",
    "pool_key": "0.099690000000000001|0.099894000000000011",
    "ema_v1": "",
    "ema_v3": ""
  }
]
```

## Safety and resume

- Research-only driver; no live trading, production activation, or ClickHouse writes/imports.
- Atomic per-close cache writes + `cache_progress.json` checkpointing; interrupted runs resume without recomputing completed closes.
- No silent canonical fallback during bundle build or replay.

## Scaling (evidence-based)

| Horizon | Closes | Cold build (order-of-magnitude) | Warm replay |
|---------|--------|---------------------------------|-------------|
| 3 days | 864 | **~36 s** wall (+ **~18 s** 3× chunk lifetime) this pilot | **~10 s** chrono + **~0.6 ms** D8 read |
| 7 days | ~2016 | ~**2.3×** new closes vs 3-day if donor reuse limited | ~**23 s** replay extrapolation |
| 30 days | ~8640 | ~**10** UTC day-chunks; **~70 ms** amortized collect/close observed on new writes | D8 read still **sub-second**; build dominates |

**STOP.** (3-day pilot only — no month-long job.)

## Appendix — preflight

```json
{
  "closes": 864,
  "chunk_plan": [
    {
      "chunk_index": 0,
      "closes": 288,
      "first": "2026-09-23T00:00:00Z",
      "last": "2026-09-23T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 1,
      "closes": 288,
      "first": "2026-09-24T00:00:00Z",
      "last": "2026-09-24T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    },
    {
      "chunk_index": 2,
      "closes": 288,
      "first": "2026-09-25T00:00:00Z",
      "last": "2026-09-25T23:55:00Z",
      "visible_span_hours": 71.9167,
      "export_safe": true
    }
  ],
  "disk_free_gib": null,
  "d73_bundle_present": true
}
```

## Appendix — cache build (last run)

```json
{
  "complete": true,
  "entry_count": 864,
  "reused_donor": 0,
  "chunk_written": 0,
  "skipped": 864,
  "chunk_compute_s": 0.0,
  "multichunk_timing": {
    "chunk_count": 3,
    "chunk_build_s": [
      6.0775,
      6.0901,
      6.3129
    ],
    "total_build_s": 18.4806
  },
  "multichunk_session_build_s": 18.4806,
  "cache_build_wall_s": 0.8309
}
```
