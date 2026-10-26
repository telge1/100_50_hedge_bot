# BT-D6 experiment configs

Each experiment is an **immutable copy** under `results/phase_bt_d6_config_experiments_v1/`.

Copy `pool_landscape_baseline_v1.json`, change **only** approved JSON fields (e.g. `upper_gap_min_pct`), save with a new filename, run via `prepare_experiment()` — **no parameter sweeps in BT-D6**.

Example gap variants (not executed in BT-D6):

- `upper_gap_min_pct`: 0.35, 0.5, 0.75
