"""P4B rule-based directionality validation tests."""

from __future__ import annotations

import dataclasses

from orderbook_analyse.strategy_lab.models import SideName, SideRuleBundle
from orderbook_analyse.strategy_lab.models.enums import Directionality
from orderbook_analyse.strategy_lab.validation import (
    ValidationIssueCode,
    validate_strategy_v2_p4b,
)
from tests.strategy_lab.validation.conftest import catalogs, valid_rule_based_long_strategy
from tests.strategy_lab.v2_fixtures import _comparison, rule_based_signal_v2


def test_valid_long(catalogs) -> None:
    assert validate_strategy_v2_p4b(valid_rule_based_long_strategy(), catalogs).is_valid


def test_valid_short(catalogs) -> None:
    spec = dataclasses.replace(
        valid_rule_based_long_strategy(),
        signal=rule_based_signal_v2(Directionality.SHORT),
    )
    assert validate_strategy_v2_p4b(spec, catalogs).is_valid


def test_valid_both(catalogs) -> None:
    spec = dataclasses.replace(
        valid_rule_based_long_strategy(),
        signal=rule_based_signal_v2(Directionality.BOTH),
    )
    assert validate_strategy_v2_p4b(spec, catalogs).is_valid


def test_missing_long_bundle(catalogs) -> None:
    signal = dataclasses.replace(
        rule_based_signal_v2(Directionality.LONG),
        long=None,
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.RULE_LONG_BUNDLE_MISSING in {i.code for i in report.issues}


def test_unexpected_long_bundle(catalogs) -> None:
    signal = rule_based_signal_v2(Directionality.SHORT)
    signal = dataclasses.replace(
        signal,
        long=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=_comparison(),
            confirmation=None,
            invalidation=None,
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.RULE_LONG_BUNDLE_UNEXPECTED in {i.code for i in report.issues}


def test_missing_short_bundle(catalogs) -> None:
    signal = dataclasses.replace(
        rule_based_signal_v2(Directionality.BOTH),
        short=None,
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.RULE_SHORT_BUNDLE_MISSING in {i.code for i in report.issues}


def test_unexpected_short_bundle(catalogs) -> None:
    signal = rule_based_signal_v2(Directionality.LONG)
    signal = dataclasses.replace(
        signal,
        short=SideRuleBundle(
            side=SideName.SHORT,
            setup=None,
            trigger=_comparison(),
            confirmation=None,
            invalidation=None,
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.RULE_SHORT_BUNDLE_UNEXPECTED in {i.code for i in report.issues}


def test_long_side_mismatch(catalogs) -> None:
    signal = dataclasses.replace(
        rule_based_signal_v2(Directionality.LONG),
        long=SideRuleBundle(
            side=SideName.SHORT,
            setup=None,
            trigger=_comparison(),
            confirmation=None,
            invalidation=None,
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.RULE_LONG_SIDE_MISMATCH in {i.code for i in report.issues}


def test_short_side_mismatch(catalogs) -> None:
    signal = dataclasses.replace(
        rule_based_signal_v2(Directionality.SHORT),
        short=SideRuleBundle(
            side=SideName.LONG,
            setup=None,
            trigger=_comparison(),
            confirmation=None,
            invalidation=None,
        ),
    )
    spec = dataclasses.replace(valid_rule_based_long_strategy(), signal=signal)
    report = validate_strategy_v2_p4b(spec, catalogs)
    assert ValidationIssueCode.RULE_SHORT_SIDE_MISMATCH in {i.code for i in report.issues}
