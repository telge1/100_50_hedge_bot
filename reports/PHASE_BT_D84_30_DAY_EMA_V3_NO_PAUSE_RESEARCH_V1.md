# BT-D84 — 30-Day EMA V3 No Post-SL Pause Research V1

**VERDICT:** `MONTH_NO_PAUSE_RESEARCH_BLOCKED`

**Baseline execution parity:** BT-D83 `EXECUTION_PARITY_PASS` @ `f5fe267f`  
**Git HEAD:** `28cbc5c9662b27f518eea80b3116b08b535bd4b1`  
**Window:** `2026-09-01T00:00:00Z` … `2026-09-30T23:55:00Z` (**8640** closes)

## Research change (opt-in only)

| Setting | Value |
|---------|-------|
| `post_sl_pause_enabled` | **false** (BT-D84 runner) |
| Pause contract | `research_pause_v1_post_sl_opt_out` |
| SL behavior | unchanged — D46 intrabar SL still closes position |
| Production default | `SHORT_PAUSED` after modeled SL **unchanged** outside this runner |

## Provenance & cache

| Field | Value |
|-------|-------|
| Manifest entries | **n/a** / **8640** |
| Pipeline | `n/a` |
| Config hash | `n/a` |
| Donor order | D81 seven-day → D80 pilot → D73 |
| Bundle path | `results/phase_bt_d84_30day_no_pause_research_v1/cache_bundle/` |

## Parity evidence (bounded)

- Day-boundary gates: **29** × 24 closes — **NOT_RUN**
- Interior noon samples: **30** — canonical vs multichunk
- Boundary wall: **0** s; interior: **0** s
- Detail: `results/phase_bt_d84_30day_no_pause_research_v1/parity_evidence.json`

## Timing

| Stage | Seconds |
|-------|--------:|
| Multichunk session build | **n/a** |
| Cache build wall | **n/a** |
| Donor reuse | **n/a** |
| New chunk writes | **n/a** |
| Validation | **n/a** |
| Dual research replay | **n/a** |

## Monthly performance — no post-SL pause (primary)

| Metric | Value |
|--------|------:|
| Pool candidates | **n/a** |
| EMA V3 authorized | **n/a** |
| EMA blocked | **n/a** |
| Pending created | **n/a** |
| Pending EMA cancels | **n/a** |
| Modeled fills | **n/a** |
| TP / SL / ambiguous | **None** / **None** / **None** |
| Still open | **None** |
| Gross win rate (resolved) | **None%** |
| Avg win / avg loss (gross %) | **None** / **None** |
| Cumulative gross % | **None** |
| Max losing streak | **None** |
| Max drawdown (unit stack) | **None** |
| Final pause state | **None** |
| Coverage gaps | **n/a** |

**Not net profitability** — no fees, slippage, or leverage modeled.

## Reference — original SHORT_PAUSED after SL (separate replay)

| Metric | Value |
|--------|------:|
| Pending created | **n/a** |
| Modeled fills | **n/a** |
| TP / SL | **None** / **None** |
| Final pause state | **None** |

Trades are **not merged** between replays. Detail: `month_performance_short_paused_ref.json`.

### Trade ledger (no-pause primary)

```json
[]
```

### Chart review timestamps (sample)

```json
[]
```

## Safety

- Research-only isolated runner; no live trading, no ClickHouse writes, no dashboard changes.
- No changes to BT-D46 production contract, density SSOT, entry/SL/TP, or EMA rules.
- **STOP** — do not activate `post_sl_pause_enabled: false` in production.

## Appendix — preflight

```json
{
  "baseline_commit": "f5fe267f",
  "git_head": "28cbc5c9662b27f518eea80b3116b08b535bd4b1",
  "closes": 8640,
  "chunk_plan_days": 30,
  "disk_free_gib": 190.79,
  "d81_bundle_present": true,
  "d81_donor_closes": 2016,
  "d80_bundle_present": true,
  "d73_bundle_present": true,
  "ohlc_meta": {
    "symbol": "DOGEUSDT",
    "source": "ClickHouseResearchCandleSource.get_candles(5m strict_complete_buckets)",
    "resample": "pd1_landscape_viewer_v1._resample_5m_from_candles",
    "data_bounds_lo": "2026-05-01T00:00:00Z",
    "data_bounds_hi": "2026-09-30T23:45:00Z",
    "requested_start": "2026-07-31T00:00:00Z",
    "requested_end": "2026-09-30T23:55:00Z",
    "out_of_import_bounds": true,
    "bars_loaded": 17856,
    "grid_steps": 17856,
    "window_closes_missing": 0,
    "window_ohlc_ok": true
  },
  "research_post_sl_pause_enabled": false,
  "execution": "research_execution_runner_v1_d46_exit_parity",
  "note": "published_decision_close_hi is conservative vs requested window end; all 8640 decision closes verified in loaded 5m OHLC",
  "density_gate": {
    "ok": false,
    "expected_closes": 8640,
    "actual_closes": 8640,
    "published_data_lo_utc": "2026-05-01T00:00:00Z",
    "published_data_hi_utc": "2026-09-30T23:45:00Z",
    "density_scope_violations": [
      "2026-09-30T23:45:00Z",
      "2026-09-30T23:50:00Z",
      "2026-09-30T23:55:00Z"
    ],
    "density_scope_violation_count": 3,
    "last_supported_close_rule": "causal visible_to must be <= published_data_hi; typically limits last September close to 2026-09-30T23:40:00Z (8637 closes)"
  },
  "blocked": "DENSITY_PUBLICATION_TAIL"
}
```

## Density publication gate

When `density_gate.density_scope_violation_count` > 0, causal `visible_to` for late-month
5m closes extends past imported publication `data_hi` (currently **2026-09-30T23:45:00Z**).
Those closes cannot be validated via canonical or multichunk density without extending the
published timeline through at least **2026-10-01T00:00:00Z** (no ClickHouse import was
performed in BT-D84). OHLC for **2026-09-30T23:50:00Z** and **23:55:00Z** is present, but
pool density SSOT is out of scope for the last **3** requested closes.
