# P4A — Baseline (P4-Browser, vor P4A-Fix)

**Szenario:** SOLUSDT, 1m → 5m

| Metrik | Wert |
|--------|------|
| Phase A Kerzen | 304 |
| Phase A HTTP | 3.862 ms |
| Erste 5m-Freigabe | ~4,53 s nach 5m-Request-Start |
| Phase B HTTP | 11.456 ms |
| Phase B `request_limit` | 24.192 |
| Phase B Response Kerzen | **66.804** |
| Cache | hit |
| 5m `ema-post-catch-up` | skipped (`history_phase_b_bundle_ema`) ✓ |
| Parallel 1m `ema-after-candle-merge` | 50.000 / 7.615 ms ✗ (stale) |
| Vor Wechsel 1m `setData` | >334.000 (1m-History) |

## Ziel nach P4A-Fix

| Metrik | Erwartung |
|--------|-----------|
| Phase B Response Kerzen | **24.192** (bei 12 Wochen) |
| 1m EMA während 5m-Load | kein neuer 1m-Span-POST |
| Phase A / Fast-First | unverändert (~300 Kerzen, frühe Freigabe) |
| EMA9/20/59/100/200 | Server-Bundle, keine neuen Formeln |
| GO-TO / Viewport / Multi | unveränderte Semantik |

Messung: `BROWSER_PENDING`.