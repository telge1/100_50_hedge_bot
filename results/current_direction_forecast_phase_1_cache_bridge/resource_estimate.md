# Resource Estimate (no live measurement)

**Generated (UTC):** `2026-09-05T15:12:26Z`

| Item | Estimate | Notes |
|------|----------|-------|
| Checkpoint interval | 60s default | configurable |
| Checkpoints / symbol | ≤12 | ~10 min window |
| BTC checkpoint size | ~2–8 MiB uncompressed JSON-equivalent levels | depends on depth; hard cap 48 MiB/symbol |
| DOGE checkpoint size | typically smaller | same caps |
| Extra RAM (both symbols) | ~50–150 MiB typical worst planning | within caps |
| Freeze lock hold | milliseconds for ring+book copy | serialize/hash outside ingest hot path where possible |
| Dump size | ring deltas (~MB) + anchor + t0 book | hard max payload 96 MiB |
| Concurrent freezes | 1 | REQUEST_BUSY otherwise |
| Ingest isolation | observer exceptions swallowed; bridge errors must not stop WS | |

No live performance run in Phase 1.
