"""Offline tests for first-touch study confidence/contract/outcomes."""

from __future__ import annotations

import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
_shadow = str(REPO_ROOT / "src")
while _shadow in sys.path:
    sys.path.remove(_shadow)
sys.path.insert(0, str(ENGINE_ROOT / "src"))
_ORDERBOOK_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if _ORDERBOOK_SRC.is_dir():
    sys.path.insert(0, str(_ORDERBOOK_SRC))

import pytest  # noqa: E402

from obfull_research_engine.mp_price_path_4h_v1.geometry import mae_from_extremes, mfe_from_extremes  # noqa: E402
from obfull_research_engine.mp_qdh_first_touch_study_v1 import (  # noqa: E402
    ALLOW_CLICKHOUSE_WRITES,
    TARGET_REACH_PCT,
)
from obfull_research_engine.mp_qdh_first_touch_study_v1.confidence import (  # noqa: E402
    classify_availability_confidence,
    classify_flow_attribution_confidence,
)
from obfull_research_engine.mp_qdh_first_touch_study_v1.contract import (  # noqa: E402
    CONTRACT_HASH,
    compute_contract_hash,
    validate_checkpoint_contract,
)
from obfull_research_engine.mp_qdh_first_touch_study_v1.universe import build_first_touch_universe  # noqa: E402


def test_no_ch_writes():
    assert ALLOW_CLICKHOUSE_WRITES is False


def test_receive_time_does_not_force_flow_low():
    flow = classify_flow_attribution_confidence(
        coverage_pass=True,
        coverage_blockers=[],
        linkage_status="PRESENT_AT_TOUCH",
        has_wall=True,
        mass_balance_violations=0,
        seq_gaps=0,
        cross_epoch=0,
        lookahead_flags=0,
        attributed_trade_count=10,
        unmatched_fill=0.0,
        unknown_qty=1.0,
        book_decrease_qty=10.0,
        inferred_refill_qty=0.0,
        fill_qty=2.0,
    )
    assert flow["flow_attribution_confidence"] == "HIGH"
    assert flow["receive_time_affects_flow_confidence"] is False
    avail = classify_availability_confidence(
        coverage_pass=True, receive_present=0, receive_missing=100
    )
    assert avail["availability_confidence"] == "RECEIVE_TIME_NOT_AVAILABLE"
    assert avail["usable_for_live_latency_claim"] is False
    assert avail["usable_for_historical_research"] is True


def test_coverage_blocks_flow():
    flow = classify_flow_attribution_confidence(
        coverage_pass=False,
        coverage_blockers=["PUBLIC_TRADE_COVERAGE_MISSING"],
        linkage_status="PRESENT_AT_TOUCH",
        has_wall=True,
        mass_balance_violations=0,
        seq_gaps=0,
        cross_epoch=0,
        lookahead_flags=0,
        attributed_trade_count=0,
        unmatched_fill=0.0,
        unknown_qty=0.0,
        book_decrease_qty=0.0,
        inferred_refill_qty=0.0,
        fill_qty=0.0,
    )
    assert flow["flow_attribution_confidence"] == "BLOCKED"


def test_contract_hash_stable_and_rejects_stale():
    from obfull_research_engine.mp_qdh_first_touch_study_v1.contract import (
        CONTRACT_SOURCE_RELPATHS,
        LEGACY_PARAM_ONLY_CONTRACT_HASH,
    )

    h1 = compute_contract_hash()
    h2 = compute_contract_hash()
    assert h1 == h2 == CONTRACT_HASH
    assert validate_checkpoint_contract({"contract_hash": "old"}, expected_hash=h1)["reason"] == (
        "STALE_CHECKPOINT_REJECTED"
    )
    assert validate_checkpoint_contract({}, expected_hash=h1)["reason"] == "STALE_CHECKPOINT_REJECTED"
    assert validate_checkpoint_contract({"contract_hash": h1}, expected_hash=h1)["ok"] is True
    # differs from legacy v2 version string
    assert CONTRACT_HASH != "2.0.0"
    # old param-only FT hash rejected under freeze contract
    assert CONTRACT_HASH != LEGACY_PARAM_ONLY_CONTRACT_HASH
    stale = validate_checkpoint_contract(
        {"contract_hash": LEGACY_PARAM_ONLY_CONTRACT_HASH}, expected_hash=h1
    )
    assert stale["ok"] is False
    joined = "\n".join(CONTRACT_SOURCE_RELPATHS)
    assert "aggressor_flow.py" in joined
    assert "wall_flow_attribution.py" in joined
    assert "timeline_100ms.py" in joined


def test_mfe_mae_long_short_pct():
    assert mfe_from_extremes(trade_side="LONG", trigger_price=100.0, high=100.41, low=99.0) == pytest.approx(0.41)
    assert mae_from_extremes(trade_side="LONG", trigger_price=100.0, high=101.0, low=99.8) == pytest.approx(0.2)
    assert mfe_from_extremes(trade_side="SHORT", trigger_price=100.0, high=101.0, low=99.59) == pytest.approx(0.41)
    assert mae_from_extremes(trade_side="SHORT", trigger_price=100.0, high=100.25, low=99.0) == pytest.approx(0.25)


def test_universe_first_touch_only_no_282():
    uni = build_first_touch_universe(REPO_ROOT)
    assert uni["manifest"]["n_batch_events"] == 282
    assert uni["manifest"]["n_first_touch_raw"] == 120
    assert uni["manifest"]["n_included"] == 115
    assert "UNRESOLVED" in uni["manifest"]["exclusion_counts"]
    assert all(r["is_first_touch"] for r in uni["included"])
    assert len({r["event_id"] for r in uni["included"]}) == 115
    # TRUE_BREAK not using fade incorrectly
    for r in uni["included"]:
        if r["label_price_only"] == "TRUE_BREAK":
            assert r["trade_side"] == r["break_side"]


def test_target_reach_constant():
    assert TARGET_REACH_PCT == pytest.approx(0.41)
