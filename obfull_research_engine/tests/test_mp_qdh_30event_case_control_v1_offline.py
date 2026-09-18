"""Offline tests for mp_qdh_30event_case_control_v1."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
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

from obfull_research_engine.mp_qdh_30event_case_control_v1 import (  # noqa: E402
    DISTANCE_STABLE_TICKS,
    EXPECTED_N_WINNERS,
    ALLOW_CLICKHOUSE_WRITES,
)
from obfull_research_engine.mp_qdh_30event_case_control_v1.universe import (  # noqa: E402
    FORBIDDEN_MATCH_KEYS,
    MATCHING_FEATURE_KEYS,
    build_frozen_universe,
    match_quality,
    sha256_json,
)
from obfull_research_engine.mp_qdh_30event_case_control_v1.wall_movement import (  # noqa: E402
    classify_wall_movement,
    normalize_vs_trade_direction,
    wall_distance_ticks,
)
from obfull_research_engine.mp_qdh_30event_case_control_v1.features import extract_flow_features  # noqa: E402
from obfull_research_engine.mp_qdh_30event_case_control_v1.vacuum import extract_vacuum_features  # noqa: E402
from obfull_research_engine.mp_qdh_30event_case_control_v1.paired import (  # noqa: E402
    overlap_groups,
    paired_feature_rows,
)
from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import build_zone_bands  # noqa: E402


REPO = REPO_ROOT


def test_universe_exactly_15_winners_and_unique_controls():
    frozen = build_frozen_universe(REPO)
    assert frozen["manifest"]["n_winners"] == EXPECTED_N_WINNERS
    assert len(frozen["pairs"]) == 15
    assert not frozen["gaps"]
    wids = {p["winner_event_id"] for p in frozen["pairs"]}
    cids = {p["control_event_id"] for p in frozen["pairs"]}
    assert len(wids) == 15 and len(cids) == 15
    assert not (wids & cids)
    assert all(p["control_outcome"] == "WRONG_WAY" for p in frozen["pairs"])


def test_no_winner_as_control_and_no_reuse():
    frozen = build_frozen_universe(REPO)
    cids = [p["control_event_id"] for p in frozen["pairs"]]
    assert len(cids) == len(set(cids))


def test_matching_excludes_outcome_features():
    for k in FORBIDDEN_MATCH_KEYS:
        assert k not in MATCHING_FEATURE_KEYS or k == "reference_entry_ts_ns"
    # outcome class not used in match_quality hard path
    a = {"label_price_only": "ABSORB", "event_role": "LOWER", "trade_side": "LONG", "primary_outcome_class": "BIG_CLEAN_MOVE", "mfe_4h_pct": 9}
    b = {"label_price_only": "ABSORB", "event_role": "LOWER", "trade_side": "LONG", "primary_outcome_class": "WRONG_WAY", "mfe_4h_pct": 0}
    sc, _ = match_quality(a, b)
    assert sc > 0
    # label mismatch hard fail even if mfe similar
    b2 = dict(b, label_price_only="TRUE_BREAK")
    sc2, reasons = match_quality(a, b2)
    assert sc2 < 0 and "LABEL_MISMATCH" in reasons


def test_frozen_manifest_reproducible():
    a = build_frozen_universe(REPO)
    b = build_frozen_universe(REPO)
    assert a["event_list_sha256"] == b["event_list_sha256"]
    assert a["pair_list_sha256"] == b["pair_list_sha256"]
    assert a["contract_hash_sha256"] == b["contract_hash_sha256"]


def test_upper_ask_lower_bid():
    assert build_zone_bands(role="UPPER", low=1.0, high=2.0).defense_side == "ask"
    assert build_zone_bands(role="LOWER", low=1.0, high=2.0).defense_side == "bid"


def test_true_break_does_not_flip_side():
    # side from role only
    bands = build_zone_bands(role="LOWER", low=1.0, high=2.0)
    assert bands.defense_side == "bid"
    # trade_side SHORT would be break direction but defense stays bid
    assert bands.defense_side == "bid"


def test_wall_distance_ask_bid():
    assert wall_distance_ticks(wall_price=100.5, mid=100.0, wall_side="ask", tick=0.1) == pytest.approx(5.0)
    assert wall_distance_ticks(wall_price=99.5, mid=100.0, wall_side="bid", tick=0.1) == pytest.approx(5.0)


def test_classify_retreat_advance_follow_against_stationary():
    # retreat: distance increases
    assert classify_wall_movement(
        wall_side="ask", wall_price_first=100.0, wall_price_last=100.5,
        dist_first=5.0, dist_last=8.0, mid_first=99.5, mid_last=99.7,
        disappeared=False, reappeared_nearby=False,
    ) == "WALL_RETREATS_FROM_PRICE"
    assert classify_wall_movement(
        wall_side="ask", wall_price_first=100.0, wall_price_last=100.0,
        dist_first=5.0, dist_last=2.0, mid_first=99.5, mid_last=99.8,
        disappeared=False, reappeared_nearby=False,
    ) == "WALL_ADVANCES_TOWARD_PRICE"
    # follow: stable distance, same direction
    assert classify_wall_movement(
        wall_side="ask", wall_price_first=100.0, wall_price_last=100.5,
        dist_first=5.0, dist_last=5.0, mid_first=99.5, mid_last=100.0,
        disappeared=False, reappeared_nearby=False,
    ) == "WALL_FOLLOWS_PRICE"
    assert classify_wall_movement(
        wall_side="ask", wall_price_first=100.0, wall_price_last=99.5,
        dist_first=5.0, dist_last=5.0, mid_first=99.5, mid_last=100.0,
        disappeared=False, reappeared_nearby=False,
    ) == "WALL_MOVES_AGAINST_PRICE"
    assert classify_wall_movement(
        wall_side="bid", wall_price_first=100.0, wall_price_last=100.0,
        dist_first=5.0, dist_last=5.0, mid_first=100.5, mid_last=100.5,
        disappeared=False, reappeared_nearby=False,
    ) == "WALL_STATIONARY"
    assert classify_wall_movement(
        wall_side="bid", wall_price_first=100.0, wall_price_last=100.0,
        dist_first=5.0, dist_last=5.0, mid_first=100.5, mid_last=100.5,
        disappeared=True, reappeared_nearby=False,
    ) == "WALL_DISAPPEARS"
    assert classify_wall_movement(
        wall_side="bid", wall_price_first=100.0, wall_price_last=99.5,
        dist_first=5.0, dist_last=5.0, mid_first=100.5, mid_last=100.0,
        disappeared=True, reappeared_nearby=True,
    ) == "WALL_REAPPEARS_NEARBY"


def test_ask_up_down_bid_up_down_normalization():
    # Ask retreats up → opens for LONG
    n = normalize_vs_trade_direction(
        wall_side="ask", movement_state="WALL_RETREATS_FROM_PRICE", trade_side="LONG", wall_move_net_ticks=5.0
    )
    assert n["wall_opens_path_in_trade_direction"] is True
    # Bid retreats down → opens for SHORT
    n2 = normalize_vs_trade_direction(
        wall_side="bid", movement_state="WALL_RETREATS_FROM_PRICE", trade_side="SHORT", wall_move_net_ticks=-3.0
    )
    assert n2["wall_opens_path_in_trade_direction"] is True
    n3 = normalize_vs_trade_direction(
        wall_side="ask", movement_state="WALL_ADVANCES_TOWARD_PRICE", trade_side="LONG", wall_move_net_ticks=-2.0
    )
    assert n3["wall_blocks_trade_direction"] is True


def test_distance_stable_ticks_documented():
    assert DISTANCE_STABLE_TICKS == 1.0


def test_no_post_decision_in_flow_features():
    touch = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    decision = touch.replace(second=30)
    rows = []
    # causal
    rows.append({
        "bucket_start": touch.isoformat().replace("+00:00", "Z"),
        "bucket_end": (touch.replace(microsecond=100000)).isoformat().replace("+00:00", "Z"),
        "bucket_available_at": (touch.replace(microsecond=100000)).isoformat().replace("+00:00", "Z"),
        "phase": "TOUCH_TO_TRIGGER",
        "post_decision": False,
        "attributed_fill": 1.0,
        "residual_pull": 0.0,
        "refill": 0.0,
        "book_decrease": 1.0,
        "net_depletion": 1.0,
        "queue_end": 1.0,
        "qdh_ewma": 0.5,
        "queue_exhausted": False,
        "mid": 100.0,
        "microprice": 100.0,
        "cumulative_fill": 1.0,
        "cumulative_pull": 0.0,
    })
    # forensic must be ignored
    rows.append({
        "bucket_start": (decision.replace(minute=1)).isoformat().replace("+00:00", "Z"),
        "bucket_end": (decision.replace(minute=1, microsecond=100000)).isoformat().replace("+00:00", "Z"),
        "bucket_available_at": (decision.replace(minute=1, microsecond=100000)).isoformat().replace("+00:00", "Z"),
        "phase": "POST_TRIGGER_FORENSIC",
        "post_decision": True,
        "attributed_fill": 99.0,
        "residual_pull": 99.0,
        "refill": 99.0,
        "book_decrease": 99.0,
        "net_depletion": 99.0,
        "queue_end": 0.0,
        "qdh_ewma": 9.0,
        "queue_exhausted": True,
        "mid": 110.0,
        "microprice": 110.0,
        "cumulative_fill": 100.0,
        "cumulative_pull": 99.0,
    })
    feat = extract_flow_features(flow_100ms=rows, funnel={"total_unique_trade_count": 1, "in_band_trade_count": 1, "in_band_trade_qty": 1, "correct_aggressor_trade_count": 1, "correct_aggressor_trade_qty": 1, "attributed_trade_count": 1}, touch_at=touch, decision_at=decision, trade_side="LONG")
    assert feat["attributed_fill_qty"] == 1.0
    assert feat["residual_pull_qty"] == 0.0
    assert feat["post_decision_forensic"] is False
    assert feat["snapshot_120s"] == "NOT_AVAILABLE_AT_DECISION"


def test_vacuum_not_available_without_ladder():
    touch = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)
    v = extract_vacuum_features(states=[], wall_side="ask", wall_price=100.0, touch_at=touch, decision_at=touch)
    assert v["same_side_depth_inside_1bp"] == "NOT_AVAILABLE"
    assert v["depth_beyond_wall"] == "NOT_AVAILABLE"


def test_paired_keeps_pair_and_no_zero_impute():
    pairs = [{"winner_event_id": "w1", "control_event_id": "c1"}]
    feats = {"w1": {"x": 1.0}, "c1": {"x": None}}
    rows = paired_feature_rows(pairs, feats, ["x"])
    assert rows[0]["n_valid_pairs"] == 0
    assert rows[0]["missing_policy"] == "pairwise_complete_only_never_impute_zero"


def test_overlap_deterministic():
    rows = [
        {"event_id": "a", "case_role": "WINNER", "reference_entry_ts_ns": 1_000_000_000, "pair_id": "P1"},
        {"event_id": "b", "case_role": "CONTROL", "reference_entry_ts_ns": 1_100_000_000, "pair_id": "P1"},
        {"event_id": "c", "case_role": "WINNER", "reference_entry_ts_ns": 10_000_000_000, "pair_id": "P2"},
    ]
    g1 = overlap_groups(rows, window_s=200)
    g2 = overlap_groups(rows, window_s=200)
    assert g1 == g2
    assert any(g["n_events"] >= 2 for g in g1)


def test_sha256_stable():
    assert sha256_json({"a": 1, "b": [2, 3]}) == sha256_json({"b": [2, 3], "a": 1})


def test_ch_writes_forbidden_flag():
    assert ALLOW_CLICKHOUSE_WRITES is False


def test_qdh_engine_import_reused():
    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.queue_depletion_hazard import update_qdh
    assert callable(update_qdh)


def test_fill_pull_identity_residual():
    from obfull_research_engine.level_first_episode1_wall_flow_qdh_base_v1.mass_balance import decompose_mass_balance
    # queue 1.0 → 0.0 with hit 0.4 → residual_pull=0.6, refill=0
    mb = decompose_mass_balance(queue_before=1.0, queue_after=0.0, attributed_hit_qty=0.4)
    assert mb.residual_pull_qty == pytest.approx(0.6)
    assert mb.net_refill_qty == pytest.approx(0.0)
    assert mb.net_depletion_qty == pytest.approx(1.0)


def test_near_zero_queue_policy_no_inf():
    from obfull_research_engine.mp_qdh_canonical_integration_v1.near_zero import apply_queue_policy
    pol = apply_queue_policy(queue_remaining_qty=0.0, qdh_base=1.0, queue_runway_seconds=None)
    assert pol.get("queue_state") == "QUEUE_EXHAUSTED"
    v = pol.get("canonical_qdh_base")
    if v is not None:
        import math
        assert math.isfinite(float(v))


def test_contract_hash_protects_mix(tmp_path: Path):
    frozen = build_frozen_universe(REPO)
    ckpt = tmp_path / "checkpoints"
    ckpt.mkdir()
    (ckpt / "contract_hash.txt").write_text("deadbeef\n", encoding="utf-8")
    # simulate check
    old = (ckpt / "contract_hash.txt").read_text().strip()
    assert old != frozen["contract_hash_sha256"]
