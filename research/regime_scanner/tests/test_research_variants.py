"""Tests for controlled variant runner."""

from __future__ import annotations

import json

import pytest

from research.regime_scanner.candle_sources import REGIME_ENV_FILE
from research.regime_scanner.research_runs.parameters import (
    BASELINE_PARAMETER_HASH,
    apply_parameter_overrides,
    assert_baseline_parameter_hash,
    build_baseline_parameter_set,
    parameter_hash,
)
from research.regime_scanner.research_variants.model import (
    ResearchVariant,
    variant_hash,
    variant_set_hash,
)
from research.regime_scanner.research_variants.runner import (
    compute_baseline_deltas,
    rank_variants,
)
from research.regime_scanner.research_variants.sets import SIMPLE_REGIME_STABILITY_V1, get_variant_set
from research.regime_scanner.research_variants.stability import (
    SHORT_RUN_THRESHOLD_BARS,
    compute_stability_metrics,
    compute_stability_score,
)
from research.regime_scanner.research_variants.version import RUNNER_VERSION


def _trend_row(ts: str, state: str, prev: str | None = None, bias: str | None = None):
    return {
        "timestamp": ts,
        "state": state,
        "previous_state": prev,
        "metadata_json": {"structure_5m": {"bias": bias}},
    }


def test_variant_names_unique() -> None:
    names = [v.name for v in SIMPLE_REGIME_STABILITY_V1.variants]
    assert len(names) == len(set(names))


def test_baseline_has_no_overrides() -> None:
    baseline = next(v for v in SIMPLE_REGIME_STABILITY_V1.variants if v.name == "baseline")
    assert baseline.parameter_overrides == {}


def test_baseline_parameter_hash_matches_reference() -> None:
    params = build_baseline_parameter_set(data_source="mysql")
    assert parameter_hash(params) == BASELINE_PARAMETER_HASH
    assert_baseline_parameter_hash(params)


def test_unknown_override_path_fails() -> None:
    base = build_baseline_parameter_set()
    with pytest.raises(ValueError, match="not allowed"):
        apply_parameter_overrides(base, {"scanner_name": "x"})


def test_variant_hash_deterministic() -> None:
    v = ResearchVariant(name="x", description="d", parameter_overrides={"trend_state.adx_confirm": 20.0})
    base = build_baseline_parameter_set()
    params = apply_parameter_overrides(base, v.parameter_overrides)
    ph = parameter_hash(params)
    h1 = variant_hash(v, resulting_parameter_hash=ph)
    h2 = variant_hash(v, resulting_parameter_hash=ph)
    assert h1 == h2
    assert RUNNER_VERSION in json.dumps({"runner_version": RUNNER_VERSION})


def test_baseline_config_not_mutated() -> None:
    base = build_baseline_parameter_set()
    before = parameter_hash(base)
    v = next(v for v in SIMPLE_REGIME_STABILITY_V1.variants if v.name == "faster_confirmation")
    updated = apply_parameter_overrides(base, v.parameter_overrides)
    assert parameter_hash(base) == before
    assert parameter_hash(updated) != before


def test_state_change_count() -> None:
    rows = [
        _trend_row("t1", "strong_bullish", bias="bullish"),
        _trend_row("t2", "strong_bullish", "strong_bullish", "bullish"),
        _trend_row("t3", "bullish_weakening", "strong_bullish", "bullish"),
    ]
    m = compute_stability_metrics(trend_states=rows, structure_events=[])
    assert m["state_change_count"] == 1.0
    assert m["bars_total"] == 3.0


def test_short_state_run_detection() -> None:
    rows = [
        _trend_row("t1", "strong_bullish", bias="bullish"),
        _trend_row("t2", "bullish_weakening", "strong_bullish", "bullish"),
        _trend_row("t3", "strong_bullish", "bullish_weakening", "bullish"),
    ]
    m = compute_stability_metrics(trend_states=rows, structure_events=[])
    assert m["short_state_run_count"] >= 1.0
    assert SHORT_RUN_THRESHOLD_BARS == 3


def test_degenerate_detection() -> None:
    rows = [_trend_row(f"t{i}", "neutral") for i in range(20)]
    m = compute_stability_metrics(trend_states=rows, structure_events=[])
    assert m["degenerate"] is True
    assert compute_stability_score(m) < 0


def test_score_reproducible() -> None:
    rows = [
        _trend_row("t1", "strong_bullish", bias="bullish"),
        _trend_row("t2", "strong_bullish", "strong_bullish", "bullish"),
        _trend_row("t3", "strong_bullish", "strong_bullish", "bullish"),
        _trend_row("t4", "bullish_weakening", "strong_bullish", "bullish"),
        _trend_row("t5", "early_bearish", "bullish_weakening", "bearish"),
    ]
    m = compute_stability_metrics(trend_states=rows, structure_events=[])
    s1 = compute_stability_score(m)
    s2 = compute_stability_score(m)
    assert s1 == s2


def test_ranking_stable_ties() -> None:
    rows = [
        {"variant_name": "b", "score": 10.0, "stability_metrics": {"degenerate": False}},
        {"variant_name": "a", "score": 10.0, "stability_metrics": {"degenerate": False}},
        {"variant_name": "c", "score": 5.0, "stability_metrics": {"degenerate": False}},
    ]
    ranked = rank_variants(rows)
    assert ranked[0][0] == "a"
    assert ranked[1][0] == "b"


def test_compare_baseline_deltas() -> None:
    base = {"state_change_count": 10.0, "score": 20.0, "transition_share": 0.1}
    var = {"state_change_count": 8.0, "score": 22.0, "transition_share": 0.12}
    deltas = compute_baseline_deltas(base, var)
    assert deltas["delta_state_change_count"] == -2.0
    assert deltas["delta_score"] == 2.0


def test_variant_set_hash_deterministic() -> None:
    assert variant_set_hash(SIMPLE_REGIME_STABILITY_V1) == variant_set_hash(
        get_variant_set("simple_regime_stability_v1")
    )


@pytest.mark.skipif(not REGIME_ENV_FILE.exists(), reason="regime DB env file not present")
def test_mysql_variant_schema_init() -> None:
    from research.regime_scanner.candle_sources import load_regime_db_env_file
    from research.regime_scanner.mysql_candle_store.config import load_regime_db_config
    from research.regime_scanner.research_variants.store_mysql import MySQLVariantStore

    load_regime_db_env_file()
    store = MySQLVariantStore(load_regime_db_config())
    try:
        store.init_schema()
        store.init_schema()
    finally:
        store.close()
