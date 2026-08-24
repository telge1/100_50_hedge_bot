# Strategy Lab P4B validation

P4B extends P4A with graph-structure validation for rule-based and
state-machine signals. It provides deterministic, read-only checks for local
component references, rule-based directionality, and state-machine graph
semantics.

## API

P4A remains available unchanged:

```python
from orderbook_analyse.strategy_lab.validation import (
    validate_strategy_v2_p4a,
    require_valid_strategy_v2_p4a,
)
```

P4B runs P4A first and appends graph issues into one sorted report:

```python
from orderbook_analyse.strategy_lab.validation import (
    CatalogBundleV2,
    production_catalog_bundle_v2,
    validate_strategy_v2_p4b,
    require_valid_strategy_v2_p4b,
)

catalogs = production_catalog_bundle_v2()
report = validate_strategy_v2_p4b(spec, catalogs)
```

There is no public `validate_strategy_v2` yet. P4B is exported only from
`strategy_lab.validation`, not from the top-level `strategy_lab` package.

## Local components

Components exist locally within:

- `RuleBasedSignalSpec.components`
- `StateMachineSignalSpec.components`

`PluginSignalSpec` has no local rule components.

Checks:

- `component_id` is unique within the signal mode
- every `ComponentReference` resolves to a defined local component
- no direct or indirect component cycles
- references are collected from side bundles, transition conditions, and
  component roots

Components are not globally registered; visibility is limited to the enclosing
strategy signal.

## Cycle diagnosis

Cycles are detected with a deterministic Tarjan SCC pass over component-root
dependency edges. Each distinct cycle is reported once with a canonical path
(starting at the lexicographically smallest component id). Unknown references
do not create artificial cycle edges.

## Rule-based directionality

| Directionality | Required bundles | Forbidden bundles | Side constraints |
|----------------|------------------|-------------------|------------------|
| LONG | `long` | `short` must be `None` | `long.side == LONG` |
| SHORT | `short` | `long` must be `None` | `short.side == SHORT` |
| BOTH | `long` and `short` | — | `long.side == LONG`, `short.side == SHORT` |

No automatic mirroring or mirror fields are applied.

## State-machine graph

Structural checks include:

- unique `state_id`, `transition_id`, and `timeout_id`
- `initial_state` references a defined state
- transition `from_state` / `to_state` references
- timeout `in_state` / `to_state` references
- reset `target_state` references

### Separate transition and timeout ID namespaces

`transition_id` and `timeout_id` are validated in separate namespaces. The same
stable identifier may appear once as a transition id and once as a timeout id
without conflict, because they are distinct typed fields with separate
collections.

## Reachability

Reachability is computed structurally from `initial_state` over:

- valid `TransitionSpec` edges (`from_state → to_state`)
- valid `TimeoutTransitionSpec` edges (`in_state → to_state`)

`ResetRule` targets are **not** reachability edges.

When `initial_state` is unknown, `SM_INITIAL_STATE_UNKNOWN` is emitted and no
`SM_UNREACHABLE_STATE` cascade follows.

## Priorities

Within each valid source state, `TransitionSpec.priority` and
`TimeoutTransitionSpec.priority` share one priority namespace. Duplicate
priorities emit `SM_DUPLICATE_PRIORITY` with deterministic context listing the
conflicting event ids. Unknown source states are excluded from priority checks.

## Emission

- `TransitionPurpose.INVALIDATION` requires `emission is None`
- `TransitionPurpose.NORMAL` allows optional `SignalEmissionSpec`
- timeout transitions have no emission field
- emission side must match `StateMachineSignalSpec.directionality`
- directionality promises minimum long/short emission coverage on valid normal
  transitions

Invalidation emissions do not count toward coverage and emit
`SM_INVALIDATION_WITH_EMISSION` separately.

## Reset rules

- at most one `ResetRule` per `ResetEvent`
- `target_state` must exist
- reset events must have a matching graph source:
  - `SIGNAL_EMITTED`: valid normal emission exists
  - `INVALIDATED`: valid invalidation transition exists
  - `TIMEOUT`: valid timeout exists

Missing reset rules are allowed and mean no automatic reset for that event.

## Issue codes

P4B adds component, rule-based directionality, and state-machine codes listed
in `ValidationIssueCode`. Each active code has a dedicated emission test.

## Deferred P4C scope

P4B does **not** validate:

- `StrategySpec.data_requirements`
- signal/execution timeframes
- warm-up, source/outcome padding
- entry causality, exit, intrabar
- fees, slippage, funding, portfolio
- baseline vs. research parameter space
- provenance, analysis, or validation requirements

These belong to P4C.

## Reports and determinism

Issues remain sorted by path, severity, code, and message. Validation is
read-only and collects all independent errors without automatic repair.
