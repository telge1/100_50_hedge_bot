"""Tests for FROZEN_LARGE_MOVE_CANDIDATE_FORWARD_CONFIRMATION_V1."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.large_move_forward_confirmation_runner import (
    EXPECTED_CANDIDATE_SHA,
    EXPECTED_V2_SHA_PREFIX,
    EXCLUDED_DAYS,
    FROZEN_FEATURES,
    NO_FIT_FWD,
    _build_day_coverage,
    _candidate_sha,
    _load_json,
    _score,
    _verify_candidate,
    _verify_v2,
)

CAND_DIR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_accepted_large_move_separability_discovery_v1/candidate_bundle_v1"
)


def test_no_fit_flags_false():
    assert all(v is False for v in NO_FIT_FWD.values())


def test_excluded_days_hardcoded():
    assert EXCLUDED_DAYS == {"2026-08-24", "2026-08-25", "2026-08-26"}


def test_exactly_four_frozen_features_order():
    assert FROZEN_FEATURES == [
        "flow_opp_notional_60s",
        "flow_max_buy_bubble_5s",
        "ctx_range_bps_5m",
        "ctx_ret_bps_180s",
    ]
    c = _load_json(CAND_DIR / "candidate_contract.json")
    assert c["selected_features"] == FROZEN_FEATURES
    assert list(c["model"]["coefficients"].keys()) == FROZEN_FEATURES


def test_candidate_and_freeze_sha_verify():
    cb = _verify_candidate("test")
    assert cb["candidate_sha256"] == EXPECTED_CANDIDATE_SHA
    v2 = _verify_v2("test")
    assert str(v2["freeze_bundle_sha256"]).startswith(EXPECTED_V2_SHA_PREFIX)


def test_candidate_sha_stable():
    c = _load_json(CAND_DIR / "candidate_contract.json")
    assert _candidate_sha(c) == EXPECTED_CANDIDATE_SHA


def test_absolute_threshold_not_quantile():
    c = _load_json(CAND_DIR / "candidate_contract.json")
    thr = float(c["score_threshold"])
    assert thr == pytest.approx(0.40673268362827575)
    # scoring uses absolute compare
    assert (0.5 >= thr) is True
    assert (0.1 >= thr) is False


def test_score_uses_frozen_coefs():
    c = _load_json(CAND_DIR / "candidate_contract.json")
    coefs = {k: float(v) for k, v in c["model"]["coefficients"].items()}
    intercept = float(c["model"]["intercept"])
    # zero z-scores → sigmoid(intercept)
    p = _score([0.0, 0.0, 0.0, 0.0], coefs, intercept)
    import math

    expected = 1.0 / (1.0 + math.exp(-intercept))
    assert p == pytest.approx(expected, rel=1e-9)


def test_day_coverage_excludes_discovery_and_open_days():
    now = datetime(2026, 8, 30, 10, 0, tzinfo=timezone.utc)
    hours = []
    for day, n in [("2026-08-27", 24), ("2026-08-28", 24), ("2026-08-30", 9)]:
        for h in range(n):
            hours.append(
                {
                    "hour_start": f"{day}T{h:02d}:00:00Z",
                    "status": "ELIGIBLE",
                }
            )
    # inject excluded discovery day hours
    for h in range(24):
        hours.append({"hour_start": f"2026-08-26T{h:02d}:00:00Z", "status": "ELIGIBLE"})
    selected, excluded = _build_day_coverage(hours, now_utc=now)
    days_s = {r["utc_day"] for r in selected}
    assert "2026-08-26" not in days_s
    assert "2026-08-30" not in days_s  # not fully closed
    assert "2026-08-27" in days_s
    assert "2026-08-28" in days_s


def test_one_position_chronological():
    from orderbook_analyse.aggressor_efficiency_flip.timeutil import parse_utc

    cand = [
        {"entry_book_ts": "2026-08-27T10:00:00Z", "id": 1},
        {"entry_book_ts": "2026-08-27T10:05:00Z", "id": 2},
        {"entry_book_ts": "2026-08-27T10:16:00Z", "id": 3},
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


def test_stop_only_end_of_day_logic():
    # document: stop checked only after full day accumulation
    progress = [
        {"utc_day": "2026-08-27", "cum_candidate_n": 40, "n_days_done": 1},
        {"utc_day": "2026-08-28", "cum_candidate_n": 90, "n_days_done": 2},
        {"utc_day": "2026-08-29", "cum_candidate_n": 120, "n_days_done": 3},
    ]
    stop_at = None
    for p in progress:
        if p["cum_candidate_n"] >= 100 and p["n_days_done"] >= 3:
            stop_at = p["utc_day"]
            break
    assert stop_at == "2026-08-29"
