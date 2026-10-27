# BT-D78 — Cached Replay and Lifetime Feasibility V1

**VERDICT:** `CACHED_REPLAY_PASS`

**Git:** `37aa8b4baaf283ffba7d00614d2f56cac5a315ed`  
**D73 bundle:** `results/phase_bt_d73_selected_sep23_24_v1/cache_bundle/`  
**Density:** `validated_cache` only (fail-closed; no canonical fallback)

## Part A — Cache coverage

| Check | Result |
|-------|--------|
| Window closes | **386** (2026-09-23T06:45:00Z … 2026-09-24T14:50:00Z) |
| Manifest entries | **386** |
| Missing closes | **0** |
| Read/SHA validation failures | **0** |
| Validate wall time | **0.3723** s |
| Pipeline | `historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1` |
| Config hash | `4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9` |

## Part A — Replay metrics (RESEARCH_READY)

| Metric | V1 | V3 |
|--------|----|----|
| Pool candidates | 33 | 33 |
| Authorized | 2 | 6 |
| Blocked at candidate | 21 | 23 |
| Pending created | 2 | 6 |
| EMA pending cancels | 0 | 1 |
| Modeled fills | 1 | 1 |
| First simulated SL | None | None |
| Coverage gaps | 0 | 0 |

**DEFAULT_SHORT_PAUSED (V1):** pending created = **0** (separate from RESEARCH_READY runs).

## Part A — Runtime

| Stage | Seconds |
|-------|---------|
| OHLC load | 0.4325 |
| EMA maps (386 closes × V1/V3) | 1.5289 |
| Chronological replay (V1+V3+paused V1) | 5.6327 |
| Implied per-close replay | **14.593** ms (3 passes / 386) |

Artifacts: `results/phase_bt_d78_cached_replay_v1/` — pool_candidates, gate comparison, pending chronology, modeled positions, signal_chart_timestamps (10 chart-review rows).

### Chart-review timestamps (sample)

```
[
  {
    "decision_close_utc": "2026-09-23T09:30:00Z",
    "chart_review_reason": "EMA_V1_V3_DIFF|POOL_CONDITIONAL_DIFF|PENDING_STATE_DIFF",
    "pool_key": "0.10027|0.10034999999999999",
    "ema_v1": "SHORT_OK",
    "ema_v3": "UNCERTAIN"
  },
  {
    "decision_close_utc": "2026-09-23T09:55:00Z",
    "chart_review_reason": "EMA_V1_V3_DIFF|POOL_CONDITIONAL_DIFF|PENDING_STATE_DIFF",
    "pool_key": "0.10006|0.10019",
    "ema_v1": "SHORT_OK",
    "ema_v3": "UNCERTAIN"
  },
  {
    "decision_close_utc": "2026-09-23T10:40:00Z",
    "chart_review_reason": "PENDING_STATE_DIFF",
    "pool_key": "0.099970000000000003|0.10006",
    "ema_v1": "SHORT_OK",
    "ema_v3": "SHORT_OK"
  },
  {
    "decision_close_utc": "2026-09-23T10:45:00Z",
    "chart_review_reason": "PENDING_STATE_DIFF",
    "pool_key": "0.099970000000000003|0.10005",
    "ema_v1": "SHORT_OK",
    "ema_v3": "SHORT_OK"
  },
  {
    "decision_close_utc": "2026-09-23T11:00:00Z",
    "chart_review_reason": "PENDING_STATE_DIFF",
    "pool_key": "0.10019|0.10019400000000001",
    "ema_v1": "SHORT_OK",
    "ema_v3": "SHORT_OK"
  },
  {
    "decision_close_utc": "2026-09-23T11:10:00Z",
    "chart_review_reason": "PENDING_STATE_DIFF",
    "pool_key": "0.10006|0.10007000000000001",
    "ema_v1": "SHORT_OK",
    "ema_v3": "SHORT_OK"
  },
  {
    "decision_close_utc": "2026-09-23T12:10:00Z",
    "chart_review_reason": "PENDING_STATE_DIFF",
    "pool_key": "0.099610000000000004|0.099690000000000001",
    "ema_v1": "SHORT_OK",
    "ema_v3": "SHORT_OK"
  },
  {
    "decision_close_utc": "2026-09-23T12:25:00Z",
    "chart_review_reason": "PENDING_STATE_DIFF",
    "pool_key": "0.099690000000000001|0.099894000000000011",
    "ema_v1": "SHORT_OK",
    "ema_v3": "SHORT_OK"
  },
  {
    "decision_close_utc": "2026-09-23T10:00:00Z",
    "chart_review_reason": "MODELED_SIMULATED_FILL_v1",
    "pool_key": "0.10006|0.10019",
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

## Part A — Outcomes (this window)

| Outcome | V1 | V3 |
|---------|----|----|
| Simulated fills | 1 @ 2026-09-23T10:00:00Z | 1 @ 2026-09-23T12:30:00Z |
| Simulated SL/TP in window | none | none |
| OPEN at window end | yes (fill without exit row) | yes |

EMA rule: V3 permits only `SHORT_OK`; `SHORT_NO` / `UNCERTAIN` block (visible at 09:30 / 09:55 chart rows).

**Speed contrast (evidence):** D73 cold canonical build was **~6 s/close** class; this replay used **~15 ms/close** density read (3 chronology passes incl. EMA/pool logic, not density compute).

---

## Part B — Lifetime amortization feasibility (investigation only)

### B.1 What `_build_lifetimes` does

Source: `pd1_landscape_viewer_v1/historical_window_v1.py`

1. Iterate every **5m grid step** from `context_from` through `vis_to`.
2. Map each step to a **causal decision close** (`_causal_decision_close`).
3. Load **ACTIVE members** at that close (`load_historical_pool_state_at_close`, `include_remaining=False` in window builder).
4. Coalesce **segments** per `pool_id`: extend `[start,end]` while membership continuous; on gap, start new segment; **update** bottom/top when member geometry changes.

### B.2 Immutable vs time-varying

| Input | Fixed for a frozen CH publish line | Varies per as-of / grid step |
|-------|-------------------------------------|------------------------------|
| Published ledger rows | Yes (historical import) | New closes only as calendar advances |
| Catalog geometry at close `t` | Deterministic given CH | **Yes** — each decision close |
| ACTIVE `pool_id` set | Deterministic at each `t` | **Yes** |
| Merged `segments[]` per pool | Derived from full walk | **Yes** — depends on entire prefix of membership |
| Node `poolActiveAt(pl, tMs)` | — | **Yes** per `as_of_utc` |
| `display_intervals` / bands | — | **Yes** per close |

**Older still-ACTIVE origins:** preserved when segment coalescence spans across the 168h context; amortization must **not** collapse history to single-interval snapshots (4X-AK import kernel).

### B.3 What can be shared across consecutive closes in one replay window

| Mechanism | Already exists | Safe? |
|-----------|----------------|-------|
| D8 band snapshots per close | D73 bundle | **Yes** (this phase) |
| `member_cache` dict across closes | D76 / window builder | **Yes** for CH member loads |
| `build_episode_walk_batch_cache` for all closes in chunk | 5S / 4X-AK | **Yes** for membership I/O |
| One `_build_lifetimes` pass for union `[context_from(chunk_start), vis_to(chunk_end)]` | Not wired to backtester | **Plausible** if per-close Node `as_of` matches canonical |
| Reuse one Node output for multiple closes | No | **No** without proof |

### B.4 `analyzer_depth2_landscape.js` constraints

- Consumes **`payload.pools`** with **`segments`**; evaluates **`buildDepth2Intervals`** at **`as_of_utc`**.
- Forensic **`compareParity`** inside analyzer assumes chart-path geometry — changing pool segment inputs changes bands.
- **Per-close Node call** is ~seconds class in canonical path but **~0.04 s** in 4X-AK profile when pools are already materialized; batching multiple `as_of` in one Node process is an **unproven** optimization (D8 doc).

### B.5 Required parity tests before any incremental engine

1. Band set equality (`parity_compare_v1`) for every close in chunk vs per-close canonical.
2. `active_state_fingerprint` / membership at sample closes.
3. One isolated chronology digest (BT-D78-style) canonical vs chunk-amortized density session.
4. Edge cases: membership **toggle** mid-chunk, geometry **update** without id change, first close after 168h context shift.

### B.6 Expected performance (evidence-bound only)

| Approach | Supported estimate |
|----------|-------------------|
| Validated cache replay | **~0.001 s** read + strategy/EMA (this run **~8 s** total for 386×3 passes) |
| Amortized lifetime + per-close Node | **Not measured**; upper bound still **O(chunk_grid × Node)**; may remove redundant **member** loads and duplicate lifetime prefix work for overlapping 48h+168h windows |
| 4X-AK **0.16 s/close** import loop | **Not applicable** to canonical band contract |

### Minimal next safe experiment

1. Pick **24 consecutive closes** inside this window.
2. Run canonical `collect_at_close` vs **one** shared `build_landscape_window` + per-close Node `as_of` (Tier C from BT-D77).
3. Compare band sets (`parity_compare_v1`) and one **isolated chronology** digest.

**STOP.**
