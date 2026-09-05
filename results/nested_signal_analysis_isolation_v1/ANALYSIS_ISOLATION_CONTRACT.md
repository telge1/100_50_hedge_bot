# Analysis Isolation Contract — nested_signal_analysis_isolation_v1

## Status

```text
SIGNAL_LEVEL_ANALYSIS_ISOLATION=true
CROSS_SIGNAL_METRIC_CONTAMINATION=0 (blocked; attempts counted)
OVERLAP_CLUSTERING_IMPLEMENTED=true
```

## Scope

Offline follow-up to `nested_profile_edge_signal_v1`. Raw Full-OB packets may remain shared inside one parent capture. All **derived** analysis (profile edges, metrics, outcomes, coverage, eligibility) is namespaced by `signal_id`.

## Per-signal immutable contract

| Field | Source |
|-------|--------|
| `signal_id` | Parent: `{fight_event_id}_parent`; Nested: `nested_signal_id` |
| `parent_fight_event_id` | Parent event |
| `profile_id` / `profile_basis` / start/end / VAH/VAL/POC | Signal-owned; never overwritten by later signals |
| `edge` / `edge_price` | Signal-owned |
| `trigger_ts` / `trigger_price` | Signal trigger |
| `continuity_epoch_id` | Epoch at emission |
| `analysis_pre_start_ts` | `trigger − pre_seconds` |
| `analysis_post_end_ts` | `min(trigger + min_post, capture_available_until)` |
| `coverage_status` | FULL / PARTIAL_POST / GAP_BROKEN / INCOMPLETE |
| `continuous_capture` | **This signal window** (not parent-global) |
| `replayable` | Epoch coverage + parent replayable flag |
| `research_eligible` | Strict: continuous window + full post + replayable |
| `overlap_cluster_id` / `overlapping_signal_ids` | Deterministic connected components |

## Timing (existing contract only)

```text
Pre-Signal:  ringbuffer_minutes × 60 = 600 s (default)
Post-Signal: minimum_post_capture_minutes × 60 = 3600 s (default)
```

Near hard-cap with &lt; 3600 s post coverage:

```text
signal_research_eligible=false
reason=INSUFFICIENT_SIGNAL_POST_COVERAGE
```

## Isolation rules

1. Signal A analysis uses only A's contract, window, and epoch coverage.
2. Later signals must not mutate earlier profile edges, windows, metrics, or outcomes.
3. Gaps must not be spanned for continuous research eligibility.
4. Derived stores (`SignalMetricStore`) reject cross-signal writes and increment `contamination_attempts`.

## Persistence

- `{event_root}/signal_analysis_contracts.jsonl` — rewritten on each nested emit with full cluster set
- Nested bodies gain analysis fields at emit time
- Module: `signal_analysis_isolation.py`
- Manager: `_refresh_signal_analysis_contracts`, `_append_analysis_contracts_ledger`
