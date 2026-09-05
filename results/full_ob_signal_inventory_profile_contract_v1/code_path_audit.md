# Code Path Audit — Full-OB Profile → CROSS_IN

Read-only. Paths relative to `orderbook_analyse/src/orderbook_analyse/`.

## End-to-end

| Step | File | Symbol | Role |
|------|------|--------|------|
| 1 | `orderbook_v2_live/collector.py` | FR init ~L192–209 | Builds `ClickHouseCompletedProfileProvider(window_minutes=fr_settings.profile_window_minutes)`; attaches `FullObEdgeFlightRecorder` |
| 2 | `orderbook_v2_live/full_ob_edge_flight_recorder/config.py` | `FlightRecorderSettings`, `load_flight_recorder_settings` | Defaults: `profile_window_minutes=30`, `profile_poll_sec=20`, arm/capture/disarm **50 / 20 / 75**; env `OB_V3_FULL_OB_FR_*` |
| 3 | `.../profiles.py` | `last_completed_window` | UTC-aligned completed `[start,end)` of `window_minutes` |
| 4 | `.../profiles.py` | `ClickHouseCompletedProfileProvider.load` | Volume via `market_profile.build.build_profile`; optional TPO 1m marks from `load_public_trade_records`; emits 8 `EdgeLevel`s; meta `bracket_minutes=window_minutes` |
| 5 | `.../manager.py` | `poll_profiles` / `tick` | Poll ≥ `profile_poll_sec`; `watcher.set_edges`; mid from Full-OB or OB200 mid provider; `evaluate` → arm / bootstrap_observe / trigger / extend |
| 6 | `.../watcher.py` | `classify_zone_state`, `in_edge_zone` | `IN` if dist≤`capture_bps` (20); `OUT` if dist≥`disarm_bps` (75); else `APPROACH`. **arm_bps not used inside classify body** |
| 7 | `.../watcher.py` | `EdgeWatcher.evaluate` | Nearest edge; CROSS_IN on enter `IN`; bootstrap if start already `IN` without `saw_outside`; freeze via `begin_capture` |
| 8 | `.../manager.py` | `_start_or_merge_event` | Hard gate: only `CROSS_IN` + `edge_entry_crossed`; builds `CapturePlan`; freezes `edge_price_at_trigger` + `profile_context` |
| 9 | `.../capture_plan.py` | `CapturePlan` | Timing, markers, research gate (`REAL_CROSS_IN`) |
| 10 | `.../event_writer.py` | writer | Writes `event_manifest.json`, `profile_context.json`, deltas/markers |

## Zone / hysteresis (proven)

```text
distance_bps = |mid - edge| / mid * 10000

IN        : dist <= 20   (capture_distance_bps)  → edge zone for CROSS_IN
APPROACH  : 20 < dist < 75
OUT       : dist >= 75   (disarm_distance_bps)   → rearm prerequisite after cooldown
ARMED path: dist <= 50 and not IN               (arm_distance_bps)
```

CROSS_IN = transition into `IN` (prior not in edge zone). Bootstrap = already `IN` at start without prior outside.

## Profile window vs “30m”

| Concept | Value | Source |
|---------|-------|--------|
| Profile lookback / completed window | **30 minutes UTC** | `profile_window_minutes` / `OB_V3_FULL_OB_FR_PROFILE_WINDOW_MIN` |
| Meta field `bracket_minutes` | 30 | Same as window (name ≠ TPO bracket) |
| TPO mark size (when trades load) | **1 minute** | `int(ts.timestamp()) // 60` in `profiles.py` |
| Candle TF | none | Trades / volume bins, not klines |
| Capture segment / extension defaults | also 30 min | Unrelated to profile VA |

## Live vs disk (signal path)

| Module | Newer than PID 1565672 start? | Governs live signals? |
|--------|-------------------------------|------------------------|
| `profiles.py` | No | Yes (loaded at start) |
| `watcher.py` | No | Yes |
| `config.py` | No | Yes |
| `manager.py` / `continuity_contract.py` / resync pieces | Yes | **No** — disk ahead of process |

## Manifest proof (all 7 events)

- `profile_context.bracket_minutes = 30`
- `tpo_source = volume_proxy_fallback`
- `causality = trades_strictly_before_cutoff`
- `completeness = completed_window_only`
- Trigger edges: `TPO_VAH` or `TPO_VAL` with prices matching `volume_vah` / `volume_val` in the same file
