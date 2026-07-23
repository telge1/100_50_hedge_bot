"""No-hardcoding and freeze audits for TEM structure-break v2."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from research.regime_scanner.run_tem_structure_break_generalization import overfitting_checks
from research.regime_scanner.tem_structure_break.frozen_v2 import FROZEN_RULE_ID, frozen_semantics_public
from research.regime_scanner.tem_structure_break.monitor import SIGNAL_VERSION

MONITOR = Path(__file__).resolve().parents[1] / "tem_structure_break" / "monitor.py"


def test_monitor_has_no_aave_hardcoding() -> None:
    text = MONITOR.read_text(encoding="utf-8")
    assert "AAVEUSDT" not in text
    assert "2026-01-19" not in text
    assert "170.86" not in text
    assert "continuous|0006" not in text
    assert re.search(r"(?<![0-9])0006(?![0-9])", text) is None


def test_overfitting_checks_clean() -> None:
    checks = overfitting_checks()
    assert checks["any_hardcoding"] is False
    assert checks["coin_or_trade_literal_compares"] == []


def test_frozen_semantics_snapshot_complete() -> None:
    sem = frozen_semantics_public()
    assert sem["frozen"] is True
    assert sem["rule_id"] == FROZEN_RULE_ID
    assert sem["signal_version"] == SIGNAL_VERSION
    for key in (
        "reclaim_window",
        "level_priority_4h_arm",
        "frozen_level_semantics",
        "dynamic_v1lag_semantics",
        "rebreak_semantics",
        "state_transitions",
        "event_dedup_semantics",
        "macro_aware_reclaim_semantics",
        "break_episode_semantics",
        "sticky_invalidation_semantics",
    ):
        assert key in sem


def test_monitor_ast_has_no_usdt_literal_branches() -> None:
    tree = ast.parse(MONITOR.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "USDT" not in node.value
            assert "continuous|" not in node.value
