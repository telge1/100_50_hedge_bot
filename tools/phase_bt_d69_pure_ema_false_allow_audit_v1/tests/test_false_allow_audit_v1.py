"""BT-D69 regression tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
for p in (
    PKG,
    ROOT / "tools/phase_bt_d59_market_regime_pool_evolution_v1",
    ROOT / "tools/phase_bt_d60_ema_history_validation_v1",
    ROOT / "tools/phase_bt_d61_regime_gegenprobe_audit_v1",
    ROOT / "tools/phase_bt_d65_pure_ema_timestamp_review_v1",
):
    sys.path.insert(0, str(p))

from variant_classifiers_v1 import classify_variant  # noqa: E402


def _minimal_row(**kwargs):
    base = {
        "ema_ready": True,
        "ema_bull_stack_9_20_59_200": False,
        "ema_bear_stack_9_20_59_200": False,
        "ema_tight_cluster": False,
        "close_below_ema9": True,
        "close_below_ema20": True,
        "close_below_ema59": True,
        "ema59_slope_6b_pct": -0.1,
        "ema59_slope_12b_pct": -0.1,
        "ema200_slope_12b_pct": -0.05,
        "ema200_slope_24b_pct": -0.05,
    }
    base.update(kwargs)
    return base


def test_v1_fallback_short_ok_without_bear_stack():
    row = _minimal_row(ema_bear_stack_9_20_59_200=False)
    d, r = classify_variant(row, "v1_bt_d65_frozen")
    assert d == "SHORT_OK"
    assert r == "PRICE_BELOW_FALLING_EMA59_200"


def test_v3_blocks_fallback_without_bear_stack():
    row = _minimal_row(ema_bear_stack_9_20_59_200=False)
    d, _ = classify_variant(row, "v3_bear_stack_falling_59_200")
    assert d != "SHORT_OK"


def test_v3_allows_full_bear_stack_falling():
    row = _minimal_row(ema_bear_stack_9_20_59_200=True)
    d, r = classify_variant(row, "v3_bear_stack_falling_59_200")
    assert d == "SHORT_OK"
    assert r == "RESEARCH_BEAR_STACK_FALLING_59_200"


def test_manifest_verdict_if_results_present():
    path = ROOT / "results/phase_bt_d69_pure_ema_audit_v1/evidence_manifest.json"
    if not path.is_file():
        return
    m = json.loads(path.read_text())
    assert m["verdict"] in (
        "PURE_EMA_FALSE_ALLOW_AUDIT_PASS",
        "PURE_EMA_FALSE_ALLOW_AUDIT_INCONCLUSIVE",
        "PURE_EMA_FALSE_ALLOW_AUDIT_BLOCKED",
    )
