# Root Cause and Solution

**Generated (UTC):** `2026-09-05T15:12:26Z`

## Root cause (Phase 0 → Phase 1)

CLI cannot read another process's RAM ringbuffer. Existing unix socket exposes live Full-OB **point-in-time** snapshot only.

Separately, even in-process ringbuffer deltas are **not** a complete historical book tape without an anchor at/before the pre-roll start.

## Solution implemented

1. **Solution B:** periodic in-RAM full-book checkpoints when bridge enabled.
2. **RO bridge:** separate unix socket + atomic dump under fixed root.
3. **Reuse:** FR `BoundedRawRingBuffer.snapshot()` (no flush), `FullBookState` apply/replay rules, collector health hooks.
4. **Default off:** no live activation in this phase.

## Verdict rationale

`READY_FOR_CONTROLLED_RESTART` — offline gates passed; bridge disabled by default; no live process changed.
