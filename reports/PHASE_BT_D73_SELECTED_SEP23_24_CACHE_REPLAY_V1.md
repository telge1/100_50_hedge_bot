# BT-D73 — Selected Sep-23/24 Canonical Cache + EMA V1/V3 Replay

**VERDICT:** `SELECTED_WINDOW_REPLAY_PASS`

## Window (UTC, inclusive 5m decision closes)

- Start: `2026-09-23T06:45:00Z`
- End: `2026-09-24T14:50:00Z`
- Closes: **386** (expected 386)

## Preflight

- Git HEAD: `16eaebc72974409ae367517c744cfabb5de619e0`
- Disk free (GiB): **190.44**
- Density warmup hours (BT-D4): **168** (≥168 required)
- OHLC load: **0.466** s; missing window bars: **0**
- Donor cache hits in window: **0** / misses **386**
- Bounded sample density (1 close): **6.2669** s
- BT-D60: `EMA_HISTORY_VALIDATED`

## Cache build

- Wall time: **2269.5913** s
- Validated snapshots: **386** / 386
- Donor reuse: **0**
- Cold computed: **386**
- Cold compute total: **2268.1789** s
- Mean cold (this run): **5.8761** s

Bundle: `results/phase_bt_d73_selected_sep23_24_v1/cache_bundle/`

## Chronological replay (RESEARCH_READY)

| Metric | V1 | V3 |
|--------|----|----|
| Pool candidates | 33 | 33 |
| Authorized at candidate | 2 | 6 |
| Blocked at candidate | 21 | 23 |
| Pending created | 2 | 6 |
| Modeled fills | 1 | 1 |
| First simulated SL bar | None | None |

**Fail-closed default:** `DEFAULT_SHORT_PAUSED` V1 pending created = **0** (no silent pause disable).

**Disclaimer:** Simulated limit touches are not confirmed exchange executions.

## Artifacts

`results/phase_bt_d73_selected_sep23_24_v1/` — coverage_manifest, cache_progress, pool_candidates, ema_v1_v3_gate_comparison, pending chronology, modeled positions, signal_chart_timestamps, replay_summary.

## EMA gate summary (RESEARCH_READY, all 386 closes)

- Bars with valid pool candidate: **33**
- EMA-only classification differs (V1 vs V3): see `ema_v1_v3_gate_comparison.csv` (`ema_only_differs`)
- V1 authorized pending at candidate: **2**; V3: **6**
- EMA shadow rows at candidates: `ema_shadow_at_candidates` captured in replay (independent of pause)

## Pause segments

- **Before first simulated SL:** entire window (no `SIMULATED_SL` in V1 or V3).
- **After pause suppression:** not applicable in this window (pause remained `READY`).
- **DEFAULT_SHORT_PAUSED:** **0** pending creates (fail-closed control run).

## Shared density

Single validated `PilotDensitySession` / BT-D73 cache bundle served both V1 and V3 replays (identical pool candidate count **33**).

## Verification

- `pytest` D8/D9 cache: **pass**
- `pytest` D41 pending + D45 execution contract: **pass**
- `pytest` D71 compare + isolated EMA V1/V3: **pass**
- `pytest` BT-D73 window contract: **pass**
- Entry/SL/TP: unchanged baseline config (`load_isolated_strategy_bundle`); example V1 fill at `2026-09-23T10:00:00Z` entry **0.10006**, SL **0.10039038**, TP **0.098462** (`modeled_positions_v1.csv`).

## Storage

- Cache bundle: **386** entries (~**50 MB** under `cache_bundle/entries/`).
- Resumable progress: `cache_progress.json` (safe restart without overwriting valid entries).

## Chart review

See `signal_chart_timestamps.csv` for EMA diffs, pending-state diffs, and modeled fill events (V1 fill `2026-09-23T10:00:00Z`; V3 fill `2026-09-23T12:30:00Z`).
