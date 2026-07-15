# Research Runs — Reproducible Scanner Outputs

## Purpose

Store deterministic, reproducible **baseline** results from the existing regime
scanner pipeline under a unique `run_id`, with a fachlich-stable
`run_fingerprint`.

This layer is **not** an optimizer. It does not search parameters, score profit,
or select a best run.

```text
MySQL / Feather Candles
        ↓
bestehende Scanner-Pipeline (unverändert)
        ↓
ResearchRunContext
        ↓
deterministische Outputs
        ↓
Research-Result-Store  (research_* tables)
```

Candle tables (`market_candles`, `data_validation_runs`) stay untouched.

## Scope vs optimizer

| In scope | Out of scope |
|----------|--------------|
| Run metadata | Parameter grids |
| Parameter set + hash | Live bot wiring |
| Trend states | TP / PnL maximization |
| Structure events | Automatic best-run selection |
| Final signals (when pipeline exported) | Scheduler |
| Output hashes | New strategy logic |

## Tables

| Table | Role |
|-------|------|
| `research_parameter_sets` | Deduplicated fachliche configs by `parameter_hash` |
| `research_runs` | One row per execution (`run_id` UUID) |
| `research_trend_states` | Normalized trend-timeline snapshots |
| `research_structure_events` | Structure events in the analysis window |
| `research_signals` | Final signals (momentum confirmations when exported) |
| `research_run_metrics` | Counts / runtime metrics |

Foreign keys are **not** used (DB user lacks `REFERENCES`). Integrity is
enforced in application code + unique indexes.

Indexes / unique keys:

- `uq_research_parameter_sets_hash`
- `research_runs.run_id` PK, indexes on fingerprint / symbol+window / status
- `uq_research_trend_states_run_event (run_id, event_key)`
- `uq_research_structure_events_run_event (run_id, event_key)`
- `uq_research_signals_run_signal (run_id, signal_key)`
- `uq_research_run_metrics_run_name (run_id, metric_name)`

## Run identity

### `run_id`

UUID of a concrete execution. Always unique per invocation.

### `run_fingerprint`

SHA-256 of a canonical JSON payload (`sort_keys=True`, separators `,` / `:`):

```text
scanner_name, scanner_version, symbol, exchange, data_source,
start_time, end_time, warmup_start, decision_time,
timeframes, history_candles, parameters (full canonical set),
code_version (git HEAD SHA when available),
candle_input_hashes {5m, 15m, 30m}
```

Timestamps are UTC ISO-8601 via `ensure_utc_timestamp(...).isoformat()`.

**Excluded:** `run_id`, wall-clock start/finish, duration, secrets, file paths.

Identische fachliche Runs → identical fingerprint; different executions →
different `run_id`.

## Parameter set

`ResearchParameterSet` contains only fachliche knobs:

- scanner name / version
- exchange, symbol, data_source
- timeframes, history_candles
- `RegimeScannerConfig`, `TrendStateConfig`, `PriceActionConfig`, `MomentumConfig`

Serialized with each nested `to_dict()`, then `parameter_hash = sha256(json)`.

No passwords, no file paths. Secret-like keys are rejected.

## Status and transactions

1. Insert `research_runs` with `status=running` (after parameter set + fingerprint).
2. Run scanner with fresh in-memory state.
3. Normalize outputs → hashes → **one transaction** writes child rows + marks
   `completed`.
4. On scanner or store failure: mark `failed`, roll back result transaction.
   No partial “completed” children.

Older runs are never deleted automatically.

## Event keys

| Entity | Key |
|--------|-----|
| Trend state | `{decision_time}\|{state}\|{transition_reason}` |
| Structure | `{event_time}\|{event_type}\|{direction}\|{ref_pivot_time}\|{price}\|{timeframe}` |
| Signal | `{timestamp}\|{direction}\|{signal_type}\|{setup_id}` |

Rare collisions get a stable ordinal suffix `|#{n}`.

## Output normalization

- UTC ISO timestamps
- Finite floats via `%.17g`; NaN/Inf → `null`
- Sorted as documented (timestamp; structure also by type/key; signals by direction/key)
- `json_safe` payloads in `metadata_json`

## Hash semantics

| Hash | Source |
|------|--------|
| `parameter_hash` | Canonical parameter JSON |
| `run_fingerprint` | See above |
| `candle_hash_*` | `candles_export_hash` over `[warmup_start, end)` |
| `trend_state_hash` | Normalized trend rows |
| `structure_event_hash` | Normalized structure rows |
| `price_action_hash` / `momentum_hash` / `signal_hash` | Pipeline groups when exported; else `not_exported` |
| `combined_output_hash` | Only groups that are **not** `not_exported` / `not_available` |

## Baseline window

```text
Warm-up floor: 2025-12-27T00:00:00Z
Analyse:       2026-03-01T00:00:00Z → 2026-03-08T00:00:00Z
```

Trend replay uses causal indicator warm-up (~450 bars before start, floored at
the warm-up floor) — same idea as the MySQL/Feather parity audit.

Candle input hashes cover the full `[warmup_start, end)` slice.
HTF hashes use scanner `aggregate_candles(..., decision_time=end)`.

## CLI

Research runner default `data_source` is **mysql**. Scanner library default
remains **feather**.

```bash
PYTHONPATH=. python3 -m research.regime_scanner.research_runs init-schema

PYTHONPATH=. python3 -m research.regime_scanner.research_runs run-baseline \
  --exchange bybit \
  --symbol APTUSDT \
  --data-source mysql \
  --warmup-start 2025-12-27T00:00:00Z \
  --start 2026-03-01T00:00:00Z \
  --end 2026-03-08T00:00:00Z

# Optional: skip PA/Momentum pipeline (signals → not_exported)
PYTHONPATH=. python3 -m research.regime_scanner.research_runs run-baseline --skip-pipeline

PYTHONPATH=. python3 -m research.regime_scanner.research_runs compare-runs \
  --run-id-a <RUN_A> --run-id-b <RUN_B>

PYTHONPATH=. python3 -m research.regime_scanner.research_runs show-run --run-id <ID> --sample
```

Env: `research/regime_scanner/.env.regime_db` (gitignored). Never commit secrets.

## Read API

```python
from research.regime_scanner.research_runs import (
    get_run, list_runs, load_trend_states,
    load_structure_events, load_signals, compare_runs,
)
```

## Known limitation — Price Action / Momentum

Trend-state and structure parity (Feather vs MySQL) are proven.

The full long pipeline export (PA events + momentum confirmations as “final
signals”) is fachlich determined at identical candle input, but has not been
exhaustively re-proven end-to-end for the multi-hour week walk in this package.
Use `--skip-pipeline` for a fast trend/structure-only baseline; enable pipeline
when you accept the longer runtime.

When pipeline is skipped:

```text
price_action_hash = not_exported
momentum_hash = not_exported
signal_hash = not_exported
```

`combined_output_hash` then covers only trend + structure.

## Future: variant runner

Next step after proven baseline reproducibility: store alternate
`ResearchParameterSet`s under new fingerprints — still without auto-optimization.
