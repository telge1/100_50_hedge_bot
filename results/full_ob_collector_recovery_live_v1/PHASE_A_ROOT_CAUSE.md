# PHASE_A_ROOT_CAUSE — Full OB Collector Stop

**UTC:** 2026-09-04T19:37:52Z

## Classification

```text
PROCESS_CRASH
```

## Evidence

| Field | Value |
|-------|-------|
| Last PID | **1692334** (lock file `logs/orderbook_v3_raw_archive_only.lock`) |
| Mechanism | `nohup` via `scripts/start_orderbook_v3_raw_archive_btc_doge.sh` (no systemd unit) |
| Mode | `raw-archive-only` + Full Book + Flight Recorder (`.env` FR enable=true) |
| Last health | `collector_state=STOPPED` at ~2026-09-04 18:30:36 local; `last_error=stale_market_data` on final health line |
| Log crash | `IndexError: pop from empty list` in `nested_profile_signal.py:_evict_if_needed` |
| Stack | `manager.tick` → `_register_nested_profile_from_pending` → `register_profile` → `_evict_if_needed` → `ordered.pop(0)` |
| Why empty pop | Eviction marked `PROFILE_EXPIRED` but **never removed** profile from `_profiles[sym]`, so `len(by_sym)` never shrank |
| OOM | Kernel OOM events exist earlier (other PIDs); **not** PID 1692334 at stop time |
| Systemd | No user unit for this collector → **no auto-restart** after crash |
| Lock after death | Stale PID text `1692334` remains; process absent |

## Why service did not come back

Not a systemd service (`Restart=` N/A). Started under nohup; crash exited process; no supervisor restarted it. Dashboard correctly shows STOPPED.

## Offline fix applied (before any restart)

`by_sym.pop(victim.profile_id, None)` added in `_evict_if_needed` + regression test. Full-OB related pytest: **84 passed**.
