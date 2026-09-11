"""Regression: LF1 outcomes require aligned_path_analysis_v1 in the worktree package."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]
SRC = ENGINE_ROOT / "src"
sys.path.insert(0, str(SRC))
# Real LF1 also puts orderbook_analyse/src on PYTHONPATH for CH helpers.
sys.path.insert(1, "/home/telgenbuescher/projects/orderbook_analyse/src")


def test_aligned_path_package_is_real_directory_not_symlink():
    pkg = SRC / "obfull_research_engine" / "aligned_path_analysis_v1"
    assert pkg.is_dir()
    assert not pkg.is_symlink()
    assert (pkg / "__init__.py").is_file()
    assert (pkg / "metrics.py").is_file()
    assert (pkg / "prices.py").is_file()


def test_import_aligned_path_from_worktree_only():
    # Mimic the user-mandated worktree-first import without falling back to a
    # second obfull_research_engine source tree.
    import importlib

    mod = importlib.import_module("obfull_research_engine.aligned_path_analysis_v1")
    assert "orderbook_analyse_btc30m_v1" in Path(mod.__file__).resolve().parts


def test_directional_return_and_analyze_path_smoke():
    from obfull_research_engine.aligned_path_analysis_v1.metrics import (
        analyze_path,
        directional_return_pct,
    )

    assert abs(directional_return_pct(101.0, 100.0, "BULLISH") - 1.0) < 1e-12
    det = pd.Timestamp("2026-09-07T10:00:00Z")
    ts = np.array(
        [
            (det + pd.Timedelta(seconds=s)).to_datetime64()
            for s in (0.0, 5.0, 10.0)
        ]
    )
    rec = analyze_path(
        ts=ts,
        prices=np.array([100.0, 100.2, 100.1]),
        detection=det,
        direction="BULLISH",
        reference_price=100.0,
        reference_price_ts=det,
        price_source="TEST",
        path_resolution_ms=1000,
        horizon_s=10,
    )
    assert rec["peak_mfe_pct"] >= 0.2 - 1e-9


def _mini_trade_index():
    from obfull_research_engine.outcomes.public_trade_index import PublicTrade, PublicTradeIndex

    base = pd.Timestamp("2026-09-07T10:30:00Z")
    trades = []
    for i in range(0, 120):
        ts = base + pd.Timedelta(seconds=i)
        price = 100.0 + 0.01 * i
        size = 0.01
        trades.append(
            PublicTrade(
                trade_ts=ts,
                ingest_timestamp=ts,
                trade_id=str(i),
                price=price,
                side="Buy",
                size=size,
                notional=price * size,
            )
        )
    return PublicTradeIndex.from_trades(trades, raw_count=len(trades))


def test_measure_detection_outcomes_real_call_reaches_aligned_path():
    """Must invoke measure_detection_outcomes (not a mocked import)."""
    from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.outcomes import (
        measure_detection_outcomes,
    )

    trades = _mini_trade_index()
    detection = datetime(2026, 9, 7, 10, 30, 0, tzinfo=timezone.utc)
    outcome_end = datetime(2026, 9, 7, 11, 0, 0, tzinfo=timezone.utc)
    out = measure_detection_outcomes(
        detection=detection,
        direction="BULLISH",
        trades=trades,
        mid_index=None,
        outcome_end=outcome_end,
    )
    assert out["directional_hit_evaluated"] is True
    assert out.get("price_source") in {
        "FIRST_PUBLIC_TRADE_AT_OR_AFTER_DETECTION",
        "MID_1S_AT_DETECTION",
    }
    assert out.get("reference_price") == 100.0
    assert isinstance(out.get("horizons"), list)
    assert len(out["horizons"]) >= 1
    # At least one horizon path was computed via analyze_path.
    assert any("peak_mfe_pct" in h for h in out["horizons"] if isinstance(h, dict))


def test_lf1_outcomes_module_imports_aligned_path_symbols():
    import obfull_research_engine.bounded_level_first_analyzer_pilot_v1.outcomes as outcomes_mod

    src = Path(outcomes_mod.__file__).read_text()
    assert "aligned_path_analysis_v1.metrics" in src
    assert "aligned_path_analysis_v1.prices" in src
    assert "measure_detection_outcomes" in src


def test_export_builder_price_inputs_module_imports():
    from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.export_builder_price_inputs import (
        export_trades_and_candles_jsonl,
    )

    assert callable(export_trades_and_candles_jsonl)
