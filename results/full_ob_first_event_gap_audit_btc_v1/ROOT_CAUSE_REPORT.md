# ROOT_CAUSE_REPORT — First BTC Full-OB Event u-Gap

## Verdict

**`RECONNECT_RESYNC_BOUNDARY_CONFIRMED`**

### Primary cause (file / class / function)

1. `orderbook_v2_live/collector.py` — `_run_ws_session`: `DeadConnection("stale_market_data")` when no WS topic arrives for `stale_data_sec` (15s).
2. Same reconnect path calls `full_book.on_reconnect()`.
3. `orderbook_v2_live/on_demand_full.py` — `OnDemandFullBookManager.on_reconnect()`: clears book + sync buffer, increments `reconnect_count`, does **not** increment `gap_count`.
4. `subscribe_symbol()` / `_apply_rest_snapshot()`: in-memory REST resync → persisted packets resume as `flight_phase=buffer`, `apply_outcome=accepted`.
5. Persistence consequence: `full_ob_edge_flight_recorder/event_writer.py` — `ActiveEventWriter._note_continuity()` counts forward `u` holes → `persisted_capture_u_gap_count`.

### Amplifier

No mid-event REST snapshot is written into the FR event directory after reconnect, so offline replay cannot open a new self-contained epoch from capture alone.

## First gap boundary

| Field | Value |
|---|---|
| Source | finalized segment 0 `full_ob_raw_deltas.jsonl.zst` |
| Prev | line 3772, u=**4350204**, seq=805051079937, ts=1788509523272 |
| Expected | u=**4350205** |
| Actual | line 3774, u=**4350353**, seq=805051320556, ts=1788509553073 |
| Missing | **148** u |
| Δ exchange / receive | **~29801 ms / ~29902 ms** |
| Prev record | delta / live / applied |
| Gap packet | delta / buffer / accepted |
| Marker between | PROFILE_UPDATE_DURING_CAPTURE (excluded from u+1) |
| Wall clock | 2026-09-04T08:12:03.272Z → 08:12:33.073Z |
| Reconnect log | `2026-09-04 08:12:17,668 WARNING ... reconnect after stale_market_data` |

Four forward gaps in segment 0 (missing 148+153+199+252). All **before** segment rollover (08:35). Open `cont_001/*.tmp` not read.

## Answers

| Item | Result |
|---|---|
| Source-feed apply GAP count | **0** (reconnect bypasses `DeltaOutcome.GAP`) |
| Local receive silence ≥15s | **Yes** (stale watchdog); Bybit-vs-transport not independently proven |
| Persistence-only writer drop | **No** |
| Segment rollover involved | **No** |
| Reconnect/Resync | **Yes — primary** |
| Queue/Writer drops | **No** (0) |
| Markers falsely counted | **No** |
| False-positive gap accounting | **No** |

## Continuity layers

- `source_feed_u_gap_count = 0`
- `persisted_capture_u_gap_count = 4`
- `database_replay_u_gap_count` (audit window) = 2 — CH matches source, does not hide gaps

## Replay / research

`EVENT_NOT_SELF_CONTAINED_ACROSS_GAP` — continuous replay fails at boundary; `research_eligible=false`.

## ClickHouse / Dedup

- Gap parity: **True**
- Dedup without OPTIMIZE: physical 406 → logical 203; naive/dedup ratio 2.0; passed=True

## Safety

- Collector PID 1565672 alive=True (untouched)
- OI PID 147111 alive=True (untouched)
- Finalized delta+snapshot SHA unchanged; `event_manifest.json` may still change while event is live
- No prod CH mutation; no commit/push; **no code changes**

## Follow-up (not activated)

Persist post-reconnect REST checkpoint + `RECONNECT_RESYNC` marker into open FR events (requires explicit restart approval).
