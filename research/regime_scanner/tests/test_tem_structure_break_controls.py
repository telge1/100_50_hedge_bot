"""Control selection tests (scanner-blind, deterministic)."""

from __future__ import annotations

from research.regime_scanner.tem_structure_break.control_selection import (
    SELECTION_RULE_ID,
    select_control_specs,
    selection_manifest,
)
from research.regime_scanner.tem_structure_break.eval_common import load_blocker_specs


def test_control_selection_deterministic_and_scanner_blind() -> None:
    coins = {s.coin for s in load_blocker_specs()}
    a, audit_a = select_control_specs(coins)
    b, audit_b = select_control_specs(coins)
    assert [x.trade_id for x in a] == [x.trade_id for x in b]
    assert [x["trade_id"] for x in audit_a] == [x["trade_id"] for x in audit_b]
    assert all(s.cohort == "control" for s in a)
    assert all(s.holdout_bucket == "control" for s in a)
    assert all((s.final_pnl or 0) > 0 for s in a)
    assert all(s.selection_reason for s in a)
    # no blocker trade ids
    blocker_ids = {s.trade_id for s in load_blocker_specs()}
    assert blocker_ids.isdisjoint({s.trade_id for s in a})


def test_selection_manifest_documents_rule() -> None:
    m = selection_manifest()
    assert m["selection_rule_id"] == SELECTION_RULE_ID
    assert m["scanner_blind"] is True
    assert "max_cycle" in m["documentation"]
