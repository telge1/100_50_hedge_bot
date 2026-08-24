# EDC M0 multicoin runner (P2D2)

Sequential orchestration over a Strategy universe subset:

```text
StrategySpecV2 + CompiledStrategyV2 + CatalogBundleV2
  + ClickHouseQueryClient (caller-owned)
  + universe_path + start/end + checkpoint_dir
→ run_edc_m0_multicoin_v2
→ EdcMulticoinRunV2
```

Per symbol: `load_edc_m0_market_data_v2` (P2D1) → `execute_edc_m0_strict_sync_v2` (P2B)
→ atomic JSON checkpoint. No parallelization, no exports, no enrichment, no Cluster.

## Public API

```python
run_edc_m0_multicoin_v2(
    spec: StrategySpecV2,
    compiled: CompiledStrategyV2,
    catalogs: CatalogBundleV2,
    *,
    client: ClickHouseQueryClient,
    universe_path: Path,
    start: datetime,   # UTC inclusive
    end: datetime,     # UTC exclusive
    checkpoint_dir: Path,
    symbols: tuple[str, ...] | None = None,  # None = full universe
    resume: bool = False,
    retry_failures: bool = False,
) -> EdcMulticoinRunV2
```

Result types (frozen, slots, kw_only):

- `SymbolRunFailureV2` — `symbol`, `error_type`, `message` (no tracebacks / addresses)
- `EdcMulticoinRunV2` — `strategy_hash`, `universe`, `start`, `end`,
  `requested_symbols`, `completed_runs`, `failures`
  - Properties (derived only): `completed_symbols`, `failed_symbols`,
    `candidate_count`, `trade_count`, `gross_pnl_usdt`, `costs_usdt`, `net_pnl_usdt`

Errors: `StrategyMulticoinError` (preflight / universe / checkpoint).

## Sequential flow

1. Preflight once: plugin / contract / mode / policy / P4C / canonical bytes /
   strategy hash / UTC window (`end > start`). No parameter overrides.
2. Load and verify universe file (see below).
3. Resolve symbol list in **full-universe order**.
4. For each symbol sequentially:
   - optional resume from checkpoint
   - load market data → execute adapter
   - write atomic checkpoint
5. Aggregate completed runs and failures (one symbol failure does not abort others).

No threads, processes, or dynamic Bybit universe queries.

## Universe check

Source of truth: `config/universe_tradeable_51.json`.

- File must exist
- SHA256 of unmodified bytes must equal `spec.universe.content_hash`
- JSON structure matches the existing universe contract
- Full universe: exactly 51 unique symbols
- Symbol order taken from the file (no silent uppercase normalize)
- ID / version fields checked when present in the file

If `symbols` is set: exact `tuple`, non-empty, no duplicates, every symbol in the
universe; execution order follows the full-universe order (not request or set order).
Unknown symbols are rejected (never ignored).

## Checkpoint format

Path: `checkpoint_dir / "symbols" / "<SYMBOL>.json"`

Versioned JSON (`checkpoint_format_version = edc_multicoin_checkpoint/v1`):

- Fingerprint: strategy hash, universe content hash, start/end UTC, plugin/contract,
  costs, signal/execution TFs
- `symbol`, `status` (`complete` | `failed`)
- Success: full `StrategyRunResultV2`
- Failure: `SymbolRunFailureV2`

Rules: Decimal as string, datetime canonical UTC, enums via `.value`,
`sort_keys=True`, no pickle, no host/runtime metadata.

### Atomic write

Write to a temporary file in the same directory → flush → `os.fsync` → close →
`os.replace`. On write/replace failure the temp file is removed when possible and
an existing final checkpoint is left intact. No partially visible final JSON.

## Resume rules

`resume=True` and a checkpoint exists:

- Success: skip only if fingerprint matches exactly:
  format version, strategy hash, universe id/version/content hash, window,
  plugin/contract/mode/policy, costs (incl. slippage/funding), signal/execution TFs,
  symbol. Mismatch or unknown/missing fields → fail-fast (`StrategyMulticoinError`).
- Failure: `retry_failures=True` re-runs; `retry_failures=False` loads the failure.
- Corrupt JSON → deterministic error (no silent ignore, no auto-delete).
- Resume reconstructs full `StrategyRunResultV2` so aggregated properties stay correct.
- Pure resume does not rewrite checkpoint bytes.

## Failure isolation

Caught at the symbol boundary **only**:

- `StrategyMarketDataError`
- `StrategyAdapterError`
- ClickHouse driver errors (when importable)

Unexpected exceptions (`TypeError`, `AssertionError`, `KeyError`, …) and
`BaseException` (`KeyboardInterrupt`, `SystemExit`, …) **propagate**. Prior symbols'
checkpoints remain. Preflight / checkpoint incompatibilities fail the whole run.

## No strategy overrides

Runner does not mutate Spec, Compiled, or catalogs. P2B may still run its own
per-coin guards.

## Local 3-coin smoke

Gate: `STRATEGY_LAB_EDC_MULTICOIN_SMOKE=1`

Symbols: `XRPUSDT`, `LITUSDT`, `NEARUSDT` (universe order: XRP → NEAR → LIT).
Window: `2026-07-24 00:00 UTC` → `2026-08-23 00:00 UTC` (exclusive).

Reference trade counts (v2): XRP 15, LIT 5, NEAR 12 (32 total).
Confirmed @ Spec 0.11% (live smoke): XRP net 33.5, LIT net 32, NEAR net 16.34916,
pooled net 81.84916, costs 35.2 (32 × 1.10).
Resume must issue **0** new ClickHouse queries and return an equal result object.

## Deferred (not in P2D2)

- Full 51-coin 30d run
- 8-month run
- Standardized CSV exports
- Enrichment join
- Root-cause / regime analysis
- Parallelization
- Cluster
- CLI / Dashboard
