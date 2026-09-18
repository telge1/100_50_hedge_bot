# Deprecated / Legacy Research Modules

These packages are **not** the active First-Touch/QDH entry-point. They remain for historical reproducibility and offline regression tests.

| Module | Lifecycle | Why kept |
|---|---|---|
| `mp_edge_event_study_v1` | LEGACY_REPRODUCIBILITY | Batch/episode lineage, tests |
| `mp_edge_event_study_v2` | LEGACY_REPRODUCIBILITY / ACTIVE_TRANSITIVE (armed/touch rules in tests) | Causal zone arming proofs |
| `mp_edge_event_batch_v1` | LEGACY_REPRODUCIBILITY | Frozen batch `20260916` producer |
| `mp_ob_feature_enrichment_v1` | LEGACY_REPRODUCIBILITY | Older OB feature path; tests |
| `mp_entry_confirmation_v1` | LEGACY_REPRODUCIBILITY | Separate entry study |
| `mp_big_move_case_control_v1` | ACTIVE_TRANSITIVE (`stats`) + legacy study | cliffs/median helpers used by FT |
| `mp_qdh_30event_case_control_v1` | ACTIVE_TRANSITIVE + legacy | wall_movement, paired, frozen universe hashes |
| `mp_qdh_v2_large_run_readiness_v1` | LEGACY_REPRODUCIBILITY | Readiness gate artifacts |
| `mp_qdh_canonical_integration_v1` | ACTIVE_TRANSITIVE | event_load, near_zero |
| `ob_forschungsengine_v1` | LEGACY_REPRODUCIBILITY | Earlier engine facade / stubs |
| `mp_wall_flow_qdh_silver_v1` | ACTIVE_TRANSITIVE | Silver adapters for FT path |

**Do not** present these as the live trading strategy. Active entry: `python -m obfull_research_engine.mp_qdh_first_touch_study_v1`.
