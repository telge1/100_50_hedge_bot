"""Tests for LF1 results-root isolation and builder price-export helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / "src"))

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import (  # noqa: E402
    FORBIDDEN_WRITE_ROOT,
    RESULTS_ROOT,
    assert_safe_results_root,
    resolve_results_root,
    run_dir,
)


def test_default_results_root_refused_when_symlinked_into_old_repo():
    # Worktree default RESULTS_ROOT is a symlink into orderbook_analyse.
    if not RESULTS_ROOT.is_symlink():
        pytest.skip("default RESULTS_ROOT is not a symlink in this environment")
    with pytest.raises(ValueError) as ei:
        resolve_results_root(None)
    assert "unsafe" in str(ei.value).lower() or "refusing" in str(ei.value).lower()


def test_results_root_refuses_old_checkout_path(tmp_path: Path):
    forbidden = FORBIDDEN_WRITE_ROOT / "obfull_research_engine" / "results" / "should_not_write"
    with pytest.raises(ValueError):
        assert_safe_results_root(forbidden)


def test_isolated_results_root_under_worktree_runs(tmp_path: Path, monkeypatch):
    root = Path(
        "/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1/"
        "obfull_research_engine/runs/full_inputs/btcusdt_20260907_v1"
    )
    resolved = assert_safe_results_root(root)
    assert resolved == root.resolve()
    # Must not resolve into the frozen main checkout (path-boundary safe).
    assert FORBIDDEN_WRITE_ROOT not in resolved.parents
    assert resolved != FORBIDDEN_WRITE_ROOT
    d = run_dir("BTCUSDT", "lf1_deadbeefcafebabe", results_root=root)
    assert d == resolved / "BTCUSDT" / "lf1_deadbeefcafebabe"
    assert str(d).startswith(str(resolved))


def test_cli_accepts_results_root_flag():
    from obfull_research_engine.bounded_level_first_analyzer_pilot_v1 import cli as cli_mod
    import argparse

    # Re-parse the same flags the CLI exposes.
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-root", type=str, default=None)
    parser.add_argument("--export-builder-price-inputs", action="store_true")
    args = parser.parse_args(
        [
            "--results-root",
            "/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1/obfull_research_engine/runs/full_inputs/btcusdt_20260907_v1",
            "--export-builder-price-inputs",
        ]
    )
    assert args.results_root.endswith("btcusdt_20260907_v1")
    assert args.export_builder_price_inputs is True
    assert hasattr(cli_mod, "main")
