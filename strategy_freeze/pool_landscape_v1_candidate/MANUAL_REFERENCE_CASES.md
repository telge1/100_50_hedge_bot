# MANUAL_REFERENCE_CASES — unchanged labels

**Authority:** `Signal_definierung.md` and Phase 1E capture (`reports/DOGEUSDT_PHASE1E_DEPTH2_REFERENCE_CAPTURE_V1.md`). Labels are **not** modified based on backtest outcomes.

## Pilot scanner IDs (11–12 Sep 2026)

| ID | UTC signal (5m close) | User label | Notes |
|----|------------------------|------------|-------|
| C001 | 2026-09-11T12:00:00Z | **NO_SHORT** (`MANUALLY_NO_SHORT`) | Upper pool too narrow/insignificant |
| C002 | 2026-09-11T15:35:00Z | **NO_SHORT** | Upper OK visually; lower TP landscape too thin |
| C003 | 2026-09-11T17:00:00Z | **SHORT** (`MANUALLY_CONFIRMED_SHORT`) | “Perfect”; D2 age 1 bar — **do not** downgrade via 5-bar gate |
| C004 | 2026-09-11T19:45:00Z | **Too early / not preferred** | User prefers later contacts on established structure |
| C008 | 2026-09-11T21:10:00Z | **SHORT** (`MANUALLY_CONFIRMED_SHORT`) | D2 age 14 bars |
| C010 | 2026-09-11T23:40:00Z | **SHORT accepted** (SL possible) | Entry 23:45 hypothetical |
| C011 | 2026-09-11T23:55:00Z | **Same opportunity as C010** | No add while C010 open (`DUPLICATE_WHILE_POSITION_OPEN`) |
| C013 | 2026-09-12T00:35:00Z | **Same opportunity as C010** | `MANUAL_SAME_OPPORTUNITY_DUPLICATE` |

Other exported scanner IDs (C005–C007, C009, C012, C014–C020): **no** manual SHORT/NO_SHORT label in `Signal_definierung.md` §C — treat as **UNREVIEWED** for manual reference tier.

## S-series (manual short references)

| Case | Status in fallbuch | Depth-2 notes |
|------|-------------------|---------------|
| S1 | Manual short ref 2026-09-15 ~01:45 UTC | Phase 1C: hint at depth-1, not depth-2 at hint price |
| S2 | Documented; timestamps/TP edge open | Signal_definierung.md |
| S3 | User rejection-at-upper narrative | Micro-upper / depth1 vs 2 hypothesis open |
| S4 | “Perfect short” preference / selectivity | Signal_definierung.md |
| S5 | Accepted short vs EMA ordering | Contrast anchor |
| S6–S8 | See Signal_definierung.md | Not all re-listed here — full text in source |
| S9 / S10 | Two shorts same upper zone; S10 levels open | Signal_definierung.md L292–317 |

## N-series (manual no-trade)

| Case | Decision |
|------|----------|
| N1 | **No long** — early lowers in downtrend (contrast L1) |
| N2 | **No short** — bullish EMA structure at entry |
| N3 | **No short** — stacked upper pools, small gaps (vs S5) |
| N4 | **No short** — weak lower target landscape |
| N5 | See Signal_definierung.md |

## L-series

| Case | Notes |
|------|-------|
| L1 | Long 2026-09-16 ~02:00 UTC — range/asymmetric landscape rationale; trigger rule **OPEN** |

## Two-lower-pool reversal (11–12 Sep 2026)

| Ref | Status |
|-----|--------|
| Two-Lower-Pool Reversal V1 | `MANUAL_REVERSAL_REFERENCE` — not algorithmically validated |

## Phase 2G automatic grouping (research only)

C010 primary; C011/C013 duplicates — see `results/phase2g_upper_opportunity_dedup_v1/C010_C011_C013_forensics.md`. Does **not** override manual labels; supports opportunity identity hypothesis.
