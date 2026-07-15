"""Tests for Phase C2B2A neutral-fallback root-cause audit (read-only)."""

from __future__ import annotations

from pathlib import Path

import pytest

from research.regime_scanner.trend_neutral_fallback_root_cause_audit import (
    AGE_CANDIDATES,
    LONG_MIN,
    assert_safe_output_dir,
    recommended_c2b_stack_config,
)
from research.regime_scanner.trend_state_machine import default_trend_state_config


def test_recommended_stack_is_strict_strict_24() -> None:
    cfg = recommended_c2b_stack_config()
    assert cfg.weakening_multi_bar_mode == "strict"
    assert cfg.turning_multi_bar_mode == "strict"
    assert cfg.turning_evidence_window_bars == 24
    assert default_trend_state_config().turning_multi_bar_mode == "off"
    assert default_trend_state_config().weakening_multi_bar_mode == "off"


def test_refuse_overwrite_prior_result_dirs() -> None:
    with pytest.raises(ValueError):
        assert_safe_output_dir(
            Path("research/regime_scanner/results_trend_topping_bottoming_multibar_phase_c2b1")
        )
    with pytest.raises(ValueError):
        assert_safe_output_dir(Path("research/regime_scanner/results"))


def test_thresholds_and_long_min_constants() -> None:
    assert LONG_MIN == 96
    assert AGE_CANDIDATES == (24, 48, 96)


def test_audit_module_is_diagnostic_only() -> None:
    import inspect

    import research.regime_scanner.trend_neutral_fallback_root_cause_audit as m

    src = inspect.getsource(m)
    assert "Does **not** implement neutral transitions" in m.__doc__
    assert "step_trend_state" in src
    assert "turning_neutral_fallback_mode" in src
    # must not patch SM transition helpers
    assert "multi_bar_turning_exit =" not in src
