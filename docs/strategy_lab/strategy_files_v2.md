# Strategy Lab — StrategySpec V2 YAML files (P6)

P6 closes the authoring loop:

```text
YAML
→ safe load (P2)
→ StrategySpecV2 decode (P6)
→ P4C validate
→ canonical compile + SHA256 (P5)
```

## API

```python
from orderbook_analyse.strategy_lab.decoder_v2 import (
    StrategyDecodeError,
    decode_strategy_v2,
    load_strategy_v2_yaml,
    load_strategy_v2_yaml_file,
    load_compile_strategy_v2,
)
```

- `load_strategy_v2_yaml` / `load_strategy_v2_yaml_file` use the existing safe loader only.
- `load_compile_strategy_v2(path, catalogs)` runs `load → decode → compile_strategy_v2`.
  P4C is **not** called twice; compilation already requires P4C.

## Example strategies

| File | Plugin | Notes |
|------|--------|-------|
| `strategies/strategy_lab/edc_m0_strict_sync_v2.yaml` | `edc_m0_strict_sync` | Full research space for TP/SL/horizon/roundtrip |
| `strategies/strategy_lab/cluster_sweep_v2.yaml` | `cluster_sweep` | Empty research space; Phase-1 exit/cost baseline |

Golden artifacts (byte-identical compile output):

- `strategies/strategy_lab/compiled/*.canonical.json`
- `strategies/strategy_lab/compiled/*.sha256` (64 lowercase hex + optional newline)

## Universe hash

Both files pin:

```text
config/universe_tradeable_51.json
content_hash = sha256: + SHA256(exact committed file bytes)
```

Computed value:

`sha256:796ace7b68178a52279aee256ea7c1a109a3aa780b8ebf36d965f89a300b49bb`

This is the raw file digest, not a normalized symbol-list hash.
Runtime decoder/validator/compiler **do not** open that file.

## EDC baseline

- Signal TF 5m, execution TF 1m
- Features (frozen `edc_m0_strict_sync` catalog contract in `catalogs/v2/plugins.py`):
  - `ema_fast` → `ema` period **9**
  - `ema_slow` → `ema` period **20**
  - `atr` → `atr_wilder` period **14**
  - **no** `ema_medium`, **no** period **59** in the plugin `required_features`
- Note: legacy research `EmaDualCrossConfig` still defaults to 9/20/59 and warmup
  `59+20=79`, and catalog warmup reuses 79 bars — but the Phase-1 V2 plugin feature
  contract intentionally binds only the dual-cross pair as `ema_fast=9` /
  `ema_slow=20` plus ATR. EMA 9/20/59 belongs to **cluster_sweep**, not EDC V2.
- Decision: signal bar close → next signal-TF bar open @ bar open
- Notional 1000 USDT
- TP 0.75%, SL 0.50%, horizon 8h, roundtrip 0.15%
- Slippage/funding `not_modeled`, compounding `false`
- Research candidates: TP 0.40/0.50/0.60/0.75; SL 0.50/1.00; horizon 4/6/8h; roundtrip 0.11/0.15/0.20
- Root values are the baseline (no separate baseline object)

## Cluster baseline

- Signal TF 15m, execution TF 1m
- Features: EMA 9/20/59, ATR 14, LLD clusters `gap_pct=0.10%`, `minimum_pools=3`
- Decision: confirmation bar close → next bar open after confirmation @ bar open
- Research space empty (valid)
- **Research assumption:** TP 0.75%, SL 0.50%, horizon 8h, roundtrip 0.15% are Phase-1
  placeholders required by `ExitSpecV2` / `CostsSpecV2`. They are **not** claimed as a
  frozen legacy cluster production baseline.

## Provenance

All provenance fields are explicit YAML literals (git commit, repository, source paths,
`catalog/v2`, plugin id + contract version, causality). No environment or git resolution.

## Decoder rules (summary)

- Closed unions require `kind` and select exactly one `_schema_kind` variant
- YAML sequences become tuples only for tuple annotations
- `Decimal` preserved; `float` and bool-as-int rejected
- Unknown fields / missing required fields / bad enums fail with exact paths
- Only `metadata.schema_version = strategy_spec/v2` is accepted
