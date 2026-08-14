"""Dashboard signal source switch — frozen vs research 1m (no live deps)."""

from __future__ import annotations

import pytest

from stoch_signal_source import (
    SOURCE_FROZEN_BASELINE,
    SOURCE_RESEARCH_1M_TIMING,
    assert_sources_do_not_mix,
    frozen_upstream_path,
    get_dashboard_signal_source,
    get_default_research_display_variant,
    normalize_dashboard_signal_source,
    normalize_research_display_variant,
    research_upstream_path,
)


def test_dashboard_frozen_baseline_mode():
    src = get_dashboard_signal_source({"DASHBOARD_SIGNAL_SOURCE": "FROZEN_BASELINE"})
    assert src == SOURCE_FROZEN_BASELINE
    assert frozen_upstream_path() == "/api/signals"
    assert normalize_dashboard_signal_source("BASELINE") == SOURCE_FROZEN_BASELINE
    # Default (missing env) is frozen baseline — not research.
    assert get_dashboard_signal_source({}) == SOURCE_FROZEN_BASELINE
    # Research variant env must not flip the source.
    assert (
        get_dashboard_signal_source(
            {"DEFAULT_RESEARCH_DISPLAY_VARIANT": "WAIT_1M_EXTREME"}
        )
        == SOURCE_FROZEN_BASELINE
    )


def test_dashboard_research_1m_mode():
    src = get_dashboard_signal_source({"DASHBOARD_SIGNAL_SOURCE": "RESEARCH_1M_TIMING"})
    assert src == SOURCE_RESEARCH_1M_TIMING
    assert research_upstream_path() == "/api/research/1m_timing_signals"
    assert get_default_research_display_variant({}) == "WAIT_1M_EXTREME_TURN_CROSS"
    assert normalize_research_display_variant("CROSS") == "WAIT_1M_EXTREME_TURN_CROSS"
    assert normalize_research_display_variant("Extreme") == "WAIT_1M_EXTREME"


def test_dashboard_sources_do_not_mix():
    research_rows = [
        {
            "signal_id": "research1m:WAIT_1M_EXTREME_TURN_CROSS:abc",
            "research_mode": True,
            "timing_variant": "WAIT_1M_EXTREME_TURN_CROSS",
            "1m_trigger_state": "WAITING_FOR_1M_EXTREME",
            "feed_source": "RESEARCH_1M_TIMING",
        }
    ]
    assert_sources_do_not_mix(SOURCE_RESEARCH_1M_TIMING, research_rows)

    leaked = [
        {
            "signal_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            "feed_source": "signal_generator.signals",
        }
    ]
    with pytest.raises(AssertionError):
        assert_sources_do_not_mix(SOURCE_RESEARCH_1M_TIMING, leaked)

    # Frozen mode accepts production rows…
    assert_sources_do_not_mix(SOURCE_FROZEN_BASELINE, leaked)
    # …and rejects research leakage.
    with pytest.raises(AssertionError):
        assert_sources_do_not_mix(SOURCE_FROZEN_BASELINE, research_rows)


def test_research_mode_never_uses_production_path():
    assert research_upstream_path() != frozen_upstream_path()
