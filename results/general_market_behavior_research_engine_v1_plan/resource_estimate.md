# Resource Estimate (6-core server, approximate)

## Full-OB COMPLETE hour (BTC probe reference)
- ~6.1k deltas, ~39k bid + ~27k ask levels at end → heavy in RAM if many snapshots retained
- Replay verify ~1–2s wall for hash+apply; **per-second snapshot materialization** dominates

## Recommendations
- Default store **band aggregates + top-K walls**, not full level arrays every second in CH
- Keep event replay on FS for deep dives
- Batch: 1 hour Full-OB state build target **< 5–15 min** CPU if streaming apply + emit 1s aggregates (to be measured in P1)
- RAM budget per worker: **2–4 GB** for Full-OB BTC book + buffers; avoid parallel multi-symbol Full-OB on same host as live collectors without cap
- Disk derived 1s state: order **~1–5 KB/row compressed** if aggregates-only → ~0.1–0.5 GB/symbol/day; full L arrays would be 10–100× — reject as default
- OB200 track: much cheaper; multi-day OK

## Incremental processing
Watermark by (symbol, hour, segment_id, build_id); skip COMPLETE hours already built; never rewrite RAW.

## Coexistence
Do not starve live collectors / materializer; research jobs nice-ionice / CPUQuota in later ops (not changing systemd in this plan phase).
