# Watcher Code Path — Signal vs FIGHT_ACTIVE

Paths relative to `orderbook_analyse/src/orderbook_analyse/orderbook_v2_live/full_ob_edge_flight_recorder/`.

## State machine flow

```text
poll_profiles (manager.py)
  ClickHouseCompletedProfileProvider.load (profiles.py)
    last_completed_window → build_profile → EdgeLevel[*]
  EdgeWatcher.set_edges (watcher.py)
    if CAPTURING|FIGHT_ACTIVE|POST_CAPTURE → pending_profile_update (deferred)
    else → _edges + frozen_profile updated

tick → evaluate (watcher.py) per symbol mid
  classify_zone_state: IN ≤20 bps, OUT ≥75 bps, else APPROACH
  lifecycle branches:
    FIGHT_ACTIVE|CAPTURING|POST_CAPTURE → action=extend (never trigger)
    REARMED|IDLE|BOOK_READY|ARMED → may action=trigger (CROSS_IN)

_start_or_merge_event (manager.py)
  if sym in _writers → _handle_open_event_tick only (no new event)
  else if CROSS_IN + edge_entry_crossed → new CapturePlan + writer

_handle_open_event_tick
  PROFILE_UPDATE_DURING_CAPTURE marker from pending_profile_update
  EDGE_RETOUCH / SECONDARY_EDGE_TRIGGER markers
  RESULT marker

_maybe_end_event
  extensions while acceptance_pending / in zone / etc.
  hard cap at hard_capture_end_ts
  finalize → end_capture → COOLDOWN → REARMED after disarm OUT
```

## Answers (Phase B)

| # | Question | Answer |
|---|----------|--------|
| 1 | Watcher during FIGHT_ACTIVE on each tick? | **Yes** — `manager.tick()` always calls `evaluate()` |
| 2 | New 30m profiles loaded during FIGHT_ACTIVE? | **Yes** — `poll_profiles` every 20s; `set_edges` stores `pending_profile_update`, emitted as `PROFILE_UPDATE_DURING_CAPTURE` marker |
| 3 | VAH/VAL frozen for whole event? | **Yes** — `begin_capture` sets `frozen_edges`; `evaluate` uses `st.frozen_edges or _edges` |
| 4 | Can new profile cause GENUINE_CROSS_IN during FIGHT_ACTIVE? | **No** — FIGHT_ACTIVE branch returns `action="extend"` only; CROSS_IN requires IDLE/ARMED/REARMED path |
| 5 | New profile contact handling | **Not ignored** — `PROFILE_UPDATE_DURING_CAPTURE` persisted; nearest-edge kind change → `SECONDARY_EDGE_TRIGGER` flood; **not** stored as GENUINE_CROSS_IN |
| 6 | Guard clause? | **Implicit** — `if st.lifecycle in {CAPTURING, FIGHT_ACTIVE, POST_CAPTURE}: return extend` (watcher.py L238–279). No separate `skip new signal` string, same effect |
| 7 | Block scope | **Both** — Signalerkennung as CROSS_IN blocked; second capture blocked via `_writers` guard |
| 8 | One open event per symbol? | **Yes** — `_writers` dict keyed by symbol; `max_parallel_events=2` across symbols |
| 9 | When ARMED again? | After `end_capture` → COOLDOWN (5 min) → zone OUT (≥75 bps) → REARMED; then CROSS_IN possible |
| 10 | Extensions to hard cap? | **Yes** — each extension adds up to 30 min to `normal_end_ts`, capped at `hard_capture_end_ts` (trigger + 180 min) |

## Key functions

| File | Function | Role |
|------|----------|------|
| `profiles.py` | `ClickHouseCompletedProfileProvider.load` | 30m completed VAH/VAL |
| `profiles.py` | `last_completed_window` | UTC window boundaries |
| `watcher.py` | `set_edges` | Defer updates during capture |
| `watcher.py` | `evaluate` | Zone + lifecycle decisions |
| `watcher.py` | `begin_capture` / `end_capture` | Freeze / release edges |
| `manager.py` | `poll_profiles` | 20s profile poll |
| `manager.py` | `_start_or_merge_event` | Hard gate CROSS_IN + single writer |
| `manager.py` | `_handle_open_event_tick` | Markers during open event |
| `manager.py` | `_maybe_end_event` | Extension + finalize |
| `capture_plan.py` | `classify_fight_extension` | Extension reason enum |
| `capture_plan.py` | `compute_normal_end` / `compute_hard_capture_end` | Timing contract |

## Thresholds (live defaults, unset env)

- Arm: 50 bps (approach subscription, not zone IN)
- Entry/CROSS_IN: 20 bps (zone IN)
- Rearm/OUT: 75 bps
