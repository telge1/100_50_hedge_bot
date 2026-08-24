# Strategy Lab P4A validation

P4A is the first validation phase for `StrategySpecV2`. It provides a
deterministic, read-only validator framework with feature binding checks,
rule-tree operand typing, and plugin signal basics.

## Scope

P4A validates:

- Feature bindings against `catalog/v2` feature contracts
- Parameter bindings (features and plugin config) via normative V2 contracts
- Plugin parameter `binding_target` (`plugin_ref_config` vs promoted signal fields)
- Plugin `mode_contract` (`required` / `optional` / `not_applicable`)
- Rule-tree operand resolution and operator signature matching
- `operator_contract_version` on rule-based and state-machine signals
- Plugin signal basics (identity, kind, config, policy, direction, required features)

P4A does **not** validate:

- State-machine graph structure (P4B)
- Component reference resolution or cycles (P4B)
- Data requirements, timeframes, warm-up, entry, padding, outcome, exit, costs, portfolio (P4C)
- Plugin execution or adapter imports

## API (provisional)

```python
from orderbook_analyse.strategy_lab.validation import (
    CatalogBundleV2,
    production_catalog_bundle_v2,
    validate_strategy_v2_p4a,
    require_valid_strategy_v2_p4a,
)

catalogs = production_catalog_bundle_v2()
report = validate_strategy_v2_p4a(spec, catalogs)
```

`validate_strategy_v2_p4a` is intentionally provisional until P4B/P4C are
implemented. Catalogs must be passed explicitly; there are no hidden global
fallbacks.

## Catalog bundle

`CatalogBundleV2` bundles the closed `catalog/v2` registries:

- features
- operators
- plugins

`production_catalog_bundle_v2()` wraps only the static production registries.

## Reports

`ValidationReport` contains a sorted tuple of `ValidationIssue` values.

- `is_valid` is true when there are no errors
- warnings alone do not invalidate a report
- issues are sorted by: path, severity, code, message

`ValidationFailedError` from `require_valid_strategy_v2_p4a` carries the full
report.

## Path format

Stable dotted/bracket paths, for example:

- `features[1].catalog_feature_id`
- `features[1].bindings[0].value`
- `signal.long.trigger.left.feature_alias`
- `signal.plugin.config[2].value`

## Issue codes

Closed stable codes include:

| Area | Codes |
|------|-------|
| Features | `FEATURE_DUPLICATE_ALIAS`, `FEATURE_UNKNOWN_ID`, `FEATURE_CONTRACT_VERSION`, `FEATURE_DUPLICATE_PARAMETER`, `FEATURE_MISSING_PARAMETER`, `FEATURE_UNKNOWN_PARAMETER`, `FEATURE_PARAMETER_TYPE`, `FEATURE_PARAMETER_BOUNDS`, `FEATURE_RATE_UNIT`, `FEATURE_IDENTIFIER_VALUE` |
| Operands / operators | `OPERAND_UNKNOWN_FEATURE_ALIAS`, `OPERAND_UNKNOWN_FEATURE_OUTPUT`, `OPERATOR_UNKNOWN`, `OPERATOR_CONTRACT_VERSION`, `OPERATOR_SIGNATURE_MISMATCH`, `OPERATOR_RESULT_NOT_BOOLEAN` |
| Plugins | `PLUGIN_UNKNOWN`, `PLUGIN_CONTRACT_VERSION`, `PLUGIN_KIND`, `PLUGIN_RESERVED_CONFIG_KEY`, `PLUGIN_DUPLICATE_PARAMETER`, `PLUGIN_MISSING_PARAMETER`, `PLUGIN_UNKNOWN_PARAMETER`, `PLUGIN_PARAMETER_TYPE`, `PLUGIN_PARAMETER_BOUNDS`, `PLUGIN_RATE_UNIT`, `PLUGIN_POLICY_MISMATCH`, `PLUGIN_MODE_MISMATCH`, `PLUGIN_REQUIRED_FEATURE_MISSING`, `PLUGIN_REQUIRED_FEATURE_MISMATCH`, `PLUGIN_DIRECTION_UNSUPPORTED` |

All listed codes are **P4A-active**. Unknown feature outputs in rule trees emit
`OPERAND_UNKNOWN_FEATURE_OUTPUT`. Non-comparable output types (for example
`cluster_snapshot` collections) emit `OPERATOR_SIGNATURE_MISMATCH`.

P4B/P4C codes are not pre-declared in the enum; they will be added when those
phases are implemented.

Catalog integrity rejects ambiguous operator signature overloads. The validator
raises `ValidationInvariantError` if an integrity-invalid operator catalog is
supplied.

## Behaviour

- Collects all independent errors in one pass; never stops at the first issue
- Does not mutate the input `StrategySpecV2` or catalogs
- Does not add defaults or repair invalid specs
- Does not execute plugins or perform I/O
- Avoids error cascades (for example unknown feature aliases do not produce
  follow-on operator signature errors)
- `ComponentReference` is structurally accepted in P4A; existence and cycles are P4B

## Typed issue context

Optional structured context uses a closed union (`UnknownIdentifierContext`,
`ExpectedActualTypeContext`, `ExpectedActualVersionContext`, `ParameterNameContext`,
`BoundsContext`, `OperatorSignatureContext`, `FeatureAliasContext`). Public
validation models do not use `dict` or `Any`.
