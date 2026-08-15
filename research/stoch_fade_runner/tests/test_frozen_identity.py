from __future__ import annotations

from pathlib import Path

from research.stoch_fade_runner.config import STRATEGY_ID, assert_frozen_pin
from research.stoch_fade_runner.engine import evaluate_symbol
from research.stoch_fade_runner.identity import (
    BE50_OUTCOME_ACTIVE,
    BLOCKED_BY_FROZEN_STRATEGY_MISMATCH,
    CANDIDATE_LIVE_STRATEGY,
    EDGES_VERSION_PIN,
    FROZEN_SOURCE_SHA256,
    SIGNAL_TFS_PIN,
    frozen_identity,
)


def test_frozen_identity_imports_and_hashes() -> None:
    ident = frozen_identity()
    assert ident["strategy_id"] == "wave_fade_frozen_f16ae32"
    assert ident["source_commit"].startswith("f16ae32")
    assert ident["candidate_live_strategy"] == "wave_fade_no_be50_v1"
    assert ident["signal_tfs"] == ["15m", "30m", "1h", "4h"]
    assert ident["edges_version"] == "apt_is_q4_frozen_20260808"
    assert ident["be50_outcome_active"] is False
    assert ident["generation_shared_with_live"] is True
    assert ident["source_sha256"] == FROZEN_SOURCE_SHA256
    assert_frozen_pin()
    assert STRATEGY_ID == "wave_fade_frozen_f16ae32"
    assert CANDIDATE_LIVE_STRATEGY == "wave_fade_no_be50_v1"
    assert SIGNAL_TFS_PIN == ("15m", "30m", "1h", "4h")
    assert EDGES_VERSION_PIN == "apt_is_q4_frozen_20260808"
    assert BE50_OUTCOME_ACTIVE is False
    assert BLOCKED_BY_FROZEN_STRATEGY_MISMATCH == "BLOCKED_BY_FROZEN_STRATEGY_MISMATCH"


def test_runner_does_not_copy_strategy_formulas() -> None:
    engine = Path(__file__).resolve().parents[1] / "engine.py"
    text = engine.read_text(encoding="utf-8")
    assert "def build_symbol_signals" not in text
    assert "def resolve_entries" not in text
    assert "def assign_trend_bucket" not in text
    assert "def load_frozen_eff_edges" not in text
    assert "TREND_ALIGNED" not in text
    assert "uses_be50_exit" not in text
    assert "scan_exit" not in text
    assert evaluate_symbol.__module__ == "research.stoch_fade_runner.engine"
