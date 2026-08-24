"""Signal definition models for StrategySpec V2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from orderbook_analyse.strategy_lab.models.enums import (
    Directionality,
    EvaluationTiming,
    SideName,
    TransitionConflictPolicy,
    TransitionExecutionPolicy,
)
from orderbook_analyse.strategy_lab.models.identifiers import ContractVersion, StableIdentifier
from orderbook_analyse.strategy_lab.models.rules import BooleanExpression, RuleComponentSpec
from orderbook_analyse.strategy_lab.models.state_machine import (
    ResetRule,
    StateSpec,
    TimeoutTransitionSpec,
    TransitionSpec,
)
from orderbook_analyse.strategy_lab.models.contracts_v2.enums import (
    ResearchConfirmationPolicyV2,
)
from orderbook_analyse.strategy_lab.models.plugin_ref_v2 import PluginRefV2
from orderbook_analyse.strategy_lab.models.strategy import (
    ConfirmationSpec,
    InvalidationSpec,
    SetupSpec,
    TriggerSpec,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class PluginSignalSpec:
    _schema_kind: ClassVar[str] = "plugin"
    plugin: PluginRefV2
    mode_id: StableIdentifier | None
    directionality: Directionality
    rules_embedded_in_yaml: bool
    confirmation_policy: ResearchConfirmationPolicyV2 | None
    setup: SetupSpec
    trigger: TriggerSpec
    confirmation: ConfirmationSpec
    invalidation: InvalidationSpec

    def __post_init__(self) -> None:
        if type(self.rules_embedded_in_yaml) is not bool:
            raise TypeError("rules_embedded_in_yaml must be exact bool")
        if self.rules_embedded_in_yaml:
            raise ValueError("rules_embedded_in_yaml must be False for PluginSignalSpec")


@dataclass(frozen=True, slots=True, kw_only=True)
class SideRuleBundle:
    side: SideName
    setup: BooleanExpression | None
    trigger: BooleanExpression
    confirmation: BooleanExpression | None
    invalidation: BooleanExpression | None


@dataclass(frozen=True, slots=True, kw_only=True)
class RuleBasedSignalSpec:
    _schema_kind: ClassVar[str] = "rule_based"
    operator_contract_version: ContractVersion
    directionality: Directionality
    evaluation_timing: EvaluationTiming
    components: tuple[RuleComponentSpec, ...] = ()
    long: SideRuleBundle | None
    short: SideRuleBundle | None


@dataclass(frozen=True, slots=True, kw_only=True)
class StateMachineSignalSpec:
    _schema_kind: ClassVar[str] = "state_machine"
    operator_contract_version: ContractVersion
    directionality: Directionality
    evaluation_timing: EvaluationTiming
    initial_state: StableIdentifier
    states: tuple[StateSpec, ...]
    transitions: tuple[TransitionSpec, ...]
    timeouts: tuple[TimeoutTransitionSpec, ...] = ()
    components: tuple[RuleComponentSpec, ...] = ()
    transition_execution_policy: TransitionExecutionPolicy
    transition_conflict_policy: TransitionConflictPolicy
    reset_rules: tuple[ResetRule, ...]

    def __post_init__(self) -> None:
        if type(self.states) is not tuple:
            raise TypeError("StateMachineSignalSpec.states must be a tuple")
        if len(self.states) < 1:
            raise ValueError("StateMachineSignalSpec requires at least one state")


SignalDefinition = PluginSignalSpec | RuleBasedSignalSpec | StateMachineSignalSpec
