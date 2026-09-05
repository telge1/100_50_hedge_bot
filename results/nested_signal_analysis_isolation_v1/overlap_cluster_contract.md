# Overlap Cluster Contract

## Rule

If analysis windows `[analysis_pre_start_ts, analysis_post_end_ts]` overlap within the same `parent_fight_event_id`:

1. Signals remain **separate cases** (separate `signal_id`, profile, metrics, outcomes).
2. Each stores `overlapping_signal_ids`.
3. A deterministic `overlap_cluster_id` is assigned to the connected component.
4. Raw Full-OB is **not** duplicated.
5. Metrics are still computed **per signal**.
6. Statistical exports set `independent_observation=false` for clustered members — they must not be silently counted as independent observations.

## Determinism

- Union-Find over pairs with overlapping windows (same parent).
- Cluster id = `overlap_cluster_{sha256(sorted(signal_ids))[:16]}`.
- Output order: `(parent_fight_event_id, trigger_ts, signal_id)`.
- Input permutation must not change cluster membership or cluster ids (proven in tests).

## Non-overlap

Isolated signals: `overlap_cluster_id=null`, `independent_observation=true`.

## Alias

`market_episode_id` ≡ `overlap_cluster_id` for research exports.
