"""P4B determinism tests."""

from __future__ import annotations

import dataclasses

from orderbook_analyse.strategy_lab.models import (
    ComponentReference,
    RuleComponentSpec,
    StateSpec,
)
from orderbook_analyse.strategy_lab.validation import validate_strategy_v2_p4b
from tests.strategy_lab.validation.conftest import (
    catalogs,
    sid,
    valid_rule_based_long_strategy,
    valid_state_machine_long_strategy,
)
from tests.strategy_lab.v2_fixtures import state_machine_signal_v2


def test_repeated_validation_identical_report(catalogs) -> None:
    spec = valid_state_machine_long_strategy()
    first = validate_strategy_v2_p4b(spec, catalogs)
    second = validate_strategy_v2_p4b(spec, catalogs)
    assert first == second


def test_swapped_component_order_same_cycle_diagnosis(catalogs) -> None:
    components_a = (
        RuleComponentSpec(
            component_id=sid("a"),
            description="a",
            root=ComponentReference(component_id=sid("b")),
        ),
        RuleComponentSpec(
            component_id=sid("b"),
            description="b",
            root=ComponentReference(component_id=sid("a")),
        ),
    )
    components_b = (components_a[1], components_a[0])
    base = valid_rule_based_long_strategy()
    spec_a = dataclasses.replace(
        base,
        signal=dataclasses.replace(base.signal, components=components_a),
    )
    spec_b = dataclasses.replace(
        base,
        signal=dataclasses.replace(base.signal, components=components_b),
    )
    report_a = validate_strategy_v2_p4b(spec_a, catalogs)
    report_b = validate_strategy_v2_p4b(spec_b, catalogs)
    cycles_a = [issue for issue in report_a.issues if issue.code.value == "COMPONENT_CYCLE"]
    cycles_b = [issue for issue in report_b.issues if issue.code.value == "COMPONENT_CYCLE"]
    assert len(cycles_a) == 1
    assert len(cycles_b) == 1
    assert cycles_a[0].context == cycles_b[0].context


def test_swapped_state_order_same_semantic_issue_codes(catalogs) -> None:
    sm = state_machine_signal_v2()
    orphan = StateSpec(state_id=sid("orphan"), description="orphan")
    signal_fwd = dataclasses.replace(sm, states=sm.states + (orphan,))
    signal_rev = dataclasses.replace(
        sm,
        states=(orphan, sm.states[0], sm.states[1]),
    )
    spec_fwd = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal_fwd)
    spec_rev = dataclasses.replace(valid_state_machine_long_strategy(), signal=signal_rev)
    report_fwd = validate_strategy_v2_p4b(spec_fwd, catalogs)
    report_rev = validate_strategy_v2_p4b(spec_rev, catalogs)
    assert {issue.code for issue in report_fwd.issues} == {
        issue.code for issue in report_rev.issues
    }
