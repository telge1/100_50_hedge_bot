# Strategy Lab Catalogs V1

## Purpose

Strategy Lab P3 provides three closed, deterministic catalogs:

1. **Features** — generic derived values such as `ema`, `atr_wilder`, and
   `lld_liquidity_clusters`
2. **Operators** — comparison and cross operators with explicit causal contracts
3. **Signal plugins** — reference strategy families for EDC M0 and cluster sweep

Catalogs describe contracts and metadata only. They do not execute strategies,
load YAML, validate full `StrategySpec` documents, or import legacy modules.

## Closed registry rule

Each catalog is a fixed tuple compiled into the repository. Lookup is by exact ID
only. Unknown IDs raise `UnknownCatalogEntryError`. There is no runtime
registration, plugin discovery, or silent default.

## ID and version rules

- IDs use `lowercase_snake_case`
- Each descriptor carries `contract_version = catalog/v1`
- Catalog version is separate from `StrategySpec` schema version
- Breaking catalog changes require a new contract version and explicit review

## Generic features and bound requirements

Feature IDs describe the calculation kind, not concrete parameter values.
Plugins bind features through `BoundFeatureRequirement`:

- `alias` — stable usage name within the plugin (for example `ema_fast`)
- `feature_id` — catalog feature ID (for example `ema`)
- `bindings` — explicit typed parameter values (`IntParam`, `RateParam`, ...)

Rate parameters must use `RateParam` with an explicit `RateUnit`. For example,
TRP `cluster_gap_pct=0.10` means **0.10 percent of price**, not a 0.10 fraction.

## EDC baseline signal vs supportive confirmation

`edc_m0_strict_sync` separates:

- **Baseline signal detection** — M0 strict sync cross from signal-TF candles only
- **Supportive confirmation policy** — `confirmation_policy=core_research_supportive`
  filters detected candidates using trades, orderbook, and liquidity locations

Missing OI or liquidations do not block the supportive policy. Missing
confirmation-required sources must not be treated as implicit ALLOW.

## Feature vs data source

Features describe derived outputs. Data sources describe raw inputs and are
declared on plugins through `DataRequirementDescriptor` with explicit roles:

- `signal_required`
- `execution_required`
- `confirmation_required`
- `analysis_optional`
- `validation_optional`

Optional analysis sources must not block signal reproduction.

## Warm-up vs loading vs outcome padding

These concepts are separate:

- **Feature warmup** — minimum bars for a feature value to become valid
- **Signal warmup** — engine bar-index gate before a plugin may emit signals
- **Source loading padding** — calendar padding when fetching market data
- **Outcome evaluation padding** — calendar padding after the evaluation window

Loading and outcome padding are not feature or signal warmup.

## Operator contract

Operators describe arity, operand types, result type, null policy, and whether a
previous closed observation is required. Cross operators document:

- `crosses_above(a, b)` when `previous(a) <= previous(b)` and `current(a) > current(b)`
- `crosses_below(a, b)` when `previous(a) >= previous(b)` and `current(a) < current(b)`

Logical operators accept boolean operands only. Numeric comparisons accept
compatible numeric operand types only.

## Plugin contract

Signal plugins declare:

- bound feature requirements
- typed data requirements with roles
- signal-timeframe semantics (`fixed`, `allowed_set`, or `caller_configured`)
- execution timeframe
- decision timing and entry timing
- signal warmup and optional loading/outcome padding
- causality status and claim
- adapter binding status

For P3 both reference plugins are `adapter_pending`.

## No runtime discovery

Catalogs are not populated from imports, entry points, filesystem scans, or
legacy adapters. Provenance fields are metadata only.

## No silent defaults

`legacy_reference_value` documents a frozen legacy reference. It is not applied
automatically by Strategy Lab. Feature parameters such as `period` must be bound
explicitly per plugin usage unless a parameter is marked `must_be_explicit = False`
for documented optional metadata only.

## Causality principle

Descriptors must state when a value becomes available. Signal plugins must
declare decision timing separately from tradable entry timing. Lookahead semantics
are forbidden.

## Future catalog changes

- Additive IDs in a new `catalog/vN` are allowed when backward compatible
- Changing parameter meaning, timing, or required data for an existing ID is a
  breaking change
- Breaking changes require a new contract version and validator updates in P4
