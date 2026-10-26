# IMPLEMENTATION_DEVIATIONS — User strategy vs current code

**Legend:** MATCH | DEVIATION | MISSING | UNRESOLVED

| # | User rule | Source | Current implementation | Default / location | Status | Clarification needed |
|---|-----------|--------|------------------------|-------------------|--------|----------------------|
| 1 | Relevant upper touch pool | USER_CONFIRMED | `pickUpperPool`: nearest proximity among depth-2 uppers | `timeline_scanner.js` | DEVIATION | “Relevant” ≠ nearest distance |
| 2 | Bearish rejection on signal candle | USER_CONFIRMED (S3) | `rejection_v1`: close < open only | Phase 2A | PARTIAL MATCH | rejection_v2 / touch quality not user-frozen |
| 3 | Entry next 5m open | USER_CONFIRMED | Implemented | Phase 2A | MATCH | — |
| 4 | Large upper gap in entry landscape | USER_CONFIRMED | **Profile only** (`large_gap_pct` 0.5); **not** scan gate | Phase 2A profiles | MISSING | Require gate or post-filter? |
| 5 | min_pool_age_bars | RESEARCH (not hard) | Off in 2A; **on** in 4A scenario `age_on` | 4A backtest | DEVIATION | Which scenarios represent intent? |
| 6 | min_pool_gap_pct 0.5 | RESEARCH | Computed in profiles, not enforced on emit | Phase 2A | MISSING | Same as row 4 |
| 7 | Dense lower TP cluster | USER_CONFIRMED | `select_short_tp`: sort lowers by **desc top**, first ≥0.5% | `backtest_pilot.py` | DEVIATION | Phase 4B forensics: all shorts NOT_VALIDATED vs user rule |
| 8 | SL 0.2% above upper top | USER_CONFIRMED | `pool_upper * 1.002` | 4A | MATCH | — |
| 9 | LONG mirror entry | UNRESOLVED | No Phase 2A long scanner | — | MISSING | Only 3B experimental longs |
| 10 | Dense upper TP | USER_CONFIRMED | Rank-1 nearest upper from 3B CSV | 4A | DEVIATION | Not dense-cluster logic |
| 11 | LONG SL below lower | USER_CONFIRMED | `pool_lower * 0.998` on 3B second lower | 4A | MATCH for experimental path | Entry pool definition open |
| 12 | WATCH blocks new shorts | USER (manual MS) | 3B `blocked_short_windows` + state | 4A scenario B | UNRESOLVED | Not validated vs C003/C008 |
| 13 | Two-lower LONG confirmation | USER_MANUAL_REFERENCE | Phase 3B detector PARTIAL | `two_pool_detector.py` | UNRESOLVED | Timestamps vs user chart |
| 14 | Same opportunity dedup | USER_CONFIRMED | Phase 2G research dedup; **not** in 4A backtester | 2G JS | MISSING in execution | Merge policy for trades? |
| 15 | Max 1 position | USER_CONFIRMED | Implemented | 4A | MATCH | — |
| 16 | Raw signals preserved | USER_CONFIRMED | `raw_signals.csv` | 4A | MATCH | — |
| 17 | Re-entry after exit | UNRESOLVED | 4A allows C013 after C009 close; no opp gate | 4A | UNRESOLVED | User rule O-10 |
| 18 | Intrabar exit | Policy in 4A code | SL-first conservative path documented; ambiguous flag | 4A | UNRESOLVED | User sign-off |
| 19 | Fees 0.11% round trip | USER_CONFIRMED | Applied on closed trades | 4A | MATCH | Slippage missing |
| 20 | Signal → executable trade | USER_CONFIRMED | TP resolve + eligibility + position slot | 4A | PARTIAL | Missing user quality filters |
| 21 | Proximity 0.05% | — | Phase 2A default | IMPLEMENTATION_DEFAULT | — | User did not freeze value |
| 22 | Cooldown 6 candles / pool | — | Phase 2A | IMPLEMENTATION_DEFAULT | — | User did not freeze |
| 23 | Max 20 chronological exports | — | Phase 2A cap | IMPLEMENTATION_DEFAULT | — | Hides S5 etc. |
| 24 | Production PD1 adapter | DATA-03 | `pd1_exact_density_trp_adapter.js` | dashboard research path | MATCH (landscape) | Scanner path is Phase 2A tool, not prod bot |

## Original LMV2

Path examples: `backtester/short/dashboard/research_charts/historical_lmv2/`, `frozen_strategies/e1r_cluster3_ema200_v1/`. **Not** listed as deviations above — separate frozen strategy line.

---

## Phase 4D amendment (2026-10-22)

Strict backtester V2 (`tools/phase4d_pool_landscape_backtester_v2/`) addresses price-formula deviations. See `STRATEGY_SPEC_CANDIDATE_4D.md` and `reports/DOGEUSDT_PHASE4D_STRICT_STRATEGY_BACKTESTER_V2.md`. Entry execution remains **OPEN**.
