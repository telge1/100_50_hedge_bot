# nested_profile_edge_signal_v1 — Design Proposal (NOT IMPLEMENTED)

## Problem

During `FIGHT_ACTIVE`, the watcher continues ticking and new 30m profiles complete, but:
- `GENUINE_CROSS_IN` cannot fire (lifecycle branch + single-writer gate).
- Contacts with **new** profile edges appear only as `SECONDARY_EDGE_TRIGGER` / `PROFILE_UPDATE_DURING_CAPTURE` — not research-grade nested signals.

## Goals

1. Keep **one** Full-OB capture stream per symbol (`fight_event_id` unchanged).
2. Persist every **valid** nested profile-edge signal with full provenance.
3. Distinguish bootstrap, old-edge retouch, and new-profile edge contact.
4. Allow nested signals to **extend** capture timing only (reuse existing extension machinery).

## Record schema (proposed)

File: `{event_root}/nested_signals.jsonl` (append-only, zstd optional)

```json
{
  "schema": "nested_profile_edge_signal_v1",
  "nested_signal_id": "BTCUSDT_20260904T090000Z_ns_a1b2c3",
  "parent_fight_event_id": "BTCUSDT_20260904T080534Z_1fd9a66d36",
  "continuity_epoch_id": 4,
  "symbol": "BTCUSDT",
  "signal_ts": "2026-09-04T09:00:12.345Z",
  "signal_quality": "NESTED_CROSS_IN",
  "profile_id": "BTCUSDT_20260904T083000_090000",
  "profile_start": "2026-09-04T08:30:00Z",
  "profile_end": "2026-09-04T09:00:00Z",
  "profile_basis": "volume_proxy_fallback",
  "vah": 81120.0,
  "val": 81010.0,
  "poc": 81117.5,
  "edge_kind": "TPO_VAH",
  "edge_side": "UPPER",
  "edge_price": 81120.0,
  "market_price_at_signal": 81102.69,
  "distance_bps": 2.13,
  "prior_zone_state": "OUT",
  "trigger_zone_state": "IN",
  "arm_history": [{"ts": "...", "dist_bps": 48.2, "zone": "APPROACH"}],
  "supersedes_profile_id": "BTCUSDT_20260904T073000_080000",
  "classification": "NEW_PROFILE_EDGE",
  "extends_capture": true,
  "extension_reason": "NESTED_PROFILE_EDGE_CONTACT"
}
```

## Classification rules

| Condition | Class | Stored as |
|-----------|-------|-----------|
| Start already IN, no prior outside | Bootstrap | audit log only (existing) |
| Contact frozen trigger edge after breakout/reclaim | `OLD_EDGE_RETOUCH` | marker (existing) + optional nested row |
| New completed profile; price enters ≤20 bps of **new** VAH/VAL; prior zone not IN | `NESTED_CROSS_IN` | nested_signals.jsonl |
| Nearest edge kind change on frozen set only | `SECONDARY_EDGE_TRIGGER` | keep; do not promote to nested unless new profile id differs |

## Watcher hook (future)

After `PROFILE_UPDATE_DURING_CAPTURE` dequeue in `_handle_open_event_tick`:

1. Evaluate **new** profile edges (not frozen) in shadow mode.
2. If `entered IN` from non-IN on new edge set → emit nested signal record.
3. Do **not** call `_start_or_merge_event` (no second writer).
4. Optionally bump `plan.normal_end_ts` via existing extension classifier.

## Invariants

- At most one open writer per symbol.
- Nested signals never open `.tmp` segments.
- Parent `fight_event_id` immutable.
- `continuity_epoch_id` copied from active segment writer.
- Bootstrap and old-edge retouch must never receive `signal_quality=NESTED_CROSS_IN`.

## Migration

- Backfill not required for pilot.
- Replay tools gain optional nested signal index alongside markers.
