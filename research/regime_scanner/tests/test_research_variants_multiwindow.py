"""Tests for multi-window variant stability evaluation."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from research.regime_scanner.candle_sources import REGIME_ENV_FILE
from research.regime_scanner.research_runs.parameters import BASELINE_PARAMETER_HASH, build_baseline_parameter_set
from research.regime_scanner.research_variants.aggregate import (
    ROBUSTNESS_DEGENERATE_PENALTY,
    aggregate_variant_results,
    check_window_plausibility,
    compute_robustness_score,
)
from research.regime_scanner.research_variants.runner import rank_variants
from research.regime_scanner.research_variants.sets import SIMPLE_REGIME_STABILITY_V1, get_variant_set
from research.regime_scanner.research_variants.window_runner import PILOT_VARIANTS, _select_variants
from research.regime_scanner.research_variants.window_sets import REGIME_MARKET_WINDOWS_V1, get_window_set
from research.regime_scanner.research_variants.windows import (
    CANONICAL_WARMUP_START,
    MAX_ANALYSIS_END,
    ResearchWindow,
    window_hash,
    window_set_hash,
)
from research.regime_scanner.timeframes import ensure_utc_timestamp


def test_window_names_unique() -> None:
    names = [w.name for w in REGIME_MARKET_WINDOWS_V1.windows]
    assert len(names) == len(set(names))


def test_window_times_utc_and_ordered() -> None:
    max_end = ensure_utc_timestamp(MAX_ANALYSIS_END)
    for w in REGIME_MARKET_WINDOWS_V1.windows:
        assert w.warmup_start.tzinfo is not None
        assert w.start_time.tzinfo is not None
        assert w.end_time.tzinfo is not None
        assert w.start_time < w.end_time
        assert ensure_utc_timestamp(w.end_time) <= max_end


def test_window_hash_deterministic() -> None:
    w = REGIME_MARKET_WINDOWS_V1.windows[0]
    assert window_hash(w) == window_hash(w)


def test_window_set_hash_deterministic() -> None:
    h1 = window_set_hash(REGIME_MARKET_WINDOWS_V1)
    h2 = window_set_hash(get_window_set("regime_market_windows_v1"))
    assert h1 == h2
    assert len(h1) == 64


def test_runtime_fields_do_not_affect_window_hash() -> None:
    w = REGIME_MARKET_WINDOWS_V1.windows[0]
    canonical = w.to_canonical_dict()
    assert "run_id" not in canonical
    assert "runtime_seconds" not in canonical
    assert "created_at" not in canonical


def test_variant_set_unchanged() -> None:
    assert len(SIMPLE_REGIME_STABILITY_V1.variants) == 5
    baseline = next(v for v in SIMPLE_REGIME_STABILITY_V1.variants if v.name == "baseline")
    assert baseline.parameter_overrides == {}


def test_baseline_parameter_hash_unchanged() -> None:
    params = build_baseline_parameter_set(data_source="mysql")
    assert params.to_canonical_dict()  # smoke
    from research.regime_scanner.research_runs.parameters import parameter_hash

    assert parameter_hash(params) == BASELINE_PARAMETER_HASH


def test_march_week_in_window_set() -> None:
    names = {w.name for w in REGIME_MARKET_WINDOWS_V1.windows}
    assert "transition_march_week" in names


def test_pilot_mode_selects_two_variants() -> None:
    pilot = _select_variants(SIMPLE_REGIME_STABILITY_V1, pilot=True)
    assert {v.name for v in pilot} == PILOT_VARIANTS


def test_full_mode_selects_five_variants() -> None:
    full = _select_variants(SIMPLE_REGIME_STABILITY_V1, pilot=False)
    assert len(full) == 5


def test_ranking_ties_deterministic() -> None:
    rows = [
        {"variant_name": "b", "score": 1.0, "stability_metrics": {"degenerate": False}},
        {"variant_name": "a", "score": 1.0, "stability_metrics": {"degenerate": False}},
    ]
    assert rank_variants(rows) == [("a", 1), ("b", 2)]


def test_aggregate_score_metrics() -> None:
    rows = [
        {"score": 1.0, "rank": 1, "degenerate": False, "expected_character": "uptrend"},
        {"score": 3.0, "rank": 2, "degenerate": False, "expected_character": "range"},
    ]
    agg = aggregate_variant_results(rows)
    assert agg["mean_score"] == 2.0
    assert agg["median_score"] == 2.0
    assert agg["min_score"] == 1.0
    assert agg["max_score"] == 3.0


def test_robustness_score_formula() -> None:
    scores = [1.0, 2.0, 3.0]
    expected = compute_robustness_score(scores=scores, degenerate_count=0)
    assert expected == compute_robustness_score(scores=scores, degenerate_count=0)
    with_degen = compute_robustness_score(scores=scores, degenerate_count=1)
    assert with_degen == pytest.approx(expected - ROBUSTNESS_DEGENERATE_PENALTY)


def test_degenerate_penalty_in_aggregate() -> None:
    rows = [
        {"score": 0.0, "rank": 1, "degenerate": True, "expected_character": "mixed"},
    ]
    agg = aggregate_variant_results(rows)
    assert agg["degenerate_window_count"] == 1


def test_window_character_plausibility_uptrend() -> None:
    metrics = {
        "degenerate": False,
        "bars_total": 100.0,
        "uptrend_bars": 60.0,
        "downtrend_bars": 10.0,
        "range_bars": 10.0,
        "transition_bars": 20.0,
        "unknown_bars": 0.0,
    }
    result = check_window_plausibility(expected_character="uptrend", metrics=metrics)
    assert result["plausible"] is True
    assert result["warnings"] == []


def test_unsuitable_window_detected() -> None:
    metrics = {
        "degenerate": True,
        "bars_total": 100.0,
        "uptrend_bars": 0.0,
        "downtrend_bars": 0.0,
        "range_bars": 0.0,
        "transition_bars": 100.0,
        "unknown_bars": 0.0,
    }
    result = check_window_plausibility(expected_character="uptrend", metrics=metrics)
    assert result["plausible"] is False


def test_transition_heavy_uptrend_is_warning_not_hard_fail() -> None:
    metrics = {
        "degenerate": False,
        "bars_total": 100.0,
        "uptrend_bars": 0.0,
        "downtrend_bars": 0.0,
        "range_bars": 0.0,
        "transition_bars": 100.0,
        "unknown_bars": 0.0,
    }
    result = check_window_plausibility(expected_character="uptrend", metrics=metrics)
    assert result["plausible"] is True
    assert "mostly_transition_not_clear_uptrend" in result["warnings"]
    assert result["scanner_price_mismatch"] is True


def test_window_set_serialization_no_secrets() -> None:
    payload = json.dumps(REGIME_MARKET_WINDOWS_V1.to_canonical_dict())
    assert "password" not in payload.lower()
    assert "secret" not in payload.lower()


@pytest.mark.skipif(not REGIME_ENV_FILE.exists(), reason="regime DB env not configured")
def test_march_week_baseline_scores_reproduced() -> None:
    from research.regime_scanner.candle_sources import load_regime_db_env_file
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
    from research.regime_scanner.research_runs.store_mysql import MySQLResearchStore
    from research.regime_scanner.research_variants.stability import compute_stability_metrics
    from research.regime_scanner.research_variants.store_mysql import MySQLVariantStore

    load_regime_db_env_file()
    config = load_regime_db_config()
    research = MySQLResearchStore(config)
    variants = MySQLVariantStore(config)
    try:
        vs = variants.get_variant_set_by_name("simple_regime_stability_v1")
        assert vs is not None
        runs = variants.list_variant_runs(int(vs["id"]))
        baseline = next(r for r in runs if r["variant_name"] == "baseline")
        run_id = str(baseline["run_id"])
        trend = research.load_trend_states(run_id)
        structure = research.load_structure_events(run_id)
        stability = compute_stability_metrics(trend_states=trend, structure_events=structure)
        assert float(stability["score"]) == pytest.approx(-5.56851, rel=1e-4)
        slower = next(r for r in runs if r["variant_name"] == "slower_confirmation")
        assert float(slower["score"]) == pytest.approx(-5.52883, rel=1e-4)
    finally:
        research.close()
        variants.close()


@pytest.mark.skipif(not REGIME_ENV_FILE.exists(), reason="regime DB env not configured")
def test_schema_init_includes_window_tables() -> None:
    from research.regime_scanner.candle_sources import load_regime_db_env_file
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
    from research.regime_scanner.research_runs.store_mysql import MySQLResearchStore
    from research.regime_scanner.research_variants.store_mysql import MySQLVariantStore

    load_regime_db_env_file()
    config = load_regime_db_config()
    research = MySQLResearchStore(config)
    variants = MySQLVariantStore(config)
    try:
        research.init_schema()
        variants.init_schema()
        candles_before = research.count_candles()
        validation_before = research.count_validation_runs()
        assert candles_before > 0
        assert validation_before >= 0
    finally:
        research.close()
        variants.close()
