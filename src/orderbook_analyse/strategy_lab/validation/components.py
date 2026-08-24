"""Local component reference and cycle validation for P4B."""

from __future__ import annotations

from orderbook_analyse.strategy_lab.models.identifiers import StableIdentifier
from orderbook_analyse.strategy_lab.models.rules import (
    BooleanAndExpression,
    BooleanExpression,
    BooleanNotExpression,
    BooleanOrExpression,
    ComponentReference,
    RuleComponentSpec,
)
from orderbook_analyse.strategy_lab.models.signals import (
    RuleBasedSignalSpec,
    SideRuleBundle,
    StateMachineSignalSpec,
)
from orderbook_analyse.strategy_lab.models.state_machine import TransitionSpec
from orderbook_analyse.strategy_lab.validation._issue_helpers import make_error
from orderbook_analyse.strategy_lab.validation.diagnostics import (
    ComponentCycleContext,
    UnknownIdentifierContext,
)
from orderbook_analyse.strategy_lab.validation.models import (
    ValidationIssue,
    ValidationIssueCode,
)


def validate_rule_based_components(
    signal: RuleBasedSignalSpec,
) -> tuple[ValidationIssue, ...]:
    return _validate_components(
        components=signal.components,
        reference_sources=_rule_based_reference_sources(signal),
    )


def validate_state_machine_components(
    signal: StateMachineSignalSpec,
) -> tuple[ValidationIssue, ...]:
    return _validate_components(
        components=signal.components,
        reference_sources=_state_machine_reference_sources(signal),
    )


def _validate_components(
    *,
    components: tuple[RuleComponentSpec, ...],
    reference_sources: tuple[tuple[str, ComponentReference], ...],
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    known_ids: dict[str, int] = {}
    id_to_component: dict[str, StableIdentifier] = {}

    for index, component in enumerate(components):
        component_key = component.component_id.value
        id_to_component[component_key] = component.component_id
        if component_key in known_ids:
            issues.append(
                make_error(
                    ValidationIssueCode.COMPONENT_DUPLICATE_ID,
                    path=f"signal.components[{index}].component_id",
                    message=(
                        f"duplicate component_id {component_key!r} "
                        f"(first at signal.components[{known_ids[component_key]}])"
                    ),
                    context=UnknownIdentifierContext(identifier=component.component_id),
                )
            )
        else:
            known_ids[component_key] = index

    known_set = set(known_ids)
    for path, reference in reference_sources:
        ref_key = reference.component_id.value
        if ref_key not in known_set:
            issues.append(
                make_error(
                    ValidationIssueCode.COMPONENT_UNKNOWN_REFERENCE,
                    path=f"{path}.component_id",
                    message=f"unknown component reference {ref_key!r}",
                    context=UnknownIdentifierContext(identifier=reference.component_id),
                )
            )

    issues.extend(_detect_cycles(components, known_set))
    return tuple(issues)


def _detect_cycles(
    components: tuple[RuleComponentSpec, ...],
    known_ids: set[str],
) -> tuple[ValidationIssue, ...]:
    graph: dict[str, set[str]] = {cid: set() for cid in known_ids}
    index_by_id: dict[str, int] = {}

    for index, component in enumerate(components):
        component_key = component.component_id.value
        if component_key not in known_ids:
            continue
        index_by_id[component_key] = index
        for ref in _refs_in_expression(component.root):
            if ref in known_ids:
                graph[component_key].add(ref)

    sccs = _tarjan_scc(graph)
    issues: list[ValidationIssue] = []
    reported: set[tuple[str, ...]] = set()

    for scc in sccs:
        if len(scc) == 1:
            node = scc[0]
            if node not in graph.get(node, set()):
                continue
            canonical = (node,)
        else:
            cycle = _extract_cycle_from_scc(graph, scc)
            if cycle is None:
                continue
            canonical = _canonical_cycle_path(cycle)

        if canonical in reported:
            continue
        reported.add(canonical)

        start_id = min(canonical)
        component_index = index_by_id[start_id]
        cycle_ids = tuple(
            StableIdentifier(value=node_id) for node_id in _closed_cycle(canonical)
        )
        issues.append(
            make_error(
                ValidationIssueCode.COMPONENT_CYCLE,
                path=f"signal.components[{component_index}]",
                message=f"component cycle detected: {' -> '.join(cycle_ids[i].value for i in range(len(cycle_ids)))}",
                context=ComponentCycleContext(cycle_path=cycle_ids),
            )
        )

    return tuple(issues)


def _closed_cycle(path: tuple[str, ...]) -> tuple[str, ...]:
    if len(path) == 1:
        return (path[0], path[0])
    return path + (path[0],)


def _canonical_cycle_path(nodes: tuple[str, ...]) -> tuple[str, ...]:
    if len(nodes) == 1:
        return nodes
    start = min(nodes)
    start_index = nodes.index(start)
    rotated = nodes[start_index:] + nodes[:start_index]
    return tuple(rotated)


def _extract_cycle_from_scc(
    graph: dict[str, set[str]],
    scc: list[str],
) -> tuple[str, ...] | None:
    scc_set = set(scc)
    start = min(scc)
    stack = [start]
    parent: dict[str, str | None] = {start: None}

    while stack:
        node = stack[-1]
        expanded = False
        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in scc_set:
                continue
            if neighbor not in parent:
                parent[neighbor] = node
                stack.append(neighbor)
                expanded = True
                break
            if neighbor == start and len(stack) > 1:
                cycle: list[str] = []
                current: str | None = node
                while current is not None and current != start:
                    cycle.append(current)
                    current = parent.get(current)
                cycle.append(start)
                cycle.reverse()
                return _canonical_cycle_path(tuple(cycle))
        if not expanded:
            stack.pop()

    if start in graph.get(start, set()):
        return (start,)
    return None


def _tarjan_scc(graph: dict[str, set[str]]) -> list[list[str]]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    sccs: list[list[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in sorted(graph.get(node, ())):
            if neighbor not in indices:
                strongconnect(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                component.append(w)
                if w == node:
                    break
            sccs.append(sorted(component))

    for node in sorted(graph):
        if node not in indices:
            strongconnect(node)

    return sccs


def _rule_based_reference_sources(
    signal: RuleBasedSignalSpec,
) -> tuple[tuple[str, ComponentReference], ...]:
    sources: list[tuple[str, ComponentReference]] = []
    if signal.long is not None:
        sources.extend(_bundle_reference_sources("signal.long", signal.long))
    if signal.short is not None:
        sources.extend(_bundle_reference_sources("signal.short", signal.short))
    for index, component in enumerate(signal.components):
        sources.extend(
            _expression_reference_sources(
                f"signal.components[{index}].root",
                component.root,
            )
        )
    return tuple(sources)


def _state_machine_reference_sources(
    signal: StateMachineSignalSpec,
) -> tuple[tuple[str, ComponentReference], ...]:
    sources: list[tuple[str, ComponentReference]] = []
    for index, transition in enumerate(signal.transitions):
        sources.extend(
            _expression_reference_sources(
                f"signal.transitions[{index}].condition",
                transition.condition,
            )
        )
    for index, component in enumerate(signal.components):
        sources.extend(
            _expression_reference_sources(
                f"signal.components[{index}].root",
                component.root,
            )
        )
    return tuple(sources)


def _bundle_reference_sources(
    prefix: str,
    bundle: SideRuleBundle,
) -> list[tuple[str, ComponentReference]]:
    sources: list[tuple[str, ComponentReference]] = []
    if bundle.setup is not None:
        sources.extend(_expression_reference_sources(f"{prefix}.setup", bundle.setup))
    sources.extend(_expression_reference_sources(f"{prefix}.trigger", bundle.trigger))
    if bundle.confirmation is not None:
        sources.extend(
            _expression_reference_sources(f"{prefix}.confirmation", bundle.confirmation)
        )
    if bundle.invalidation is not None:
        sources.extend(
            _expression_reference_sources(f"{prefix}.invalidation", bundle.invalidation)
        )
    return sources


def _expression_reference_sources(
    path: str,
    expression: BooleanExpression,
) -> list[tuple[str, ComponentReference]]:
    if isinstance(expression, ComponentReference):
        return [(path, expression)]
    if isinstance(expression, BooleanAndExpression):
        refs: list[tuple[str, ComponentReference]] = []
        for operand_index, operand in enumerate(expression.operands):
            refs.extend(
                _expression_reference_sources(
                    f"{path}.operands[{operand_index}]",
                    operand,
                )
            )
        return refs
    if isinstance(expression, BooleanOrExpression):
        refs = []
        for operand_index, operand in enumerate(expression.operands):
            refs.extend(
                _expression_reference_sources(
                    f"{path}.operands[{operand_index}]",
                    operand,
                )
            )
        return refs
    if isinstance(expression, BooleanNotExpression):
        return _expression_reference_sources(f"{path}.operand", expression.operand)
    return []


def _refs_in_expression(expression: BooleanExpression) -> set[str]:
    return {reference.component_id.value for _, reference in _expression_reference_sources("", expression)}
