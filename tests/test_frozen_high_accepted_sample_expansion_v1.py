"""Tests for frozen HIGH∩ACCEPTED sample expansion v1."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.freeze_v1 import verify_freeze
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_coverage import (
    build_multi_day_coverage,
    chronological_eligible_hours,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.sample_expansion_runner import (
    ACCEPTANCE_ALIGN_SIGN,
    NO_FIT_FLAGS,
    TARGET_HIGH_ACCEPTED_ANY,
    FrozenBundleTampered,
    _copy_freeze_manifests,
    _is_high_accepted,
    _verify_or_tamper,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.state_aligned_outcomes import (
    alignment_for_state,
)

PRIOR = Path(
    "/home/telgenbuescher/projects/orderbook_analyse/results/"
    "frozen_high_edge_forward_outcome_evaluation_v1"
)


def test_freeze_tamper_aborts(tmp_path: Path):
    if not (PRIOR / "frozen_hashes.json").is_file():
        pytest.skip("prior freeze missing")
    _copy_freeze_manifests(PRIOR, tmp_path)
    p = tmp_path / "frozen_hashes.json"
    data = json.loads(p.read_text())
    data["source_file_sha256"]["aggressor_efficiency_trapped_vwap_acceptance/contracts.py"] = "00"
    p.write_text(json.dumps(data))
    with pytest.raises(FrozenBundleTampered):
        _verify_or_tamper(tmp_path, "tamper")


def test_no_fit_flags_false():
    assert NO_FIT_FLAGS["outcome_used_for_matching"] is False
    assert NO_FIT_FLAGS["outcome_used_for_thresholds"] is False
    assert NO_FIT_FLAGS["outcome_used_for_state_definition"] is False
    assert NO_FIT_FLAGS["outcome_used_for_sample_selection"] is False


def test_acceptance_direction_contract():
    assert ACCEPTANCE_ALIGN_SIGN["ACCEPTED_ABOVE"] == 1
    assert ACCEPTANCE_ALIGN_SIGN["ACCEPTED_BELOW"] == -1


def test_no_invented_absorption_direction():
    sign, _ = alignment_for_state(
        "ABSORPTION_NO_RESOLUTION",
        direction="LONG",
        wall_side="BID",
        acceptance_state="ACCEPTED_BELOW",
        allow_acceptance_fallback=False,
    )
    assert sign is None


def test_chronological_eligible_deterministic():
    rows, summary = build_multi_day_coverage()
    h1 = chronological_eligible_hours(rows)
    h2 = chronological_eligible_hours(rows)
    assert h1 == h2
    assert h1 == sorted(h1)
    assert all(r["status"] in {"ELIGIBLE", "PARTIAL", "BLOCKED"} for r in rows)
    assert all(r["status"] == "ELIGIBLE" for r in rows if r["hour_start"] in h1)
    assert summary["outcome_used_for_window_selection"] is False


def test_eligible_requires_next_hour():
    rows, _ = build_multi_day_coverage()
    by = {r["hour_start"]: r for r in rows}
    for h in chronological_eligible_hours(rows):
        assert by[h]["next_hour_paired"] is True
        assert by[h]["status"] == "ELIGIBLE"


def test_high_accepted_predicate():
    assert _is_high_accepted(
        {"edge_match_confidence_class": "HIGH", "final_acceptance_state": "ACCEPTED_BELOW"}
    )
    assert not _is_high_accepted(
        {"edge_match_confidence_class": "MEDIUM", "final_acceptance_state": "ACCEPTED_BELOW"}
    )
    assert not _is_high_accepted(
        {"edge_match_confidence_class": "HIGH", "final_acceptance_state": "UNKNOWN_EDGE"}
    )


def test_target_n_constant():
    assert TARGET_HIGH_ACCEPTED_ANY == 30


def test_stop_rule_logic():
    cum = 0
    stop = "COVERAGE_EXHAUSTED"
    for add in (5, 10, 8, 10):
        cum += add
        if cum >= 30:
            stop = "TARGET_REACHED"
            break
    assert stop == "TARGET_REACHED"
    assert cum >= 30


def test_prior_freeze_still_verifies():
    if not (PRIOR / "frozen_hashes.json").is_file():
        pytest.skip("prior freeze missing")
    ok = verify_freeze(PRIOR)
    assert ok["ok"] is True
    assert str(ok["freeze_bundle_sha256"]).startswith("67924037")


def test_copy_freeze_no_regen(tmp_path: Path):
    if not (PRIOR / "frozen_hashes.json").is_file():
        pytest.skip("prior freeze missing")
    _copy_freeze_manifests(PRIOR, tmp_path)
    a = (PRIOR / "frozen_hashes.json").read_text()
    b = (tmp_path / "frozen_hashes.json").read_text()
    assert a == b
    assert _verify_or_tamper(tmp_path, "copy")["ok"] is True
