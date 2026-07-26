# Multi-Blocker Break Handoff Depth Audit

**Decision:** `BREAK_HANDOFF_DEPTH_AUDIT_PASS_WITH_WARNINGS`

## Scope

After a confirmed market-structure break the original TEM bot stays armed (`COBERTURA_ARMED`) and continues causally. Cobertura starts only when price reaches `structure_break_price * (1 - depth_pct)`. Handoff uses the **live** TEM book at that candle (ledger cut), not a frozen legacy B0 snapshot.

- Ready cases: 25
- Unresolved structure breaks: 2 (BCHUSDT, TRXUSDT)
- Parity guards pass: True

## Classification rules

- `IMMEDIATE_HANDOFF_BEST` / `NO_ROBUST_HANDOFF_DEPTH`: BREAK_D0 outcome label
- `DELAYED_HANDOFF_IMPROVES_RECOVERY`: depth recovers when D0 did not
- `DELAYED_HANDOFF_IMPROVES_STATE`: better combined and improved pre-handoff state
- `DELAYED_HANDOFF_ONLY_REDUCES_POST_ACTIVATION_DD`: post-DD better, full path not
- `DELAYED_HANDOFF_WORSENS_SHARED_BE`: larger shared-BE distance and worse combined
- `ORIGINAL_BOT_IMPROVES/WORSENS_STATE_BEFORE_HANDOFF`: wait-phase state delta
- `ACTIVATION_TARGET_NOT_REACHED` / `NO_COBERTURA_BEST` / `UNRESOLVED_STRUCTURE_BREAK`

## BREAK_D0 vs LEGACY_B0_REFERENCE

- `BREAK_D0`: handoff at first causal touch of the break level after signal availability; inventory = pre-signal / break-available TEM book.
- `LEGACY_B0_REFERENCE`: prior multi-blocker baseline (T1 @ 6% start-distance after signal). Different start-state timing; not forced equal.

## Summary by variant

| variant | activation_reached | recovered_120d | combined_pnl_sum | median_combined_pnl | median_full_horizon_drawdown | median_refill_short_qty | median_shared_be_distance | activation_not_reached |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| BREAK_D0 | 25 | 2 | -657.9468165496828 | -23.62378508796227 | 12.912916456275026 | 98.76800000000003 | 0.06106226339560937 | 0 |
| BREAK_D1 | 25 | 3 | -631.824691973295 | -21.909914924860907 | 14.288416611334846 | 98.76800000000003 | 0.06106226339560937 | 0 |
| BREAK_D2 | 25 | 3 | -695.2261851423772 | -24.760093988677255 | 11.678660000000015 | 98.76800000000003 | 0.06398968731280075 | 0 |
| BREAK_D3 | 25 | 3 | -756.1324793237081 | -27.16669535416208 | 12.70304000000002 | 105.25500000000002 | 0.07382645386077484 | 0 |
| BREAK_D4 | 25 | 3 | -733.1554980679077 | -29.117581591834053 | 17.532147171594954 | 105.25500000000002 | 0.0726990090367218 | 0 |
| BREAK_D5 | 25 | 3 | -847.4707007574701 | -29.53622481728684 | 16.932567651155075 | 105.25500000000002 | 0.0839905775528979 | 0 |
| BREAK_D6 | 25 | 2 | -962.0988905934328 | -32.591055450857084 | 19.703396819115117 | 98.76800000000003 | 0.09552239220771587 | 0 |
| BREAK_D8 | 25 | 1 | -1143.732528032373 | -41.66395441629564 | 21.30361199999998 | 98.76800000000003 | 0.11921674646677027 | 0 |
| BREAK_D10 | 25 | 1 | -1290.0104074013386 | -51.13550724227063 | 26.744678438595663 | 98.76800000000003 | 0.14336016115249994 | 0 |
| BREAK_D12 | 25 | 2 | -1390.9457049034247 | -58.86827578772841 | 33.442392799964686 | 98.76800000000003 | 0.1693456193605113 | 0 |
| BREAK_D15 | 25 | 0 | -1777.0920053043587 | -68.22567988190193 | 46.93828512255316 | 120.14200000000008 | 0.20015016805674038 | 0 |
| BREAK_D20 | 25 | 0 | -2195.8838095381075 | -84.66515654710865 | 67.56083302854155 | 142.40900000000008 | 0.2738327545318263 | 0 |
| NO_COBERTURA_AFTER_BREAK | 0 | 0 | -3444.3574686669363 | -141.46470904320833 | 178.25425983494438 | 0.0 | 0.0 | 0 |
| LEGACY_B0_REFERENCE | 25 | 2 | -855.1652490533094 | -31.95453561097405 | 0.0 | 0.0 | 0.0 | 0 |

## Pairwise vs BREAK_D0

| variant | improved | worsened | add_rec | lost_rec | pnl_delta | not_reached |
| --- | --- | --- | --- | --- | --- | --- |
| BREAK_D1 | 3 | 10 | 3 | 2 | 26.122124576387854 | 0 |
| BREAK_D2 | 3 | 17 | 3 | 2 | -37.2793685926943 | 0 |
| BREAK_D3 | 2 | 23 | 2 | 1 | -98.1856627740253 | 0 |
| BREAK_D4 | 3 | 22 | 2 | 1 | -75.20868151822486 | 0 |
| BREAK_D5 | 3 | 22 | 3 | 2 | -189.5238842077873 | 0 |
| BREAK_D6 | 2 | 23 | 2 | 2 | -304.15207404374985 | 0 |
| BREAK_D8 | 1 | 24 | 1 | 2 | -485.7857114826901 | 0 |
| BREAK_D10 | 1 | 24 | 1 | 2 | -632.0635908516556 | 0 |
| BREAK_D12 | 2 | 23 | 2 | 2 | -732.998888353742 | 0 |
| BREAK_D15 | 0 | 25 | 0 | 2 | -1119.145188754676 | 0 |
| BREAK_D20 | 0 | 25 | 0 | 2 | -1537.936992988425 | 0 |

## Research answers (audit)

1. Structure-break timestamps/prices reproduced: **YES**
2. Pre-break TEM book reproduced vs fill-replay: **YES**
3. `BREAK_D0` vs `LEGACY_B0_REFERENCE`: D0 hands off at first causal touch of the structure-break level after signal availability (APT @ 00:00 / 1.7223). Legacy multi-blocker baseline waits T1@6% start-distance (APT @ 00:05 / 1.6447) and recovers APT+TIA; D0 recovers a different set (DOGE+ETC) and does **not** recover APT at immediate break handoff.
4. Between break and later depths TEM often continues with long adds/reduces (fills>0 common from ~D5 onward; D0 almost always same-bar).
5. Wait-phase state: more often **worsened** than improved before delayed handoff (long adds raise refill need; averages drift).
6. Refill qty: unchanged on same-bar activations; on live paths refill can shrink or grow with TEM qty changes (see state-change CSV).
7–9. Deeper refill prices pull short_avg down → wider long/short avg spread and larger rebound-to-long-avg proxy (shared-BE distance).
10. Additional recoveries vs D0 appear at D1–D5 for some coins (e.g. D1: AVAX/RENDER/SOL; D5: APT/DOT/TIA) but are **not stable**.
11. D0 winners (DOGE/ETC) are often **lost** at deeper depths.
12. Aggregate combined PnL: only D1 slightly beats D0 sum; D2+ worsen.
13. Post-activation DD can look better at later starts while full-horizon DD/PnL worsen — do not optimize on post-only DD.
14. Deep reach (of 25): D8=25, D10=25, D15=25, D20=25 (all targets were reached in this sample).
15. `NO_COBERTURA_AFTER_BREAK` combined sum=-3444.3574686669363 — never best vs Cobertura depths in this run (no_cobertura_better count=0).
16. No robust single handoff depth: best_combined_pnl votes={'BREAK_D0': 6, 'BREAK_D1': 8, 'BREAK_D5': 2, 'BREAK_D6': 1, 'BREAK_D4': 3, 'BREAK_D2': 4, 'BREAK_D10': 1}.
17. Fine grid D0–D6 matters: D1 has the only aggregate PnL edge and shifts recovery membership; still not robust enough for live policy.
18. Break should remain an **armed signal**, not an automatic immediate Cobertura start — but pure depth-under-break is also not a sufficient activation policy by itself (D0 ≠ legacy winners).
19. A later structure-aware reclaim/confirm trigger remains plausible research (legacy T1@6% still recovers APT/TIA); depth-only handoff does not replace it.
20. Live blockers: unresolved BCH/TRX breaks; wait-phase long adds; handoff cancel semantics; D0/legacy start-state mismatch; no capital/ops validation; research equity path approximations.

APT BREAK_D0 recovered_120d=False combined=-26.212659918068734 activation=2026-01-19T00:00:00+00:00@1.7223
BREAK_D0 recovered_120d count=2

## Decision

`BREAK_HANDOFF_DEPTH_AUDIT_PASS_WITH_WARNINGS`

