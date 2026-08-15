# Cobertura Blocker Input Bundle

**Decision: `COBERTURA_BLOCKER_INPUT_BUNDLE_PASS_WITH_WARNINGS`**

APT: **APT_BUNDLE_PASS_WITH_WARNINGS**

## Answers

1. Blockers found (join keys for `first_break`): **27**
2. Startfähig (ready): **25**
3. Unresolved: **2** — `BCHUSDT|two_early_medium|continuous|0003` (MISSING_SIGNAL_AVAILABLE_TS;MISSING_OR_NONPOSITIVE_BREAK_LEVEL;MISSING_BREAK_KIND;MISSING_OR_NONPOSITIVE_MARKET_PRICE;MISSING_OR_NONPOSITIVE_NEUTRALIZATION_FILL;REPLAY_NOT_MATCH;NOT_READY_FOR_NEUTRALIZATION), `TRXUSDT|two_early_medium|continuous|0016` (MISSING_SIGNAL_AVAILABLE_TS;MISSING_OR_NONPOSITIVE_BREAK_LEVEL;MISSING_BREAK_KIND;MISSING_OR_NONPOSITIVE_MARKET_PRICE;MISSING_OR_NONPOSITIVE_NEUTRALIZATION_FILL;REPLAY_NOT_MATCH;NOT_READY_FOR_NEUTRALIZATION)
4. All ready are REPLAY_MATCH: **True**
5. Break level + market price present for all ready: **True**
6. Open order counts match embedded orders: **True**
7. Warnings total: **25** (primarily FEE_RECONSTRUCTION_UNRESOLVED)
8. APT values:
- trade_id: `APTUSDT|two_early_medium|continuous|0006`
- break_level: `1.7639`
- market: `1.7223`
- long/short: `296.365` / `197.59699999999998`
- avgs: `1.864531340748192` / `1.864561269615919`
- net: `98.76800000000003`
- fills before/after: `9` / `4`
- open orders: `4`
- warnings: `['FEE_RECONSTRUCTION_UNRESOLVED']`
- ready: `True`
9. Deterministic JSONL: sorted by trade_id/trigger_mode; `sort_keys=True`.
10. Cobertura runner may load ready records from `blocker_historical_states.jsonl` + `cobertura_start_scenarios.jsonl` (no full Cobertura run in this step).

## Decision

`COBERTURA_BLOCKER_INPUT_BUNDLE_PASS_WITH_WARNINGS`
