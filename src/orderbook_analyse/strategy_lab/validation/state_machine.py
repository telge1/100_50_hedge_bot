"""State-machine graph validation for P4B."""

from __future__ import annotations

from collections import defaultdict

from orderbook_analyse.strategy_lab.models.enums import (
    Directionality,
    ResetEvent,
    SideName,
    TransitionPurpose,
)
from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.signals import StateMachineSignalSpec
from orderbook_analyse.strategy_lab.models.state_machine import (
    ResetRule,
    SignalEmissionSpec,
    TimeoutTransitionSpec,
    TransitionSpec,
)
from orderbook_analyse.strategy_lab.validation._issue_helpers import make_error
from orderbook_analyse.strategy_lab.validation.diagnostics import (
    StatePriorityContext,
    UnknownIdentifierContext,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
)


def validate_state_machine(
    signal: StateMachineSignalSpec,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    state_ids: dict[str, int] = {}
    defined_states: set[str] = set()

    for index, state in enumerate(signal.states):
        state_key = state.state_id.value
        defined_states.add(state_key)
        if state_key in state_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_DUPLICATE_STATE_ID,
                    path=f"signal.states[{index}].state_id",
                    message=(
                        f"duplicate state_id {state_key!r} "
                        f"(first at signal.states[{state_ids[state_key]}])"
                    ),
                    context=UnknownIdentifierContext(identifier=state.state_id),
                )
            )
        else:
            state_ids[state_key] = index

    initial_known = signal.initial_state.value in defined_states
    if not initial_known:
        issues.append(
            make_error(
                ValidationIssueCode.SM_INITIAL_STATE_UNKNOWN,
                path="signal.initial_state",
                message=f"unknown initial_state {signal.initial_state.value!r}",
                context=UnknownIdentifierContext(identifier=signal.initial_state),
            )
        )

    transition_ids: dict[str, int] = {}
    timeout_ids: dict[str, int] = {}

    valid_reachability_edges: list[tuple[str, str]] = []
    priority_groups: dict[str, dict[int, list[StableIdentifier]]] = defaultdict(
        lambda: defaultdict(list)
    )

    has_valid_normal_emission = False
    has_valid_invalidation = False
    has_valid_timeout = False
    has_long_emission = False
    has_short_emission = False

    for index, transition in enumerate(signal.transitions):
        transition_key = transition.transition_id.value
        if transition_key in transition_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_DUPLICATE_TRANSITION_ID,
                    path=f"signal.transitions[{index}].transition_id",
                    message=(
                        f"duplicate transition_id {transition_key!r} "
                        f"(first at signal.transitions[{transition_ids[transition_key]}])"
                    ),
                    context=UnknownIdentifierContext(identifier=transition.transition_id),
                )
            )
        else:
            transition_ids[transition_key] = index

        from_known = transition.from_state.value in defined_states
        to_known = transition.to_state.value in defined_states

        if not from_known:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_UNKNOWN_FROM_STATE,
                    path=f"signal.transitions[{index}].from_state",
                    message=f"unknown from_state {transition.from_state.value!r}",
                    context=UnknownIdentifierContext(identifier=transition.from_state),
                )
            )
        if not to_known:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_UNKNOWN_TO_STATE,
                    path=f"signal.transitions[{index}].to_state",
                    message=f"unknown to_state {transition.to_state.value!r}",
                    context=UnknownIdentifierContext(identifier=transition.to_state),
                )
            )

        if (
            transition.purpose is TransitionPurpose.INVALIDATION
            and transition.emission is not None
        ):
            issues.append(
                make_error(
                    ValidationIssueCode.SM_INVALIDATION_WITH_EMISSION,
                    path=f"signal.transitions[{index}].emission",
                    message="invalidation transitions must not define emission",
                    context=None,
                )
            )

        emission_supported = True
        if transition.emission is not None:
            emission_supported = _validate_emission_side(
                issues,
                path=f"signal.transitions[{index}].emission.side",
                directionality=signal.directionality,
                emission=transition.emission,
            )

        if from_known and to_known:
            valid_reachability_edges.append(
                (transition.from_state.value, transition.to_state.value)
            )

            if from_known:
                priority_groups[transition.from_state.value][transition.priority].append(
                    transition.transition_id
                )

            if transition.purpose is TransitionPurpose.INVALIDATION:
                has_valid_invalidation = True

            if (
                transition.purpose is TransitionPurpose.NORMAL
                and transition.emission is not None
                and emission_supported
            ):
                has_valid_normal_emission = True
                if transition.emission.side is SideName.LONG:
                    has_long_emission = True
                if transition.emission.side is SideName.SHORT:
                    has_short_emission = True

    for index, timeout in enumerate(signal.timeouts):
        timeout_key = timeout.timeout_id.value
        if timeout_key in timeout_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_DUPLICATE_TIMEOUT_ID,
                    path=f"signal.timeouts[{index}].timeout_id",
                    message=(
                        f"duplicate timeout_id {timeout_key!r} "
                        f"(first at signal.timeouts[{timeout_ids[timeout_key]}])"
                    ),
                    context=UnknownIdentifierContext(identifier=timeout.timeout_id),
                )
            )
        else:
            timeout_ids[timeout_key] = index

        in_known = timeout.in_state.value in defined_states
        to_known = timeout.to_state.value in defined_states

        if not in_known:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_UNKNOWN_TIMEOUT_STATE,
                    path=f"signal.timeouts[{index}].in_state",
                    message=f"unknown in_state {timeout.in_state.value!r}",
                    context=UnknownIdentifierContext(identifier=timeout.in_state),
                )
            )
        if not to_known:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_UNKNOWN_TIMEOUT_TARGET,
                    path=f"signal.timeouts[{index}].to_state",
                    message=f"unknown to_state {timeout.to_state.value!r}",
                    context=UnknownIdentifierContext(identifier=timeout.to_state),
                )
            )

        if in_known and to_known:
            valid_reachability_edges.append(
                (timeout.in_state.value, timeout.to_state.value)
            )
            has_valid_timeout = True

        if in_known:
            priority_groups[timeout.in_state.value][timeout.priority].append(
                timeout.timeout_id
            )

    for state_key, priorities in sorted(priority_groups.items()):
        for priority, event_ids in sorted(priorities.items()):
            if len(event_ids) < 2:
                continue
            sorted_ids = tuple(sorted(event_ids, key=lambda item: item.value))
            issues.append(
                make_error(
                    ValidationIssueCode.SM_DUPLICATE_PRIORITY,
                    path=f"signal.states[{state_ids[state_key]}]",
                    message=(
                        f"duplicate priority {priority} in state {state_key!r} "
                        f"for events {[item.value for item in sorted_ids]}"
                    ),
                    context=StatePriorityContext(
                        state_id=StableIdentifier(value=state_key),
                        priority=priority,
                        event_ids=sorted_ids,
                    ),
                )
            )

    if initial_known:
        reachable = _reachable_states(
            signal.initial_state.value,
            valid_reachability_edges,
        )
        for state_key in sorted(defined_states):
            if state_key not in reachable:
                issues.append(
                    make_error(
                        ValidationIssueCode.SM_UNREACHABLE_STATE,
                        path=f"signal.states[{state_ids[state_key]}]",
                        message=f"state {state_key!r} is not reachable from initial_state",
                        context=UnknownIdentifierContext(
                            identifier=StableIdentifier(value=state_key)
                        ),
                    )
                )

    issues.extend(
        _validate_emission_coverage(
            directionality=signal.directionality,
            has_long_emission=has_long_emission,
            has_short_emission=has_short_emission,
        )
    )
    issues.extend(
        _validate_reset_rules(
            signal.reset_rules,
            defined_states=defined_states,
            has_valid_normal_emission=has_valid_normal_emission,
            has_valid_invalidation=has_valid_invalidation,
            has_valid_timeout=has_valid_timeout,
        )
    )

    return tuple(issues)


def _validate_emission_side(
    issues: list[ValidationIssue],
    *,
    path: str,
    directionality: Directionality,
    emission: SignalEmissionSpec,
) -> bool:
    if directionality is Directionality.LONG and emission.side is SideName.SHORT:
        issues.append(
            make_error(
                ValidationIssueCode.SM_EMISSION_SIDE_UNSUPPORTED,
                path=path,
                message="directionality LONG does not support short emission",
                context=None,
            )
        )
        return False
    if directionality is Directionality.SHORT and emission.side is SideName.LONG:
        issues.append(
            make_error(
                ValidationIssueCode.SM_EMISSION_SIDE_UNSUPPORTED,
                path=path,
                message="directionality SHORT does not support long emission",
                context=None,
            )
        )
        return False
    return True


def _validate_emission_coverage(
    *,
    directionality: Directionality,
    has_long_emission: bool,
    has_short_emission: bool,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if directionality in (Directionality.LONG, Directionality.BOTH) and not has_long_emission:
        issues.append(
            make_error(
                ValidationIssueCode.SM_LONG_EMISSION_MISSING,
                path="signal.directionality",
                message="directionality requires at least one valid long emission",
                context=None,
            )
        )
    if directionality in (Directionality.SHORT, Directionality.BOTH) and not has_short_emission:
        issues.append(
            make_error(
                ValidationIssueCode.SM_SHORT_EMISSION_MISSING,
                path="signal.directionality",
                message="directionality requires at least one valid short emission",
                context=None,
            )
        )
    return tuple(issues)


def _validate_reset_rules(
    reset_rules: tuple[ResetRule, ...],
    *,
    defined_states: set[str],
    has_valid_normal_emission: bool,
    has_valid_invalidation: bool,
    has_valid_timeout: bool,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    seen_events: dict[ResetEvent, int] = {}

    for index, rule in enumerate(reset_rules):
        if rule.event in seen_events:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_DUPLICATE_RESET_EVENT,
                    path=f"signal.reset_rules[{index}].event",
                    message=(
                        f"duplicate reset event {rule.event.value!r} "
                        f"(first at signal.reset_rules[{seen_events[rule.event]}])"
                    ),
                    context=None,
                )
            )
        else:
            seen_events[rule.event] = index

        target_known = rule.target_state.value in defined_states
        if not target_known:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_UNKNOWN_RESET_TARGET,
                    path=f"signal.reset_rules[{index}].target_state",
                    message=f"unknown reset target_state {rule.target_state.value!r}",
                    context=UnknownIdentifierContext(identifier=rule.target_state),
                )
            )

        source_exists = {
            ResetEvent.SIGNAL_EMITTED: has_valid_normal_emission,
            ResetEvent.INVALIDATED: has_valid_invalidation,
            ResetEvent.TIMEOUT: has_valid_timeout,
        }[rule.event]
        if not source_exists:
            issues.append(
                make_error(
                    ValidationIssueCode.SM_RESET_EVENT_WITHOUT_SOURCE,
                    path=f"signal.reset_rules[{index}].event",
                    message=(
                        f"reset event {rule.event.value!r} has no matching source "
                        "in the state graph"
                    ),
                    context=None,
                )
            )

    return tuple(issues)


def _reachable_states(
    initial: str,
    edges: list[tuple[str, str]],
) -> set[str]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for source, target in edges:
        adjacency[source].add(target)

    reachable: set[str] = set()
    stack = [initial]
    while stack:
        node = stack.pop()
        if node in reachable:
            continue
        reachable.add(node)
        for neighbor in sorted(adjacency.get(node, ())):
            if neighbor not in reachable:
                stack.append(neighbor)
    return reachable
