# EDC M0 multicoin run + export (P2D3)

Thin CLI and deterministic export over the P2D2 sequential runner.

```text
Strategy YAML
  → load / decode / P4C / compile
  → ClickHouse client (CLI-owned)
  → run_edc_m0_multicoin_v2
  → run_manifest.json, coin_summary.csv, trades.csv, failures.json
```

No new trading logic. No Cluster. `results/` is gitignored / not committed.

## Public API

```python
export_edc_multicoin_artifacts_v2(run, spec, *, output_dir: Path) -> None

run_and_export_edc_m0_multicoin_v2(
    *,
    strategy_path: Path,
    universe_path: Path,
    start: datetime,
    end: datetime,
    output_dir: Path,
    client: ClickHouseQueryClient,
    symbols: tuple[str, ...] | None = None,
    checkpoint_dir: Path | None = None,  # default: output_dir/checkpoints
    resume: bool = False,
    retry_failures: bool = False,
) -> EdcMulticoinRunV2
```

## CLI

Three-coin smoke window:

```bash
PYTHONPATH=src python scripts/run_strategy_lab_edc_multicoin.py \
  --strategy strategies/strategy_lab/edc_m0_strict_sync_v2.yaml \
  --universe config/universe_tradeable_51.json \
  --start 2026-07-24T00:00:00Z \
  --end 2026-08-23T00:00:00Z \
  --output-dir results/strategy_lab/edc_m0_3coin_30d_v2 \
  --symbol XRPUSDT --symbol LITUSDT --symbol NEARUSDT
```

Full 51-coin (after P2D3 audit/commit; not part of this step):

```bash
PYTHONPATH=src python scripts/run_strategy_lab_edc_multicoin.py \
  --strategy strategies/strategy_lab/edc_m0_strict_sync_v2.yaml \
  --universe config/universe_tradeable_51.json \
  --start 2026-07-24T00:00:00Z \
  --end 2026-08-23T00:00:00Z \
  --output-dir results/strategy_lab/edc_m0_51coin_30d_v2
```

Options: `--resume`, `--retry-failures`, optional `--checkpoint-dir`
(default `<output-dir>/checkpoints`).

Window is half-open `[start, end)`. Spec roundtrip cost **0.11 %**.
Position size **1.000 USDT** (carried by the existing adapter path).

## ClickHouse

CLI uses `orderbook_analyse.orderbook_v2.ch_client.get_clickhouse_client`
(environment / project `.env`: `CLICKHOUSE_HOST`, `CLICKHOUSE_HTTP_PORT`,
`CLICKHOUSE_DATABASE`, `CLICKHOUSE_USER`, optional `CLICKHOUSE_PASSWORD`).
Importing the script or export module does **not** open a connection.

## Output files

| File | Content |
|---|---|
| `run_manifest.json` | hashes, plugin, universe, window, TFs, costs, counts, PnL |
| `coin_summary.csv` | one row per requested symbol (+ win/loss/unresolved) |
| `trades.csv` | one row per trade (empty optional fields if unresolved) |
| `failures.json` | `[]` or list of `{symbol,error_type,error_message}` |

Decimals as lossless strings; UTC ISO; `sort_keys=True` for JSON; fixed CSV columns.
Atomic write: temp → flush → `fsync` → `os.replace`.

### Coin-summary definitions

- `winning_trades`: resolved and `net_pnl_usdt > 0`
- `losing_trades`: resolved and `net_pnl_usdt < 0`
- exact-zero PnL: neither winner nor loser
- `unresolved_trades`: coverage / incomplete horizon exits
- `win_rate`: `winning / (winning + losing)`; empty if denominator is 0
  (unresolved and zero-PnL excluded from the denominator)
- `avg_net_pnl_usdt`: mean of resolved `net_pnl_usdt` (includes zero); empty if none

## Resume

`--resume` / `--retry-failures` are forwarded to P2D2 checkpoints.
Resume re-exports; identical run ⇒ identical artifact bytes; no second resume layer.

## Local smoke

```bash
STRATEGY_LAB_EDC_MULTICOIN_EXPORT_SMOKE=1 PYTHONPATH=src \
  python -m pytest tests/strategy_lab/test_edc_multicoin_export_smoke.py -vv -s
```
