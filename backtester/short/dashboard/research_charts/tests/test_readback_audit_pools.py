"""Regression: audit pool readback membership (JSON ACTIVE vs CH catalog)."""

from __future__ import annotations

import sys
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.live_micro_lld_v2_ch_test.historical_json_import.readback_parity import (
    _audit_pool_detail_checks,
)

AUDIT_A = "lldv2:DOGEUSDT:15m:micro:lower:111"
AUDIT_B = "lldv2:DOGEUSDT:15m:micro:upper:222"
STEM = "20260801T000000Z"


def test_audit_both_inactive_passes():
    assert _audit_pool_detail_checks(
        [AUDIT_A, AUDIT_B], {}, {}, stem=STEM
    ) == []


def test_audit_both_active_passes():
    row = {"pool_id": AUDIT_A, "side": "lower"}
    assert _audit_pool_detail_checks(
        [AUDIT_A],
        {AUDIT_A: row},
        {AUDIT_A: row},
        stem=STEM,
    ) == []


def test_audit_active_json_only_fails():
    row = {"pool_id": AUDIT_A}
    errs = _audit_pool_detail_checks(
        [AUDIT_A], {AUDIT_A: row}, {}, stem=STEM
    )
    assert len(errs) == 1
    assert "ACTIVE in JSON" in errs[0]
    assert AUDIT_A in errs[0]


def test_audit_active_ch_only_fails():
    row = {"pool_id": AUDIT_A}
    errs = _audit_pool_detail_checks(
        [AUDIT_A], {}, {AUDIT_A: row}, stem=STEM
    )
    assert len(errs) == 1
    assert "not ACTIVE in JSON" in errs[0]


def test_audit_mixed_inactive_and_active_pair():
    row = {"pool_id": AUDIT_A}
    errs = _audit_pool_detail_checks(
        [AUDIT_A, AUDIT_B],
        {AUDIT_A: row},
        {AUDIT_A: row},
        stem=STEM,
    )
    assert errs == []
