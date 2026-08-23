# StrategySpec V2 — Signal Architecture

StrategySpec V2 (`metadata.schema_version: strategy_spec/v2`) is a **breaking**
contract separate from V1. The YAML loader does **not** auto-migrate V1 documents;
use an explicit migration step (planned, not part of P3.5).

## Three signal modes

Every V2 strategy declares exactly one `signal` variant (discriminated by `kind`):

| `kind` | Type | Role |
|--------|------|------|
| `plugin` | `PluginSignalSpec` | Delegate signal logic to a registered plugin |
| `rule_based` | `RuleBasedSignalSpec` | Boolean rule tree per side |
| `state_machine` | `StateMachineSignalSpec` | Finite-state signal lifecycle |

Entry, exit, fees, slippage, funding, and portfolio assumptions remain **outside**
the signal block at the V2 root.

## One feature source

All feature bindings live in `StrategySpecV2.features`. Signal variants do **not**
carry their own feature lists. Rule operands reference features via
`FeatureOutputReference` (`feature_alias` + explicit `output_id`).

## Rule tree

`BooleanExpression` is a closed recursive union:

- `comparison` — binary operator with `left` / `right` operands
- `boolean_and` / `boolean_or` — at least two operands
- `boolean_not` — exactly one operand
- `component_ref` — reference to a local `RuleComponentSpec`

Operands are `feature_output` or `literal` (`ParamValue` only). Structural checks
(arity, tuple types) are enforced in P3.5; catalog/alias/operator semantics belong
to P4.

## Local components

`RuleComponentSpec` and `StateMachineSignalSpec.components` define reusable named
sub-expressions scoped to the signal. Components may reference each other by ID;
cycle detection is a P4 semantic concern.

## State machine

`StateMachineSignalSpec` adds:

- `initial_state`, `states` (≥1), `transitions`, optional `timeouts`
- `transition_execution_policy` — only `one_per_evaluation_bar`
- `transition_conflict_policy` — `error_on_multiple` or `priority_wins`
- `reset_rules` — the **only** reset mechanism

### Transition conflict

When multiple conditional transitions match on one evaluation bar,
`transition_conflict_policy` governs behavior (error vs. highest `priority` wins).
`transition_execution_policy` limits execution to **one transition per bar**.

### Timeout counting

`TimeoutTransitionSpec` fires after `after_bars` complete bars in `in_state`.
Timeouts have no `purpose`, `emission`, or reset fields — the type itself denotes
a timeout event.

## Reset rules

`reset_rules: tuple[ResetRule, ...]` maps `ResetEvent` values
(`signal_emitted`, `invalidated`, `timeout`) to a `target_state`. No other reset
fields exist on the state machine.

## Signal emission vs. entry

`TransitionSpec.emission: SignalEmissionSpec | None` declares **signal intent**
(`side`, `emission_id`). It does not execute entries; `EntrySpec` at the root
handles tradable execution separately.

## No automatic short mirroring

`RuleBasedSignalSpec` exposes optional `long` and `short` `SideRuleBundle` values.
There are no mirror fields; each side is authored explicitly. P4 validates
consistency with `directionality`.

## No silent defaults

Policy fields (`directionality`, `evaluation_timing`, `rules_embedded_in_yaml`,
conflict/execution policies) have **no outcome-changing defaults`. Optional
structural empties (`components=()`, `timeouts=()`) are explicit.

## V1 and V2 separation

- `StrategySpec` / `StrategySpecV1` — V1 root (unchanged public alias)
- `StrategySpecV2` — V2 root with `strategy_spec/v2` metadata
- Separate JSON Schema files: `strategy_spec_v1.schema.json` and
  `strategy_spec_v2.schema.json`

## No automatic loader migration

The safe YAML loader (P2) is unchanged. Loading a V2 document still yields a plain
dict; constructing `StrategySpecV2` is a separate step (P4+).
