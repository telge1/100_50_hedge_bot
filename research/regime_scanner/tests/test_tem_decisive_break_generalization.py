"""Generalization scaffolding tests for decisive v3."""

from __future__ import annotations

from research.regime_scanner.tem_structure_break.decisive_models import DECISIVE_SEMANTICS
from research.regime_scanner.tem_structure_break.generalization_metrics import confusion


def test_confusion_helper_still_works_for_v3_preds() -> None:
    blockers = [{"pred_v3": True}, {"pred_v3": False}]
    controls = [{"pred_v3": False}, {"pred_v3": False}, {"pred_v3": True}]
    cm = confusion(blockers, controls, pred_key="pred_v3")
    assert cm["tp"] == 1 and cm["fn"] == 1 and cm["fp"] == 1 and cm["tn"] == 2


def test_v2_not_mutated_flag_in_semantics() -> None:
    assert DECISIVE_SEMANTICS["does_not_mutate_v2"] is True
