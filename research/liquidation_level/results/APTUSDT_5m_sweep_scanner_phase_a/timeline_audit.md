# Phase A Timeline Audit

Sweep is **not** an entry. Snapshots freeze scanner state at sweep close.

Sampled events: 50

## OPT_000009 @ 2025-12-27T12:20:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-27T12:20:00+00:00` → `2025-12-27T12:25:00+00:00`
- Prev/next 5m: `2025-12-27T12:15:00+00:00` / `2025-12-27T12:25:00+00:00`
- Used 15m: `2025-12-27T12:00:00+00:00`–`2025-12-27T12:15:00+00:00` (available `2025-12-27T12:15:00+00:00`)
- Forming 15m (excluded if open): `2025-12-27T12:15:00+00:00`–`2025-12-27T12:30:00+00:00`
  - Reason: 15m bucket 2025-12-27 12:15:00+00:00–2025-12-27 12:30:00+00:00 still open at decision 2025-12-27 12:25:00+00:00; close_time > decision_time
- Used 30m: `2025-12-27T11:30:00+00:00`–`2025-12-27T12:00:00+00:00` (available `2025-12-27T12:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-27T12:00:00+00:00`–`2025-12-27T12:30:00+00:00`
  - Reason: 30m bucket 2025-12-27 12:00:00+00:00–2025-12-27 12:30:00+00:00 still open at decision 2025-12-27 12:25:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=25.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000012 @ 2025-12-27T14:45:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-27T14:45:00+00:00` → `2025-12-27T14:50:00+00:00`
- Prev/next 5m: `2025-12-27T14:40:00+00:00` / `2025-12-27T14:50:00+00:00`
- Used 15m: `2025-12-27T14:30:00+00:00`–`2025-12-27T14:45:00+00:00` (available `2025-12-27T14:45:00+00:00`)
- Forming 15m (excluded if open): `2025-12-27T14:45:00+00:00`–`2025-12-27T15:00:00+00:00`
  - Reason: 15m bucket 2025-12-27 14:45:00+00:00–2025-12-27 15:00:00+00:00 still open at decision 2025-12-27 14:50:00+00:00; close_time > decision_time
- Used 30m: `2025-12-27T14:00:00+00:00`–`2025-12-27T14:30:00+00:00` (available `2025-12-27T14:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-27T14:30:00+00:00`–`2025-12-27T15:00:00+00:00`
  - Reason: 30m bucket 2025-12-27 14:30:00+00:00–2025-12-27 15:00:00+00:00 still open at decision 2025-12-27 14:50:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=20.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000016 @ 2025-12-27T15:00:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-27T15:00:00+00:00` → `2025-12-27T15:05:00+00:00`
- Prev/next 5m: `2025-12-27T14:55:00+00:00` / `2025-12-27T15:05:00+00:00`
- Used 15m: `2025-12-27T14:45:00+00:00`–`2025-12-27T15:00:00+00:00` (available `2025-12-27T15:00:00+00:00`)
- Forming 15m (excluded if open): `2025-12-27T15:00:00+00:00`–`2025-12-27T15:15:00+00:00`
  - Reason: 15m bucket 2025-12-27 15:00:00+00:00–2025-12-27 15:15:00+00:00 still open at decision 2025-12-27 15:05:00+00:00; close_time > decision_time
- Used 30m: `2025-12-27T14:30:00+00:00`–`2025-12-27T15:00:00+00:00` (available `2025-12-27T15:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-27T15:00:00+00:00`–`2025-12-27T15:30:00+00:00`
  - Reason: 30m bucket 2025-12-27 15:00:00+00:00–2025-12-27 15:30:00+00:00 still open at decision 2025-12-27 15:05:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000022 @ 2025-12-27T23:10:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-27T23:10:00+00:00` → `2025-12-27T23:15:00+00:00`
- Prev/next 5m: `2025-12-27T23:05:00+00:00` / `2025-12-27T23:15:00+00:00`
- Used 15m: `2025-12-27T23:00:00+00:00`–`2025-12-27T23:15:00+00:00` (available `2025-12-27T23:15:00+00:00`)
- Forming 15m (excluded if open): `2025-12-27T23:15:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 15m bucket 2025-12-27 23:15:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Used 30m: `2025-12-27T22:30:00+00:00`–`2025-12-27T23:00:00+00:00` (available `2025-12-27T23:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-27T23:00:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 30m bucket 2025-12-27 23:00:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000025 @ 2025-12-27T23:10:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-27T23:10:00+00:00` → `2025-12-27T23:15:00+00:00`
- Prev/next 5m: `2025-12-27T23:05:00+00:00` / `2025-12-27T23:15:00+00:00`
- Used 15m: `2025-12-27T23:00:00+00:00`–`2025-12-27T23:15:00+00:00` (available `2025-12-27T23:15:00+00:00`)
- Forming 15m (excluded if open): `2025-12-27T23:15:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 15m bucket 2025-12-27 23:15:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Used 30m: `2025-12-27T22:30:00+00:00`–`2025-12-27T23:00:00+00:00` (available `2025-12-27T23:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-27T23:00:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 30m bucket 2025-12-27 23:00:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000026 @ 2025-12-27T23:10:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-27T23:10:00+00:00` → `2025-12-27T23:15:00+00:00`
- Prev/next 5m: `2025-12-27T23:05:00+00:00` / `2025-12-27T23:15:00+00:00`
- Used 15m: `2025-12-27T23:00:00+00:00`–`2025-12-27T23:15:00+00:00` (available `2025-12-27T23:15:00+00:00`)
- Forming 15m (excluded if open): `2025-12-27T23:15:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 15m bucket 2025-12-27 23:15:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Used 30m: `2025-12-27T22:30:00+00:00`–`2025-12-27T23:00:00+00:00` (available `2025-12-27T23:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-27T23:00:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 30m bucket 2025-12-27 23:00:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000027 @ 2025-12-27T23:10:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-27T23:10:00+00:00` → `2025-12-27T23:15:00+00:00`
- Prev/next 5m: `2025-12-27T23:05:00+00:00` / `2025-12-27T23:15:00+00:00`
- Used 15m: `2025-12-27T23:00:00+00:00`–`2025-12-27T23:15:00+00:00` (available `2025-12-27T23:15:00+00:00`)
- Forming 15m (excluded if open): `2025-12-27T23:15:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 15m bucket 2025-12-27 23:15:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Used 30m: `2025-12-27T22:30:00+00:00`–`2025-12-27T23:00:00+00:00` (available `2025-12-27T23:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-27T23:00:00+00:00`–`2025-12-27T23:30:00+00:00`
  - Reason: 30m bucket 2025-12-27 23:00:00+00:00–2025-12-27 23:30:00+00:00 still open at decision 2025-12-27 23:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000035 @ 2025-12-29T00:20:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T00:20:00+00:00` → `2025-12-29T00:25:00+00:00`
- Prev/next 5m: `2025-12-29T00:15:00+00:00` / `2025-12-29T00:25:00+00:00`
- Used 15m: `2025-12-29T00:00:00+00:00`–`2025-12-29T00:15:00+00:00` (available `2025-12-29T00:15:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T00:15:00+00:00`–`2025-12-29T00:30:00+00:00`
  - Reason: 15m bucket 2025-12-29 00:15:00+00:00–2025-12-29 00:30:00+00:00 still open at decision 2025-12-29 00:25:00+00:00; close_time > decision_time
- Used 30m: `2025-12-28T23:30:00+00:00`–`2025-12-29T00:00:00+00:00` (available `2025-12-29T00:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T00:00:00+00:00`–`2025-12-29T00:30:00+00:00`
  - Reason: 30m bucket 2025-12-29 00:00:00+00:00–2025-12-29 00:30:00+00:00 still open at decision 2025-12-29 00:25:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=25.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000041 @ 2025-12-29T00:30:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T00:30:00+00:00` → `2025-12-29T00:35:00+00:00`
- Prev/next 5m: `2025-12-29T00:25:00+00:00` / `2025-12-29T00:35:00+00:00`
- Used 15m: `2025-12-29T00:15:00+00:00`–`2025-12-29T00:30:00+00:00` (available `2025-12-29T00:30:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T00:30:00+00:00`–`2025-12-29T00:45:00+00:00`
  - Reason: 15m bucket 2025-12-29 00:30:00+00:00–2025-12-29 00:45:00+00:00 still open at decision 2025-12-29 00:35:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T00:00:00+00:00`–`2025-12-29T00:30:00+00:00` (available `2025-12-29T00:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T00:30:00+00:00`–`2025-12-29T01:00:00+00:00`
  - Reason: 30m bucket 2025-12-29 00:30:00+00:00–2025-12-29 01:00:00+00:00 still open at decision 2025-12-29 00:35:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000044 @ 2025-12-29T00:30:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T00:30:00+00:00` → `2025-12-29T00:35:00+00:00`
- Prev/next 5m: `2025-12-29T00:25:00+00:00` / `2025-12-29T00:35:00+00:00`
- Used 15m: `2025-12-29T00:15:00+00:00`–`2025-12-29T00:30:00+00:00` (available `2025-12-29T00:30:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T00:30:00+00:00`–`2025-12-29T00:45:00+00:00`
  - Reason: 15m bucket 2025-12-29 00:30:00+00:00–2025-12-29 00:45:00+00:00 still open at decision 2025-12-29 00:35:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T00:00:00+00:00`–`2025-12-29T00:30:00+00:00` (available `2025-12-29T00:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T00:30:00+00:00`–`2025-12-29T01:00:00+00:00`
  - Reason: 30m bucket 2025-12-29 00:30:00+00:00–2025-12-29 01:00:00+00:00 still open at decision 2025-12-29 00:35:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015320 @ 2026-06-26T13:55:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T13:55:00+00:00` → `2026-06-26T14:00:00+00:00`
- Prev/next 5m: `2026-06-26T13:50:00+00:00` / `2026-06-26T14:00:00+00:00`
- Used 15m: `2026-06-26T13:45:00+00:00`–`2026-06-26T14:00:00+00:00` (available `2026-06-26T14:00:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T14:00:00+00:00`–`2026-06-26T14:15:00+00:00`
  - Reason: 15m bucket 2026-06-26 14:00:00+00:00–2026-06-26 14:15:00+00:00 still open at decision 2026-06-26 14:00:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T13:30:00+00:00`–`2026-06-26T14:00:00+00:00` (available `2026-06-26T14:00:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T14:00:00+00:00`–`2026-06-26T14:30:00+00:00`
  - Reason: 30m bucket 2026-06-26 14:00:00+00:00–2026-06-26 14:30:00+00:00 still open at decision 2026-06-26 14:00:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015327 @ 2026-06-26T15:25:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T15:25:00+00:00` → `2026-06-26T15:30:00+00:00`
- Prev/next 5m: `2026-06-26T15:20:00+00:00` / `2026-06-26T15:30:00+00:00`
- Used 15m: `2026-06-26T15:15:00+00:00`–`2026-06-26T15:30:00+00:00` (available `2026-06-26T15:30:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T15:30:00+00:00`–`2026-06-26T15:45:00+00:00`
  - Reason: 15m bucket 2026-06-26 15:30:00+00:00–2026-06-26 15:45:00+00:00 still open at decision 2026-06-26 15:30:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T15:00:00+00:00`–`2026-06-26T15:30:00+00:00` (available `2026-06-26T15:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T15:30:00+00:00`–`2026-06-26T16:00:00+00:00`
  - Reason: 30m bucket 2026-06-26 15:30:00+00:00–2026-06-26 16:00:00+00:00 still open at decision 2026-06-26 15:30:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015336 @ 2026-06-26T16:00:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T16:00:00+00:00` → `2026-06-26T16:05:00+00:00`
- Prev/next 5m: `2026-06-26T15:55:00+00:00` / `2026-06-26T16:05:00+00:00`
- Used 15m: `2026-06-26T15:45:00+00:00`–`2026-06-26T16:00:00+00:00` (available `2026-06-26T16:00:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T16:00:00+00:00`–`2026-06-26T16:15:00+00:00`
  - Reason: 15m bucket 2026-06-26 16:00:00+00:00–2026-06-26 16:15:00+00:00 still open at decision 2026-06-26 16:05:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T15:30:00+00:00`–`2026-06-26T16:00:00+00:00` (available `2026-06-26T16:00:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T16:00:00+00:00`–`2026-06-26T16:30:00+00:00`
  - Reason: 30m bucket 2026-06-26 16:00:00+00:00–2026-06-26 16:30:00+00:00 still open at decision 2026-06-26 16:05:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015337 @ 2026-06-26T16:00:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T16:00:00+00:00` → `2026-06-26T16:05:00+00:00`
- Prev/next 5m: `2026-06-26T15:55:00+00:00` / `2026-06-26T16:05:00+00:00`
- Used 15m: `2026-06-26T15:45:00+00:00`–`2026-06-26T16:00:00+00:00` (available `2026-06-26T16:00:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T16:00:00+00:00`–`2026-06-26T16:15:00+00:00`
  - Reason: 15m bucket 2026-06-26 16:00:00+00:00–2026-06-26 16:15:00+00:00 still open at decision 2026-06-26 16:05:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T15:30:00+00:00`–`2026-06-26T16:00:00+00:00` (available `2026-06-26T16:00:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T16:00:00+00:00`–`2026-06-26T16:30:00+00:00`
  - Reason: 30m bucket 2026-06-26 16:00:00+00:00–2026-06-26 16:30:00+00:00 still open at decision 2026-06-26 16:05:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015354 @ 2026-06-26T16:35:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T16:35:00+00:00` → `2026-06-26T16:40:00+00:00`
- Prev/next 5m: `2026-06-26T16:30:00+00:00` / `2026-06-26T16:40:00+00:00`
- Used 15m: `2026-06-26T16:15:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T16:45:00+00:00`
  - Reason: 15m bucket 2026-06-26 16:30:00+00:00–2026-06-26 16:45:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T16:00:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T17:00:00+00:00`
  - Reason: 30m bucket 2026-06-26 16:30:00+00:00–2026-06-26 17:00:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=10.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015355 @ 2026-06-26T16:35:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T16:35:00+00:00` → `2026-06-26T16:40:00+00:00`
- Prev/next 5m: `2026-06-26T16:30:00+00:00` / `2026-06-26T16:40:00+00:00`
- Used 15m: `2026-06-26T16:15:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T16:45:00+00:00`
  - Reason: 15m bucket 2026-06-26 16:30:00+00:00–2026-06-26 16:45:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T16:00:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T17:00:00+00:00`
  - Reason: 30m bucket 2026-06-26 16:30:00+00:00–2026-06-26 17:00:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=10.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015362 @ 2026-06-26T16:35:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T16:35:00+00:00` → `2026-06-26T16:40:00+00:00`
- Prev/next 5m: `2026-06-26T16:30:00+00:00` / `2026-06-26T16:40:00+00:00`
- Used 15m: `2026-06-26T16:15:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T16:45:00+00:00`
  - Reason: 15m bucket 2026-06-26 16:30:00+00:00–2026-06-26 16:45:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T16:00:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T17:00:00+00:00`
  - Reason: 30m bucket 2026-06-26 16:30:00+00:00–2026-06-26 17:00:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=10.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015370 @ 2026-06-26T16:35:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T16:35:00+00:00` → `2026-06-26T16:40:00+00:00`
- Prev/next 5m: `2026-06-26T16:30:00+00:00` / `2026-06-26T16:40:00+00:00`
- Used 15m: `2026-06-26T16:15:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T16:45:00+00:00`
  - Reason: 15m bucket 2026-06-26 16:30:00+00:00–2026-06-26 16:45:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T16:00:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T17:00:00+00:00`
  - Reason: 30m bucket 2026-06-26 16:30:00+00:00–2026-06-26 17:00:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=10.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015379 @ 2026-06-26T16:35:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-26T16:35:00+00:00` → `2026-06-26T16:40:00+00:00`
- Prev/next 5m: `2026-06-26T16:30:00+00:00` / `2026-06-26T16:40:00+00:00`
- Used 15m: `2026-06-26T16:15:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 15m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T16:45:00+00:00`
  - Reason: 15m bucket 2026-06-26 16:30:00+00:00–2026-06-26 16:45:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Used 30m: `2026-06-26T16:00:00+00:00`–`2026-06-26T16:30:00+00:00` (available `2026-06-26T16:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-26T16:30:00+00:00`–`2026-06-26T17:00:00+00:00`
  - Reason: 30m bucket 2026-06-26 16:30:00+00:00–2026-06-26 17:00:00+00:00 still open at decision 2026-06-26 16:40:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=10.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_015387 @ 2026-06-27T02:50:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-27T02:50:00+00:00` → `2026-06-27T02:55:00+00:00`
- Prev/next 5m: `2026-06-27T02:45:00+00:00` / `2026-06-27T02:55:00+00:00`
- Used 15m: `2026-06-27T02:30:00+00:00`–`2026-06-27T02:45:00+00:00` (available `2026-06-27T02:45:00+00:00`)
- Forming 15m (excluded if open): `2026-06-27T02:45:00+00:00`–`2026-06-27T03:00:00+00:00`
  - Reason: 15m bucket 2026-06-27 02:45:00+00:00–2026-06-27 03:00:00+00:00 still open at decision 2026-06-27 02:55:00+00:00; close_time > decision_time
- Used 30m: `2026-06-27T02:00:00+00:00`–`2026-06-27T02:30:00+00:00` (available `2026-06-27T02:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-27T02:30:00+00:00`–`2026-06-27T03:00:00+00:00`
  - Reason: 30m bucket 2026-06-27 02:30:00+00:00–2026-06-27 03:00:00+00:00 still open at decision 2026-06-27 02:55:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=25.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000758 @ 2026-01-05T01:35:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-01-05T01:35:00+00:00` → `2026-01-05T01:40:00+00:00`
- Prev/next 5m: `2026-01-05T01:30:00+00:00` / `2026-01-05T01:40:00+00:00`
- Used 15m: `2026-01-05T01:15:00+00:00`–`2026-01-05T01:30:00+00:00` (available `2026-01-05T01:30:00+00:00`)
- Forming 15m (excluded if open): `2026-01-05T01:30:00+00:00`–`2026-01-05T01:45:00+00:00`
  - Reason: 15m bucket 2026-01-05 01:30:00+00:00–2026-01-05 01:45:00+00:00 still open at decision 2026-01-05 01:40:00+00:00; close_time > decision_time
- Used 30m: `2026-01-05T01:00:00+00:00`–`2026-01-05T01:30:00+00:00` (available `2026-01-05T01:30:00+00:00`)
- Forming 30m (excluded if open): `2026-01-05T01:30:00+00:00`–`2026-01-05T02:00:00+00:00`
  - Reason: 30m bucket 2026-01-05 01:30:00+00:00–2026-01-05 02:00:00+00:00 still open at decision 2026-01-05 01:40:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=10.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000785 @ 2026-01-05T15:30:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-01-05T15:30:00+00:00` → `2026-01-05T15:35:00+00:00`
- Prev/next 5m: `2026-01-05T15:25:00+00:00` / `2026-01-05T15:35:00+00:00`
- Used 15m: `2026-01-05T15:15:00+00:00`–`2026-01-05T15:30:00+00:00` (available `2026-01-05T15:30:00+00:00`)
- Forming 15m (excluded if open): `2026-01-05T15:30:00+00:00`–`2026-01-05T15:45:00+00:00`
  - Reason: 15m bucket 2026-01-05 15:30:00+00:00–2026-01-05 15:45:00+00:00 still open at decision 2026-01-05 15:35:00+00:00; close_time > decision_time
- Used 30m: `2026-01-05T15:00:00+00:00`–`2026-01-05T15:30:00+00:00` (available `2026-01-05T15:30:00+00:00`)
- Forming 30m (excluded if open): `2026-01-05T15:30:00+00:00`–`2026-01-05T16:00:00+00:00`
  - Reason: 30m bucket 2026-01-05 15:30:00+00:00–2026-01-05 16:00:00+00:00 still open at decision 2026-01-05 15:35:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000797 @ 2026-01-05T15:40:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-01-05T15:40:00+00:00` → `2026-01-05T15:45:00+00:00`
- Prev/next 5m: `2026-01-05T15:35:00+00:00` / `2026-01-05T15:45:00+00:00`
- Used 15m: `2026-01-05T15:30:00+00:00`–`2026-01-05T15:45:00+00:00` (available `2026-01-05T15:45:00+00:00`)
- Forming 15m (excluded if open): `2026-01-05T15:45:00+00:00`–`2026-01-05T16:00:00+00:00`
  - Reason: 15m bucket 2026-01-05 15:45:00+00:00–2026-01-05 16:00:00+00:00 still open at decision 2026-01-05 15:45:00+00:00; close_time > decision_time
- Used 30m: `2026-01-05T15:00:00+00:00`–`2026-01-05T15:30:00+00:00` (available `2026-01-05T15:30:00+00:00`)
- Forming 30m (excluded if open): `2026-01-05T15:30:00+00:00`–`2026-01-05T16:00:00+00:00`
  - Reason: 30m bucket 2026-01-05 15:30:00+00:00–2026-01-05 16:00:00+00:00 still open at decision 2026-01-05 15:45:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_001883 @ 2026-01-21T02:40:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-01-21T02:40:00+00:00` → `2026-01-21T02:45:00+00:00`
- Prev/next 5m: `2026-01-21T02:35:00+00:00` / `2026-01-21T02:45:00+00:00`
- Used 15m: `2026-01-21T02:30:00+00:00`–`2026-01-21T02:45:00+00:00` (available `2026-01-21T02:45:00+00:00`)
- Forming 15m (excluded if open): `2026-01-21T02:45:00+00:00`–`2026-01-21T03:00:00+00:00`
  - Reason: 15m bucket 2026-01-21 02:45:00+00:00–2026-01-21 03:00:00+00:00 still open at decision 2026-01-21 02:45:00+00:00; close_time > decision_time
- Used 30m: `2026-01-21T02:00:00+00:00`–`2026-01-21T02:30:00+00:00` (available `2026-01-21T02:30:00+00:00`)
- Forming 30m (excluded if open): `2026-01-21T02:30:00+00:00`–`2026-01-21T03:00:00+00:00`
  - Reason: 30m bucket 2026-01-21 02:30:00+00:00–2026-01-21 03:00:00+00:00 still open at decision 2026-01-21 02:45:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_003934 @ 2026-02-17T12:10:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-02-17T12:10:00+00:00` → `2026-02-17T12:15:00+00:00`
- Prev/next 5m: `2026-02-17T12:05:00+00:00` / `2026-02-17T12:15:00+00:00`
- Used 15m: `2026-02-17T12:00:00+00:00`–`2026-02-17T12:15:00+00:00` (available `2026-02-17T12:15:00+00:00`)
- Forming 15m (excluded if open): `2026-02-17T12:15:00+00:00`–`2026-02-17T12:30:00+00:00`
  - Reason: 15m bucket 2026-02-17 12:15:00+00:00–2026-02-17 12:30:00+00:00 still open at decision 2026-02-17 12:15:00+00:00; close_time > decision_time
- Used 30m: `2026-02-17T11:30:00+00:00`–`2026-02-17T12:00:00+00:00` (available `2026-02-17T12:00:00+00:00`)
- Forming 30m (excluded if open): `2026-02-17T12:00:00+00:00`–`2026-02-17T12:30:00+00:00`
  - Reason: 30m bucket 2026-02-17 12:00:00+00:00–2026-02-17 12:30:00+00:00 still open at decision 2026-02-17 12:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_003986 @ 2026-02-19T18:45:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-02-19T18:45:00+00:00` → `2026-02-19T18:50:00+00:00`
- Prev/next 5m: `2026-02-19T18:40:00+00:00` / `2026-02-19T18:50:00+00:00`
- Used 15m: `2026-02-19T18:30:00+00:00`–`2026-02-19T18:45:00+00:00` (available `2026-02-19T18:45:00+00:00`)
- Forming 15m (excluded if open): `2026-02-19T18:45:00+00:00`–`2026-02-19T19:00:00+00:00`
  - Reason: 15m bucket 2026-02-19 18:45:00+00:00–2026-02-19 19:00:00+00:00 still open at decision 2026-02-19 18:50:00+00:00; close_time > decision_time
- Used 30m: `2026-02-19T18:00:00+00:00`–`2026-02-19T18:30:00+00:00` (available `2026-02-19T18:30:00+00:00`)
- Forming 30m (excluded if open): `2026-02-19T18:30:00+00:00`–`2026-02-19T19:00:00+00:00`
  - Reason: 30m bucket 2026-02-19 18:30:00+00:00–2026-02-19 19:00:00+00:00 still open at decision 2026-02-19 18:50:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=20.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_006203 @ 2026-03-10T09:20:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-03-10T09:20:00+00:00` → `2026-03-10T09:25:00+00:00`
- Prev/next 5m: `2026-03-10T09:15:00+00:00` / `2026-03-10T09:25:00+00:00`
- Used 15m: `2026-03-10T09:00:00+00:00`–`2026-03-10T09:15:00+00:00` (available `2026-03-10T09:15:00+00:00`)
- Forming 15m (excluded if open): `2026-03-10T09:15:00+00:00`–`2026-03-10T09:30:00+00:00`
  - Reason: 15m bucket 2026-03-10 09:15:00+00:00–2026-03-10 09:30:00+00:00 still open at decision 2026-03-10 09:25:00+00:00; close_time > decision_time
- Used 30m: `2026-03-10T08:30:00+00:00`–`2026-03-10T09:00:00+00:00` (available `2026-03-10T09:00:00+00:00`)
- Forming 30m (excluded if open): `2026-03-10T09:00:00+00:00`–`2026-03-10T09:30:00+00:00`
  - Reason: 30m bucket 2026-03-10 09:00:00+00:00–2026-03-10 09:30:00+00:00 still open at decision 2026-03-10 09:25:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=25.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_006756 @ 2026-03-16T05:55:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-03-16T05:55:00+00:00` → `2026-03-16T06:00:00+00:00`
- Prev/next 5m: `2026-03-16T05:50:00+00:00` / `2026-03-16T06:00:00+00:00`
- Used 15m: `2026-03-16T05:45:00+00:00`–`2026-03-16T06:00:00+00:00` (available `2026-03-16T06:00:00+00:00`)
- Forming 15m (excluded if open): `2026-03-16T06:00:00+00:00`–`2026-03-16T06:15:00+00:00`
  - Reason: 15m bucket 2026-03-16 06:00:00+00:00–2026-03-16 06:15:00+00:00 still open at decision 2026-03-16 06:00:00+00:00; close_time > decision_time
- Used 30m: `2026-03-16T05:30:00+00:00`–`2026-03-16T06:00:00+00:00` (available `2026-03-16T06:00:00+00:00`)
- Forming 30m (excluded if open): `2026-03-16T06:00:00+00:00`–`2026-03-16T06:30:00+00:00`
  - Reason: 30m bucket 2026-03-16 06:00:00+00:00–2026-03-16 06:30:00+00:00 still open at decision 2026-03-16 06:00:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_007813 @ 2026-03-24T05:40:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-03-24T05:40:00+00:00` → `2026-03-24T05:45:00+00:00`
- Prev/next 5m: `2026-03-24T05:35:00+00:00` / `2026-03-24T05:45:00+00:00`
- Used 15m: `2026-03-24T05:30:00+00:00`–`2026-03-24T05:45:00+00:00` (available `2026-03-24T05:45:00+00:00`)
- Forming 15m (excluded if open): `2026-03-24T05:45:00+00:00`–`2026-03-24T06:00:00+00:00`
  - Reason: 15m bucket 2026-03-24 05:45:00+00:00–2026-03-24 06:00:00+00:00 still open at decision 2026-03-24 05:45:00+00:00; close_time > decision_time
- Used 30m: `2026-03-24T05:00:00+00:00`–`2026-03-24T05:30:00+00:00` (available `2026-03-24T05:30:00+00:00`)
- Forming 30m (excluded if open): `2026-03-24T05:30:00+00:00`–`2026-03-24T06:00:00+00:00`
  - Reason: 30m bucket 2026-03-24 05:30:00+00:00–2026-03-24 06:00:00+00:00 still open at decision 2026-03-24 05:45:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_008669 @ 2026-04-13T11:55:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2026-04-13T11:55:00+00:00` → `2026-04-13T12:00:00+00:00`
- Prev/next 5m: `2026-04-13T11:50:00+00:00` / `2026-04-13T12:00:00+00:00`
- Used 15m: `2026-04-13T11:45:00+00:00`–`2026-04-13T12:00:00+00:00` (available `2026-04-13T12:00:00+00:00`)
- Forming 15m (excluded if open): `2026-04-13T12:00:00+00:00`–`2026-04-13T12:15:00+00:00`
  - Reason: 15m bucket 2026-04-13 12:00:00+00:00–2026-04-13 12:15:00+00:00 still open at decision 2026-04-13 12:00:00+00:00; close_time > decision_time
- Used 30m: `2026-04-13T11:30:00+00:00`–`2026-04-13T12:00:00+00:00` (available `2026-04-13T12:00:00+00:00`)
- Forming 30m (excluded if open): `2026-04-13T12:00:00+00:00`–`2026-04-13T12:30:00+00:00`
  - Reason: 30m bucket 2026-04-13 12:00:00+00:00–2026-04-13 12:30:00+00:00 still open at decision 2026-04-13 12:00:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_011454 @ 2026-05-10T16:20:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-05-10T16:20:00+00:00` → `2026-05-10T16:25:00+00:00`
- Prev/next 5m: `2026-05-10T16:15:00+00:00` / `2026-05-10T16:25:00+00:00`
- Used 15m: `2026-05-10T16:00:00+00:00`–`2026-05-10T16:15:00+00:00` (available `2026-05-10T16:15:00+00:00`)
- Forming 15m (excluded if open): `2026-05-10T16:15:00+00:00`–`2026-05-10T16:30:00+00:00`
  - Reason: 15m bucket 2026-05-10 16:15:00+00:00–2026-05-10 16:30:00+00:00 still open at decision 2026-05-10 16:25:00+00:00; close_time > decision_time
- Used 30m: `2026-05-10T15:30:00+00:00`–`2026-05-10T16:00:00+00:00` (available `2026-05-10T16:00:00+00:00`)
- Forming 30m (excluded if open): `2026-05-10T16:00:00+00:00`–`2026-05-10T16:30:00+00:00`
  - Reason: 30m bucket 2026-05-10 16:00:00+00:00–2026-05-10 16:30:00+00:00 still open at decision 2026-05-10 16:25:00+00:00; close_time > decision_time
- Ages: 15m=10.0 min, 30m=25.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_012158 @ 2026-05-22T11:45:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-05-22T11:45:00+00:00` → `2026-05-22T11:50:00+00:00`
- Prev/next 5m: `2026-05-22T11:40:00+00:00` / `2026-05-22T11:50:00+00:00`
- Used 15m: `2026-05-22T11:30:00+00:00`–`2026-05-22T11:45:00+00:00` (available `2026-05-22T11:45:00+00:00`)
- Forming 15m (excluded if open): `2026-05-22T11:45:00+00:00`–`2026-05-22T12:00:00+00:00`
  - Reason: 15m bucket 2026-05-22 11:45:00+00:00–2026-05-22 12:00:00+00:00 still open at decision 2026-05-22 11:50:00+00:00; close_time > decision_time
- Used 30m: `2026-05-22T11:00:00+00:00`–`2026-05-22T11:30:00+00:00` (available `2026-05-22T11:30:00+00:00`)
- Forming 30m (excluded if open): `2026-05-22T11:30:00+00:00`–`2026-05-22T12:00:00+00:00`
  - Reason: 30m bucket 2026-05-22 11:30:00+00:00–2026-05-22 12:00:00+00:00 still open at decision 2026-05-22 11:50:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=20.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_012376 @ 2026-05-26T05:10:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-05-26T05:10:00+00:00` → `2026-05-26T05:15:00+00:00`
- Prev/next 5m: `2026-05-26T05:05:00+00:00` / `2026-05-26T05:15:00+00:00`
- Used 15m: `2026-05-26T05:00:00+00:00`–`2026-05-26T05:15:00+00:00` (available `2026-05-26T05:15:00+00:00`)
- Forming 15m (excluded if open): `2026-05-26T05:15:00+00:00`–`2026-05-26T05:30:00+00:00`
  - Reason: 15m bucket 2026-05-26 05:15:00+00:00–2026-05-26 05:30:00+00:00 still open at decision 2026-05-26 05:15:00+00:00; close_time > decision_time
- Used 30m: `2026-05-26T04:30:00+00:00`–`2026-05-26T05:00:00+00:00` (available `2026-05-26T05:00:00+00:00`)
- Forming 30m (excluded if open): `2026-05-26T05:00:00+00:00`–`2026-05-26T05:30:00+00:00`
  - Reason: 30m bucket 2026-05-26 05:00:00+00:00–2026-05-26 05:30:00+00:00 still open at decision 2026-05-26 05:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_012617 @ 2026-05-28T10:00:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-05-28T10:00:00+00:00` → `2026-05-28T10:05:00+00:00`
- Prev/next 5m: `2026-05-28T09:55:00+00:00` / `2026-05-28T10:05:00+00:00`
- Used 15m: `2026-05-28T09:45:00+00:00`–`2026-05-28T10:00:00+00:00` (available `2026-05-28T10:00:00+00:00`)
- Forming 15m (excluded if open): `2026-05-28T10:00:00+00:00`–`2026-05-28T10:15:00+00:00`
  - Reason: 15m bucket 2026-05-28 10:00:00+00:00–2026-05-28 10:15:00+00:00 still open at decision 2026-05-28 10:05:00+00:00; close_time > decision_time
- Used 30m: `2026-05-28T09:30:00+00:00`–`2026-05-28T10:00:00+00:00` (available `2026-05-28T10:00:00+00:00`)
- Forming 30m (excluded if open): `2026-05-28T10:00:00+00:00`–`2026-05-28T10:30:00+00:00`
  - Reason: 30m bucket 2026-05-28 10:00:00+00:00–2026-05-28 10:30:00+00:00 still open at decision 2026-05-28 10:05:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_012849 @ 2026-05-30T03:00:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-05-30T03:00:00+00:00` → `2026-05-30T03:05:00+00:00`
- Prev/next 5m: `2026-05-30T02:55:00+00:00` / `2026-05-30T03:05:00+00:00`
- Used 15m: `2026-05-30T02:45:00+00:00`–`2026-05-30T03:00:00+00:00` (available `2026-05-30T03:00:00+00:00`)
- Forming 15m (excluded if open): `2026-05-30T03:00:00+00:00`–`2026-05-30T03:15:00+00:00`
  - Reason: 15m bucket 2026-05-30 03:00:00+00:00–2026-05-30 03:15:00+00:00 still open at decision 2026-05-30 03:05:00+00:00; close_time > decision_time
- Used 30m: `2026-05-30T02:30:00+00:00`–`2026-05-30T03:00:00+00:00` (available `2026-05-30T03:00:00+00:00`)
- Forming 30m (excluded if open): `2026-05-30T03:00:00+00:00`–`2026-05-30T03:30:00+00:00`
  - Reason: 30m bucket 2026-05-30 03:00:00+00:00–2026-05-30 03:30:00+00:00 still open at decision 2026-05-30 03:05:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_013117 @ 2026-06-04T06:15:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-04T06:15:00+00:00` → `2026-06-04T06:20:00+00:00`
- Prev/next 5m: `2026-06-04T06:10:00+00:00` / `2026-06-04T06:20:00+00:00`
- Used 15m: `2026-06-04T06:00:00+00:00`–`2026-06-04T06:15:00+00:00` (available `2026-06-04T06:15:00+00:00`)
- Forming 15m (excluded if open): `2026-06-04T06:15:00+00:00`–`2026-06-04T06:30:00+00:00`
  - Reason: 15m bucket 2026-06-04 06:15:00+00:00–2026-06-04 06:30:00+00:00 still open at decision 2026-06-04 06:20:00+00:00; close_time > decision_time
- Used 30m: `2026-06-04T05:30:00+00:00`–`2026-06-04T06:00:00+00:00` (available `2026-06-04T06:00:00+00:00`)
- Forming 30m (excluded if open): `2026-06-04T06:00:00+00:00`–`2026-06-04T06:30:00+00:00`
  - Reason: 30m bucket 2026-06-04 06:00:00+00:00–2026-06-04 06:30:00+00:00 still open at decision 2026-06-04 06:20:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=20.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_013545 @ 2026-06-08T10:00:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-08T10:00:00+00:00` → `2026-06-08T10:05:00+00:00`
- Prev/next 5m: `2026-06-08T09:55:00+00:00` / `2026-06-08T10:05:00+00:00`
- Used 15m: `2026-06-08T09:45:00+00:00`–`2026-06-08T10:00:00+00:00` (available `2026-06-08T10:00:00+00:00`)
- Forming 15m (excluded if open): `2026-06-08T10:00:00+00:00`–`2026-06-08T10:15:00+00:00`
  - Reason: 15m bucket 2026-06-08 10:00:00+00:00–2026-06-08 10:15:00+00:00 still open at decision 2026-06-08 10:05:00+00:00; close_time > decision_time
- Used 30m: `2026-06-08T09:30:00+00:00`–`2026-06-08T10:00:00+00:00` (available `2026-06-08T10:00:00+00:00`)
- Forming 30m (excluded if open): `2026-06-08T10:00:00+00:00`–`2026-06-08T10:30:00+00:00`
  - Reason: 30m bucket 2026-06-08 10:00:00+00:00–2026-06-08 10:30:00+00:00 still open at decision 2026-06-08 10:05:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=5.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_014127 @ 2026-06-13T15:10:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-13T15:10:00+00:00` → `2026-06-13T15:15:00+00:00`
- Prev/next 5m: `2026-06-13T15:05:00+00:00` / `2026-06-13T15:15:00+00:00`
- Used 15m: `2026-06-13T15:00:00+00:00`–`2026-06-13T15:15:00+00:00` (available `2026-06-13T15:15:00+00:00`)
- Forming 15m (excluded if open): `2026-06-13T15:15:00+00:00`–`2026-06-13T15:30:00+00:00`
  - Reason: 15m bucket 2026-06-13 15:15:00+00:00–2026-06-13 15:30:00+00:00 still open at decision 2026-06-13 15:15:00+00:00; close_time > decision_time
- Used 30m: `2026-06-13T14:30:00+00:00`–`2026-06-13T15:00:00+00:00` (available `2026-06-13T15:00:00+00:00`)
- Forming 30m (excluded if open): `2026-06-13T15:00:00+00:00`–`2026-06-13T15:30:00+00:00`
  - Reason: 30m bucket 2026-06-13 15:00:00+00:00–2026-06-13 15:30:00+00:00 still open at decision 2026-06-13 15:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_014504 @ 2026-06-16T08:15:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-16T08:15:00+00:00` → `2026-06-16T08:20:00+00:00`
- Prev/next 5m: `2026-06-16T08:10:00+00:00` / `2026-06-16T08:20:00+00:00`
- Used 15m: `2026-06-16T08:00:00+00:00`–`2026-06-16T08:15:00+00:00` (available `2026-06-16T08:15:00+00:00`)
- Forming 15m (excluded if open): `2026-06-16T08:15:00+00:00`–`2026-06-16T08:30:00+00:00`
  - Reason: 15m bucket 2026-06-16 08:15:00+00:00–2026-06-16 08:30:00+00:00 still open at decision 2026-06-16 08:20:00+00:00; close_time > decision_time
- Used 30m: `2026-06-16T07:30:00+00:00`–`2026-06-16T08:00:00+00:00` (available `2026-06-16T08:00:00+00:00`)
- Forming 30m (excluded if open): `2026-06-16T08:00:00+00:00`–`2026-06-16T08:30:00+00:00`
  - Reason: 30m bucket 2026-06-16 08:00:00+00:00–2026-06-16 08:30:00+00:00 still open at decision 2026-06-16 08:20:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=20.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_014954 @ 2026-06-21T09:45:00+00:00

- Sample: `out_of_sample`
- Sweep open/close: `2026-06-21T09:45:00+00:00` → `2026-06-21T09:50:00+00:00`
- Prev/next 5m: `2026-06-21T09:40:00+00:00` / `2026-06-21T09:50:00+00:00`
- Used 15m: `2026-06-21T09:30:00+00:00`–`2026-06-21T09:45:00+00:00` (available `2026-06-21T09:45:00+00:00`)
- Forming 15m (excluded if open): `2026-06-21T09:45:00+00:00`–`2026-06-21T10:00:00+00:00`
  - Reason: 15m bucket 2026-06-21 09:45:00+00:00–2026-06-21 10:00:00+00:00 still open at decision 2026-06-21 09:50:00+00:00; close_time > decision_time
- Used 30m: `2026-06-21T09:00:00+00:00`–`2026-06-21T09:30:00+00:00` (available `2026-06-21T09:30:00+00:00`)
- Forming 30m (excluded if open): `2026-06-21T09:30:00+00:00`–`2026-06-21T10:00:00+00:00`
  - Reason: 30m bucket 2026-06-21 09:30:00+00:00–2026-06-21 10:00:00+00:00 still open at decision 2026-06-21 09:50:00+00:00; close_time > decision_time
- Ages: 15m=5.0 min, 30m=20.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000084 @ 2025-12-29T03:40:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T03:40:00+00:00` → `2025-12-29T03:45:00+00:00`
- Prev/next 5m: `2025-12-29T03:35:00+00:00` / `2025-12-29T03:45:00+00:00`
- Used 15m: `2025-12-29T03:30:00+00:00`–`2025-12-29T03:45:00+00:00` (available `2025-12-29T03:45:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T03:45:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 15m bucket 2025-12-29 03:45:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T03:00:00+00:00`–`2025-12-29T03:30:00+00:00` (available `2025-12-29T03:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T03:30:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 30m bucket 2025-12-29 03:30:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000086 @ 2025-12-29T03:40:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T03:40:00+00:00` → `2025-12-29T03:45:00+00:00`
- Prev/next 5m: `2025-12-29T03:35:00+00:00` / `2025-12-29T03:45:00+00:00`
- Used 15m: `2025-12-29T03:30:00+00:00`–`2025-12-29T03:45:00+00:00` (available `2025-12-29T03:45:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T03:45:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 15m bucket 2025-12-29 03:45:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T03:00:00+00:00`–`2025-12-29T03:30:00+00:00` (available `2025-12-29T03:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T03:30:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 30m bucket 2025-12-29 03:30:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000087 @ 2025-12-29T03:40:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T03:40:00+00:00` → `2025-12-29T03:45:00+00:00`
- Prev/next 5m: `2025-12-29T03:35:00+00:00` / `2025-12-29T03:45:00+00:00`
- Used 15m: `2025-12-29T03:30:00+00:00`–`2025-12-29T03:45:00+00:00` (available `2025-12-29T03:45:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T03:45:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 15m bucket 2025-12-29 03:45:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T03:00:00+00:00`–`2025-12-29T03:30:00+00:00` (available `2025-12-29T03:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T03:30:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 30m bucket 2025-12-29 03:30:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000088 @ 2025-12-29T03:40:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T03:40:00+00:00` → `2025-12-29T03:45:00+00:00`
- Prev/next 5m: `2025-12-29T03:35:00+00:00` / `2025-12-29T03:45:00+00:00`
- Used 15m: `2025-12-29T03:30:00+00:00`–`2025-12-29T03:45:00+00:00` (available `2025-12-29T03:45:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T03:45:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 15m bucket 2025-12-29 03:45:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T03:00:00+00:00`–`2025-12-29T03:30:00+00:00` (available `2025-12-29T03:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T03:30:00+00:00`–`2025-12-29T04:00:00+00:00`
  - Reason: 30m bucket 2025-12-29 03:30:00+00:00–2025-12-29 04:00:00+00:00 still open at decision 2025-12-29 03:45:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000109 @ 2025-12-29T05:55:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T05:55:00+00:00` → `2025-12-29T06:00:00+00:00`
- Prev/next 5m: `2025-12-29T05:50:00+00:00` / `2025-12-29T06:00:00+00:00`
- Used 15m: `2025-12-29T05:45:00+00:00`–`2025-12-29T06:00:00+00:00` (available `2025-12-29T06:00:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T06:00:00+00:00`–`2025-12-29T06:15:00+00:00`
  - Reason: 15m bucket 2025-12-29 06:00:00+00:00–2025-12-29 06:15:00+00:00 still open at decision 2025-12-29 06:00:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T05:30:00+00:00`–`2025-12-29T06:00:00+00:00` (available `2025-12-29T06:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T06:00:00+00:00`–`2025-12-29T06:30:00+00:00`
  - Reason: 30m bucket 2025-12-29 06:00:00+00:00–2025-12-29 06:30:00+00:00 still open at decision 2025-12-29 06:00:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000113 @ 2025-12-29T05:55:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-29T05:55:00+00:00` → `2025-12-29T06:00:00+00:00`
- Prev/next 5m: `2025-12-29T05:50:00+00:00` / `2025-12-29T06:00:00+00:00`
- Used 15m: `2025-12-29T05:45:00+00:00`–`2025-12-29T06:00:00+00:00` (available `2025-12-29T06:00:00+00:00`)
- Forming 15m (excluded if open): `2025-12-29T06:00:00+00:00`–`2025-12-29T06:15:00+00:00`
  - Reason: 15m bucket 2025-12-29 06:00:00+00:00–2025-12-29 06:15:00+00:00 still open at decision 2025-12-29 06:00:00+00:00; close_time > decision_time
- Used 30m: `2025-12-29T05:30:00+00:00`–`2025-12-29T06:00:00+00:00` (available `2025-12-29T06:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-29T06:00:00+00:00`–`2025-12-29T06:30:00+00:00`
  - Reason: 30m bucket 2025-12-29 06:00:00+00:00–2025-12-29 06:30:00+00:00 still open at decision 2025-12-29 06:00:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000163 @ 2025-12-30T08:10:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-30T08:10:00+00:00` → `2025-12-30T08:15:00+00:00`
- Prev/next 5m: `2025-12-30T08:05:00+00:00` / `2025-12-30T08:15:00+00:00`
- Used 15m: `2025-12-30T08:00:00+00:00`–`2025-12-30T08:15:00+00:00` (available `2025-12-30T08:15:00+00:00`)
- Forming 15m (excluded if open): `2025-12-30T08:15:00+00:00`–`2025-12-30T08:30:00+00:00`
  - Reason: 15m bucket 2025-12-30 08:15:00+00:00–2025-12-30 08:30:00+00:00 still open at decision 2025-12-30 08:15:00+00:00; close_time > decision_time
- Used 30m: `2025-12-30T07:30:00+00:00`–`2025-12-30T08:00:00+00:00` (available `2025-12-30T08:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-30T08:00:00+00:00`–`2025-12-30T08:30:00+00:00`
  - Reason: 30m bucket 2025-12-30 08:00:00+00:00–2025-12-30 08:30:00+00:00 still open at decision 2025-12-30 08:15:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=15.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000167 @ 2025-12-30T09:25:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-30T09:25:00+00:00` → `2025-12-30T09:30:00+00:00`
- Prev/next 5m: `2025-12-30T09:20:00+00:00` / `2025-12-30T09:30:00+00:00`
- Used 15m: `2025-12-30T09:15:00+00:00`–`2025-12-30T09:30:00+00:00` (available `2025-12-30T09:30:00+00:00`)
- Forming 15m (excluded if open): `2025-12-30T09:30:00+00:00`–`2025-12-30T09:45:00+00:00`
  - Reason: 15m bucket 2025-12-30 09:30:00+00:00–2025-12-30 09:45:00+00:00 still open at decision 2025-12-30 09:30:00+00:00; close_time > decision_time
- Used 30m: `2025-12-30T09:00:00+00:00`–`2025-12-30T09:30:00+00:00` (available `2025-12-30T09:30:00+00:00`)
- Forming 30m (excluded if open): `2025-12-30T09:30:00+00:00`–`2025-12-30T10:00:00+00:00`
  - Reason: 30m bucket 2025-12-30 09:30:00+00:00–2025-12-30 10:00:00+00:00 still open at decision 2025-12-30 09:30:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000218 @ 2025-12-30T15:55:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-30T15:55:00+00:00` → `2025-12-30T16:00:00+00:00`
- Prev/next 5m: `2025-12-30T15:50:00+00:00` / `2025-12-30T16:00:00+00:00`
- Used 15m: `2025-12-30T15:45:00+00:00`–`2025-12-30T16:00:00+00:00` (available `2025-12-30T16:00:00+00:00`)
- Forming 15m (excluded if open): `2025-12-30T16:00:00+00:00`–`2025-12-30T16:15:00+00:00`
  - Reason: 15m bucket 2025-12-30 16:00:00+00:00–2025-12-30 16:15:00+00:00 still open at decision 2025-12-30 16:00:00+00:00; close_time > decision_time
- Used 30m: `2025-12-30T15:30:00+00:00`–`2025-12-30T16:00:00+00:00` (available `2025-12-30T16:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-30T16:00:00+00:00`–`2025-12-30T16:30:00+00:00`
  - Reason: 30m bucket 2025-12-30 16:00:00+00:00–2025-12-30 16:30:00+00:00 still open at decision 2025-12-30 16:00:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

## OPT_000219 @ 2025-12-30T15:55:00+00:00

- Sample: `in_sample`
- Sweep open/close: `2025-12-30T15:55:00+00:00` → `2025-12-30T16:00:00+00:00`
- Prev/next 5m: `2025-12-30T15:50:00+00:00` / `2025-12-30T16:00:00+00:00`
- Used 15m: `2025-12-30T15:45:00+00:00`–`2025-12-30T16:00:00+00:00` (available `2025-12-30T16:00:00+00:00`)
- Forming 15m (excluded if open): `2025-12-30T16:00:00+00:00`–`2025-12-30T16:15:00+00:00`
  - Reason: 15m bucket 2025-12-30 16:00:00+00:00–2025-12-30 16:15:00+00:00 still open at decision 2025-12-30 16:00:00+00:00; close_time > decision_time
- Used 30m: `2025-12-30T15:30:00+00:00`–`2025-12-30T16:00:00+00:00` (available `2025-12-30T16:00:00+00:00`)
- Forming 30m (excluded if open): `2025-12-30T16:00:00+00:00`–`2025-12-30T16:30:00+00:00`
  - Reason: 30m bucket 2025-12-30 16:00:00+00:00–2025-12-30 16:30:00+00:00 still open at decision 2025-12-30 16:00:00+00:00; close_time > decision_time
- Ages: 15m=0.0 min, 30m=0.0 min
- Join ok / 5m exact: True / True
- Note: decision_time = signal_timestamp + 5m (sweep close). HTF buckets require close_time <= decision_time; incomplete buckets are excluded.

