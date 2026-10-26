# OPEN_STRATEGY_DECISIONS

Items marked **UNRESOLVED** or **EXPERIMENTAL**. No agent resolution — user must decide before `FINAL_APPROVED` freeze.

| id | Topic | Why open | Related manual refs |
|----|-------|----------|---------------------|
| O-01 | **Dense lower TP cluster** — exact selection rank (depth, width, gap, distance) | User rule qualitative; 4A used nearest top | C003 lower zone choice noted open in Signal_definierung |
| O-02 | **Dense upper TP cluster** for LONG | Mirror of O-01 | Two-lower-pool §522 |
| O-03 | **Long entry trigger candle** | Not fully specified | L1, MS-02 |
| O-04 | **min_pool_age_bars=5** as hard gate | C003 fails; user forbids retro label change | C003, Signal_definierung §D |
| O-05 | **min_pool_gap_pct=0.5** enforcement at entry | Documented vs S5/N3 contrasts; not single threshold proven | S5, N3 |
| O-06 | **Upper pool “relevance”** beyond density (persistence, interaction) | L1 vs N1 narrative | Signal_definierung L51–57 |
| O-07 | **EMA role** (context vs hard filter) | N2 vs S5 | Multiple cases |
| O-08 | **WATCH_REVERSAL lift** — when shorts allowed again | User: not specified | MS-03 |
| O-09 | **3B detector parity** with user two-lower sequence | Verdict PARTIAL; times differ from narrative | Phase 3B report |
| O-10 | **Re-entry** after SL/TP on same opportunity | Open; S9/S10 precedent | C013 after C009 in 4A is timing only |
| O-11 | **Intrabar SL vs TP** on same 5m bar | Policy stated (no optimistic TP); not user-signed | Phase 4B |
| O-12 | **Slippage & funding** | Fees only 0.11% confirmed | Signal_definierung L7 |
| O-13 | **PD10** for TP landscape | Mentioned, not wired | DATA-02 |
| O-14 | **10-bar upper age** draft vs 5-bar research param | Superseded in prose but both in Signal_definierung | §418–428 vs §545–546 |
| O-15 | **Quality / selectivity filters** (S4 “only clearest”) | Preference, not numeric rule | S4 |

Use `STRATEGY_RULE_UNRESOLVED` in any integration when a mandatory rule has no approved spec.
