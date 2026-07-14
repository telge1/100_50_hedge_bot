# Mapping / GT Root-Cause Audit (Phase C0)

Read-only. Uses Phase-B `state_timeline_5m.csv`. No SM/policy/threshold changes.

## Central question

Is the ~1.2% Phase-B clear-match rate mostly the state machine, or mapping/GT?

## Answer (short)

**Both.** Mapping `*_weakening → UNCLEAR` and sensitive CLEAR GT **inflate** mismatch.
Independently, the SM **really sticks** in weakening (Mar6 all-day) because exits need
concurrent multi-event combos, and existing policy still allows long in `bullish_weakening`.

## Match-rate contrasts

See `ground_truth_sensitivity.csv` / summary.match_rates:
- existing map × existing GT
- weakening-as-trend map × existing GT (diagnostic lift)
- strict / strong-only GT variants

## Key files

- `selected_segments.csv`, `segment_timelines.csv`
- `mapping_comparison.csv`
- `weakening_stuck_cases.csv`
- `march_06_root_cause.csv`, `march_08_09_root_cause.csv`
- `root_cause_findings.csv`

Deterministic hash: `3711a8c8fa52543b1372fad7a11f772067a4a51c8da3904a2ff7b7792dd517d1`

