"""Entry and warmup V2 contract tests."""

from __future__ import annotations

import dataclasses

from orderbook_analyse.strategy_lab.catalogs.v2 import get_plugin_v2
from orderbook_analyse.strategy_lab.models.contracts_v2 import (
    EntryPriceReferenceV2,
    EntryReferenceRuleV2,
    EntryTimingAnchorV2,
    OutcomeEvaluationPaddingV2,
    PaddingNotApplicable,
    SourceLoadingPaddingV2,
)
from tests.strategy_lab.v2_fixtures import _entry_v2, _warmup_v2, minimal_strategy_spec_v2


def test_entry_next_signal_tf_open() -> None:
    entry = _entry_v2()
    assert entry.entry_price_reference is EntryPriceReferenceV2.BAR_OPEN
    assert (
        entry.entry_reference_rule
        is EntryReferenceRuleV2.SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR
    )


def test_entry_has_no_minimum_causal_delay_field() -> None:
    names = {f.name for f in dataclasses.fields(type(_entry_v2()))}
    assert "minimum_causal_delay_bars" not in names


def test_execution_timeframe_lives_only_in_execution_assumptions_v2() -> None:
    entry = _entry_v2()
    assert "execution_timeframe" not in {f.name for f in dataclasses.fields(type(entry))}
    assert entry.entry_timing_anchor is EntryTimingAnchorV2.SIGNAL_TIMEFRAME_BAR_OPEN
    assert minimal_strategy_spec_v2().execution_assumptions.execution_timeframe.value == 1


def test_edc_and_cluster_entry_rules() -> None:
    edc = get_plugin_v2("edc_m0_strict_sync")
    cluster = get_plugin_v2("cluster_sweep")
    assert (
        edc.entry_reference_rule
        is EntryReferenceRuleV2.SIGNAL_TF_NEXT_OPEN_AFTER_SIGNAL_BAR
    )
    assert (
        cluster.entry_reference_rule
        is EntryReferenceRuleV2.NEXT_BAR_OPEN_AFTER_CONFIRMATION_BAR
    )
    assert edc.entry_timing_anchor is EntryTimingAnchorV2.SIGNAL_TIMEFRAME_BAR_OPEN
    assert cluster.entry_timing_anchor is EntryTimingAnchorV2.SIGNAL_TIMEFRAME_BAR_OPEN


def test_source_and_outcome_padding_separate_types() -> None:
    warmup = _warmup_v2()
    assert isinstance(warmup.source_loading, SourceLoadingPaddingV2)
    assert isinstance(warmup.outcome_evaluation, OutcomeEvaluationPaddingV2)
    assert not isinstance(warmup.source_loading, OutcomeEvaluationPaddingV2)


def test_cluster_padding_not_applicable_explicit() -> None:
    cluster = get_plugin_v2("cluster_sweep")
    assert cluster.source_loading_padding is not None
    assert isinstance(
        cluster.source_loading_padding.candle_history, PaddingNotApplicable
    )
    assert isinstance(
        cluster.outcome_evaluation_padding.post_window_duration,
        PaddingNotApplicable,
    )


def test_strategy_spec_v2_warmup_structure() -> None:
    spec = minimal_strategy_spec_v2()
    assert spec.warmup.signal_engine.minimum_bars == 79
    assert spec.warmup.signal_engine.bar_timeframe.value == 5
