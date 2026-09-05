"""Tests for FROZEN_HIGH_ACCEPTED_LARGE_MOVE_SEPARABILITY_DISCOVERY_V1."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orderbook_analyse.aggressor_efficiency_flip.models import Trade
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.large_move_contracts import (
    EXPECTED_V2_SHA_PREFIX,
    MODEL_CONTRACT,
    NO_FIT_LM,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.large_move_features import (
    acceptance_features,
    book_features_at_entry,
    compute_path_outcomes,
    directional_bps,
    pool_distance_features,
    trade_flow_features,
)
from orderbook_analyse.ob200_v3_raw_discovery.reconstruct import SampleRow

UTC = timezone.utc
BASE = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
ET_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_entry_timing_v1"
)
FREEZE_V2 = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_contract_fix_refreeze_v2/freeze_bundle_v2"
)


def S(
    sec: int,
    bid: float = 100.0,
    ask: float = 100.2,
    *,
    ask_wall: float | None = None,
    bid_wall: float | None = None,
) -> SampleRow:
    ms = int((BASE + timedelta(seconds=sec)).timestamp() * 1000)
    mid = 0.5 * (bid + ask)
    return SampleRow(
        symbol="BTCUSDT",
        ts_ms=ms,
        best_bid=bid,
        best_ask=ask,
        mid=mid,
        spread=ask - bid,
        spread_bps=(ask - bid) / mid * 1e4,
        microprice=mid,
        bid_levels=1,
        ask_levels=1,
        bid_qty_l10=1.0,
        ask_qty_l10=1.0,
        imbalance_l10=0.0,
        bid_qty_bps10=1.0,
        ask_qty_bps10=1.0,
        imbalance_bps10=0.0,
        bid_wall_price=bid_wall,
        bid_wall_qty=10.0 if bid_wall else None,
        ask_wall_price=ask_wall,
        ask_wall_qty=10.0 if ask_wall else None,
        source_file="test",
        warmup=False,
        bid_far_wall_price=bid_wall,
        ask_far_wall_price=ask_wall,
    )


def test_no_fit_flags_all_false():
    assert all(v is False for v in NO_FIT_LM.values())
    assert MODEL_CONTRACT["no_grid_search"] is True
    assert MODEL_CONTRACT["C"] == 1.0
    assert MODEL_CONTRACT["penalty"] == "l2"


def test_cohort_1192_unique_from_entry_timing():
    import csv

    rows = [r for r in csv.DictReader(open(ET_DIR / "entry_execution.csv")) if r["status"] == "OK"]
    ids = [r["entry_signal_id_v2"] for r in rows]
    assert len(ids) == 1192
    assert len(set(ids)) == 1192
    assert all(r.get("trade_side") in ("LONG", "SHORT") for r in rows)
    # no fallback to 1207 duplicate rows
    assert len(rows) != 1207


def test_direction_mapping_acceptance():
    rows = list(__import__("csv").DictReader(open(ET_DIR / "entry_execution.csv")))
    for r in rows:
        if r["status"] != "OK":
            continue
        if r["acceptance_state"] == "ACCEPTED_ABOVE":
            assert r["trade_side"] == "LONG"
        if r["acceptance_state"] == "ACCEPTED_BELOW":
            assert r["trade_side"] == "SHORT"


def test_freeze_v2_sha_prefix():
    from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v2 import (
        verify_freeze_v2,
    )

    out = verify_freeze_v2(FREEZE_V2)
    assert str(out["freeze_bundle_sha256"]).startswith(EXPECTED_V2_SHA_PREFIX)


def test_directional_bps_long_short():
    assert directional_bps("LONG", 100.0, 100.3) == pytest.approx(30.0)
    assert directional_bps("SHORT", 100.0, 99.7) == pytest.approx((100 / 99.7 - 1.0) * 1e4)


def test_label_25bps_and_mfe_from_entry_only_bid_ask():
    # after entry at sec=5, bid rises clearly above +25bps; pre-entry spike ignored
    samples = []
    for i in range(0, 1000):
        if i < 5:
            samples.append(S(i, bid=110.0, ask=110.2))
        elif i == 10:
            samples.append(S(i, bid=100.30, ask=100.50))  # +30bps vs entry 100
        else:
            samples.append(S(i, bid=100.0, ask=100.2))
    out = compute_path_outcomes(
        samples, side="LONG", entry_ts=BASE + timedelta(seconds=5), entry_px=100.0
    )
    assert out["LARGE_MOVE_25BPS_15M"] is True
    assert out["mfe_bps_15m"] == pytest.approx(30.0, abs=0.01)
    assert out["mfe_bps_15m"] < 50


def test_target_before_adverse_and_same_bucket_ambiguous():
    # clean: hit +25 before -15
    samples = []
    for i in range(0, 100):
        if i == 10:
            samples.append(S(i, bid=100.30, ask=100.50))
        elif i == 20:
            samples.append(S(i, bid=99.8, ask=100.0))  # adverse later
        else:
            samples.append(S(i, bid=100.0, ask=100.2))
    out = compute_path_outcomes(
        samples, side="LONG", entry_ts=BASE, entry_px=100.0
    )
    assert out["CLEAN_LARGE_MOVE_25_15"] is True
    assert out["path_class_15m"] == "TARGET_BEFORE_ADVERSE"

    # same 1s bucket both barriers
    s_pos = S(1, bid=100.30, ask=100.50)
    s_neg = S(1, bid=99.85, ask=100.05)
    s_neg.ts_ms = s_pos.ts_ms + 100  # same second
    out2 = compute_path_outcomes(
        [S(0), s_pos, s_neg], side="LONG", entry_ts=BASE, entry_px=100.0
    )
    assert out2["path_class_15m"] == "SAME_BUCKET_AMBIGUOUS"
    assert out2["CLEAN_LARGE_MOVE_25_15"] is False


def test_feature_windows_end_at_entry_no_forward():
    trades = [
        Trade(
            trade_ts=BASE - timedelta(seconds=10),
            trade_id=1,
            side="Buy",
            price=100.0,
            size=1.0,
            notional=100.0,
        ),
        Trade(
            trade_ts=BASE + timedelta(seconds=1),  # after entry — must NOT count
            trade_id=2,
            side="Buy",
            price=100.0,
            size=1.0,
            notional=99999.0,
        ),
    ]
    feats, meta = trade_flow_features(trades, entry_ts=BASE, windows=(15,))
    assert feats["flow_buy_notional_15s"] == 100.0
    assert meta["flow_15s"]["causal_ok"] is True
    assert meta["flow_15s"]["feature_available_ts"].endswith("Z") or "T" in meta["flow_15s"]["feature_available_ts"]


def test_book_and_pool_as_of_entry():
    samples = [
        S(0, bid=100.0, ask=100.2, ask_wall=100.4),
        S(5, bid=100.1, ask=100.3, ask_wall=100.5),  # after entry
    ]
    entry = BASE + timedelta(seconds=2)
    bf, bm = book_features_at_entry(samples, entry_ts=entry)
    assert bm["book"]["causal_ok"] is True
    pf, pm = pool_distance_features(
        samples,
        entry_ts=entry,
        entry_mid=100.1,
        side="LONG",
        matched_edge_price=100.0,
    )
    assert pm["pool_distance"]["causal_ok"] is True
    # wall from sample at sec0 (only sample <= entry)
    assert pf["next_opposing_wall_distance_bps"] is not None
    assert pf["free_path_bps"] == pf["next_opposing_wall_distance_bps"]


def test_acceptance_features_causal():
    row = {
        "acceptance_state": "ACCEPTED_ABOVE",
        "signal_available_ts": (BASE - timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
        "migration_class": "REARM",
        "spread_bps": "1.5",
    }
    af, am = acceptance_features(row, entry_ts=BASE)
    assert af["acc_is_above"] == 1.0
    assert af["acc_rearm"] == 1.0
    assert af["acc_secs_signal_to_entry"] == pytest.approx(3.0)
    assert am["acceptance_quality"]["causal_ok"] is True


def test_one_position_chronological_logic():
    from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc

    cand = [
        {"entry_book_ts": "2026-08-26T10:00:00Z", "id": 1},
        {"entry_book_ts": "2026-08-26T10:05:00Z", "id": 2},  # overlap 15m
        {"entry_book_ts": "2026-08-26T10:16:00Z", "id": 3},
    ]
    op = []
    free_at = None
    for r in sorted(cand, key=lambda x: parse_utc(x["entry_book_ts"])):
        ets = parse_utc(r["entry_book_ts"])
        if free_at is not None and ets < free_at:
            continue
        op.append(r)
        free_at = ets + timedelta(seconds=900)
    assert [x["id"] for x in op] == [1, 3]


def test_model_contract_no_zoo():
    assert "RandomForest" not in MODEL_CONTRACT["model_type"]
    assert MODEL_CONTRACT["score_threshold_rule"] == "development_top_20pct_quantile"
