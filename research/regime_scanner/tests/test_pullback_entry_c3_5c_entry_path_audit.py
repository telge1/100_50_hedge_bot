"""Tests for C3.5c entry-only path audit."""

from __future__ import annotations

import inspect

from research.regime_scanner.pullback_entry_c3_5c_entry_path_audit import (
    DEFAULT_OUT,
    document_pine_script_identity,
    horizon_bars_for_tf,
)


def test_pine_identity_points_to_c35c() -> None:
    doc = document_pine_script_identity()
    assert doc["visible_chart_title"] == "C3.5c Pullback Entry Diagnose"
    assert doc["measures_majorDir_background"] is False
    existing = [r for r in doc["matching_artifacts"] if r.get("exists")]
    assert existing
    assert any(r.get("indicator_title") == "C3.5c Pullback Entry Diagnose" for r in existing)
    assert any(r.get("has_majorDir_bgcolor") is False for r in existing)


def test_output_dir_not_simple_path() -> None:
    assert "c35c_entry_path_audit" in str(DEFAULT_OUT)
    assert "simple_path_audit" not in str(DEFAULT_OUT)


def test_horizon_4h_48h() -> None:
    bars, actual = horizon_bars_for_tf("4h", 48)
    assert bars == 12
    assert actual == 48.0


def test_no_lookahead_in_source() -> None:
    import research.regime_scanner.pullback_entry_c3_5c_entry_path_audit as mod

    src = inspect.getsource(mod)
    assert "lookahead_on" not in src
    assert "shift(-" not in src
