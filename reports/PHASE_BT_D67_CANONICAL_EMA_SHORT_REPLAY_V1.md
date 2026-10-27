# BT-D67 — Canonical EMA-Gated Short Replay V1

**VERDICT:** `CANONICAL_EMA_REPLAY_PARTIAL`

Git baseline: BT-D66 `0c602dd9` (runner commit at replay time).

## Pilots

| Pilot | Window | Bars | Pool candidates | Pending (research READY) | Default pause pending |
|-------|--------|------|-----------------|--------------------------|------------------------|
| sep9_evening_2026 | 2026-09-09 16:00–22:00 UTC | 73 | 12 | 8 | 0 |
| sep11_midday_2026 | 2026-09-11 11:30–13:30 UTC | 25 | 1 | 0 | 0 |

Density: D54 frozen snapshots + `ResearchDensityRunV1` canonical collect (Sep-9 gaps); D9 validated cache + canonical fallback (Sep-11 pre-12:00 bars). **98/98** preflight bars `OK`.

## Reference EMA (BT-D65 only — not R5/R6 / P2)

| UTC | Expected | Actual | Match |
|-----|----------|--------|-------|
| 2026-09-09 16:30 | SHORT_OK | UNCERTAIN (`FALLING_EMAS_PRICE_RECLAIM`) | No |
| 2026-09-09 20:55 | SHORT_OK | SHORT_OK | Yes |
| 2026-09-11 12:00 | UNCERTAIN | UNCERTAIN | Yes |
| 2026-09-11 12:05 | SHORT_OK | SHORT_OK | Yes |
| 2026-09-11 12:30 | UNCERTAIN | UNCERTAIN | Yes |

16:30 mismatch is recorded for follow-up (same frozen classifier; OHLC close vs EMA20/59 at this load).

## Causal ordering

Chronological runner evaluates **modeled fill (D45 M1) before close-based EMA cancel** on each bar. See `unresolved_timing_cases.csv` if fill precedes a would-be EMA veto on the same bar.

## Pause policy

- **Shadow track:** `ema_gate_at_candidates.csv` records EMA at each pool candidate regardless of pause.
- **Default:** `SHORT_PAUSED` — zero pending creates (not an EMA performance signal).
- **Research comparator:** `RESEARCH_READY` — pending lifecycle under EMA gate only.

## Artifacts

`results/phase_bt_d67_canonical_ema_replay_v1/` — provenance, coverage, baseline candidates, EMA shadow, lifecycle, comparison, reference audit, pause audit.

No D46 mutation. No live trades.
