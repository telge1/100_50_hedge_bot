# Historical Blocker Fill-Level Replay

**Decision: `BLOCKER_FILL_REPLAY_PASS_WITH_WARNINGS`**

APT: **APT_FILL_REPLAY_WARNING**

- fills_before=9 fills_at_or_after=4
- last_before=2026-01-18T23:50:00+00:00 CYCLE_4_LONG_ADD lq=296.365 sq=197.59699999999998
- first_after=2026-01-19T00:00:00+00:00 CYCLE_4_SHORT_REDUCE lq=526.87 sq=263.43499999999995
- pre-signal state DIFFERS from old cycle-4 candidate: got 296.365/197.59699999999998 vs candidate 526.87/199.22399999999993

## Answers

1. Trades processed / exact pre-signal book: **25 / 27**
2. Full-replay fingerprint match vs tem_end_blockers: **25 / 25**
3. Exact pre-signal states: **25**
4. Ready for neutralization: **25**
5. Unresolved: **2** — see `unresolved_replays.csv`
6. APT fills before/after 00:00: see APT details above / ledger rows (before=9, after=4).
7. Old APT cycle-4 candidate vs true pre-signal: candidate=526.87/199.22399999999993 vs pre-signal=296.365/197.59699999999998
8. Long/short/net qty: `blocker_pre_signal_states.csv`
9. Realized / fees / total economics at signal: same file (`FEE_RECONSTRUCTION_UNRESOLVED` where entry/exit fee fields missing).
10. Required short fill qty: `blocker_neutralization_calculation.csv`
11. New short average: same file (`post_neutralization_short_avg`).
12. Problems: invariant_fails=0, replay_mismatch_rows=0, fee_issues=50, market_mismatches=0.

## Decision

`BLOCKER_FILL_REPLAY_PASS_WITH_WARNINGS`
