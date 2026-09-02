"""OB replay and golden-case integration tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.btc_ob_fight.config import resolve_ob_root
from research.btc_ob_fight.ob_replay import find_hour_segment, iter_ndjson, replay_as_of


@pytest.fixture(scope="module")
def ob_root():
    root = resolve_ob_root()
    if root is None:
        pytest.skip("OB200 shadow root unavailable")
    return root


def test_ob_zstd_chunk_read(ob_root: Path):
    at = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    path = find_hour_segment(ob_root, "BTCUSDT", at)
    assert path.is_file()
    count = 0
    for _ in iter_ndjson(path):
        count += 1
        if count >= 5:
            break
    assert count >= 5


def test_snapshot_delta_reconstruction(ob_root: Path):
    at = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    snap = replay_as_of(ob_root, "BTCUSDT", at)
    assert snap["bid_levels"] >= 180
    assert snap["ask_levels"] >= 180
    assert float(snap["best_bid"]) < float(snap["best_ask"])


def test_prefix_parity_same_cutoff(ob_root: Path):
    at = datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc)
    a = replay_as_of(ob_root, "BTCUSDT", at)
    b = replay_as_of(ob_root, "BTCUSDT", at)
    assert float(a["mid"]) == float(b["mid"])
    assert a["last_u"] == b["last_u"]


@pytest.mark.integration
def test_golden_btc_case(tmp_path: Path, ob_root: Path):
    from research.btc_ob_fight.cli import run_analysis
    from research.btc_ob_fight.config import RunConfig

    cfg = RunConfig(
        symbol="BTCUSDT",
        anchor=datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        before_minutes=30,
        after_minutes=30,
        ob_root=ob_root,
        out_root=tmp_path,
    )
    code = run_analysis(cfg)
    assert code == 0
    run_dirs = sorted((tmp_path / "btc_ob_fight_cases" / "20260831T190000Z").glob("run_*"))
    summary = json.loads((run_dirs[-1] / "summary.json").read_text())
    pf = summary["profile_facts"]
    assert summary["analysis_status"] == "FACTS_READY_RULES_UNFROZEN"
    assert summary["trade_verdict_evaluated"] is False
    assert pf["inside_tpo_value_area"] is True
    assert pf.get("volume_profile_status") == "COMPUTED_SEPARATELY"
    assert summary.get("volume_profile_status") == "COMPUTED_SEPARATELY"
    vp = summary.get("volume_profile") or {}
    assert vp.get("vpoc") is not None
    assert vp.get("integrity") == "PASS"
    nearest = (pf.get("nearest_tpo_levels") or pf.get("nearest_profile_levels") or [{}])[0]
    assert nearest.get("kind") in ("vah", "poc", "val", "hvn", "lvn")
    assert abs(float(nearest["price"]) - float(pf["tpo_vah"])) <= 10.0
    tpo = summary.get("tpo_profile") or {}
    assert tpo.get("status") == "COMPUTED_SEPARATELY"
    assert pf.get("tpo_volume_confluence_status") == "VALID_INDEPENDENT_MEASURES"
    assert summary["schema_version"] == "btc_ob_fight_facts_v2_0"
    vol_vah = next((e for e in summary["level_events"] if e.get("level_id") == "VOLUME_VVAH"), None)
    assert vol_vah is not None
    tpo_vah = next((e for e in summary["level_events"] if e.get("level_id") == "TPO_VAH"), None)
    assert tpo_vah is not None
    ep = tpo_vah["first_complete_above_episode"]
    assert ep is not None
    assert 150 <= (ep["duration_seconds"] or 0) <= 180
    ws = summary.get("wall_summary") or {}
    assert ws.get("book_samples_total", 0) > 100
    assert ws.get("wall_observations_total", 0) > ws.get("unique_wall_tracks", 0)
    assert (ws.get("trade_associated_decreases") or {}).get("ask", 0) > 0
    rel = {w["label"]: w for w in summary["trade_facts"]["relative_windows"]}
    w = rel["anchor_0_10m"]
    assert 2.0e6 <= w["delta_notional"] <= 3.5e6
    assert 20 <= w["price_change_bps"] <= 35
