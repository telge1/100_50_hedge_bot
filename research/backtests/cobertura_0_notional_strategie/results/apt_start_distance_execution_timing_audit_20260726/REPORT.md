# APT Start-Distance Execution Timing Audit

**Decision: `APT_START_DISTANCE_EXECUTION_ROBUST`**

Baseline fingerprint OK: **True**
T0 6% winner fingerprint OK: **True**

## Answers

1. T0 @ 6% recovered: **True** (fill=`2026-01-19T00:05:00+00:00` @ `1.6447`)
2. T1 @ 6% recovered: **True** (trigger=`2026-01-19T00:00:00+00:00`, fill=`2026-01-19T00:05:00+00:00` @ `1.6447`)
3. T2 @ 6% recovered: **True** (fill=`2026-01-19T00:05:00+00:00` @ `1.6447`)
4. T3 @ 6% recovered: **True** (fill=`2026-01-19T00:05:00+00:00` @ `1.6447`)
5. Planned live semantics: **T0** (observe current price; market fill immediately).
6. Smallest threshold recovering under conservative T1∩T2: **0.055** (T1 fill `2026-01-19T00:05:00+00:00` @ `1.6447`; T2 fill `2026-01-19T00:05:00+00:00` @ `1.6447`)
7. Trigger/fill delay impact:
   - T0: recovered=6/7, first_thr=0.055, at_6pct=True
   - T1: recovered=7/7, first_thr=0.05, at_6pct=True
   - T2: recovered=6/7, first_thr=0.055, at_6pct=True
   - T3: recovered=7/7, first_thr=0.05, at_6pct=True
8. Current APT open-based winner robustness: **causally robust across conservative modes**
9. Rule for subsequent 25-blocker audit: Use `minimum_start_distance_pct` with **explicit timing mode**. Prefer conservative **T1/T2** (prior close → next/current open) if a recovering threshold exists; do not silently assume T0 same-open fills equal live latency. On APT, document T0@6% as the open-path reference winner (fill=2026-01-19T00:05:00+00:00).

Decision: `APT_START_DISTANCE_EXECUTION_ROBUST`

