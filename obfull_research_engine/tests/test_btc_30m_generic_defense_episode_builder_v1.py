"""Unit tests for btc_30m_generic_defense_episode_builder_v1 (synthetic, fast)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]
WORKTREE = Path("/home/telgenbuescher/projects/orderbook_analyse_btc30m_v1")
sys.path.insert(0, str(ENGINE_ROOT / "src"))
sys.path.insert(0, str(WORKTREE.parent / "orderbook_analyse" / "src"))

from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1 import (  # noqa: E402
    CONTRACT_HASH,
    EXPECTED_CONTRACT_HASH,
    WORKTREE_ROOT,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.attack_clusters import (  # noqa: E402
    group_attack_clusters,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.config import (  # noqa: E402
    FORBIDDEN_CALC_INPUT_KEYS,
    BuilderConfig,
    assert_no_forbidden_calc_inputs,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.decision_snapshots import (  # noqa: E402
    build_decision_snapshots,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.e2e import (  # noqa: E402
    assert_worktree_imports,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.handoff import (  # noqa: E402
    GENERIC_VISIT_COUNT_NOTE,
    build_defense_handoff,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.outcomes_apply import (  # noqa: E402
    verify_contract_hash,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.pipeline import (  # noqa: E402
    run_builder,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.visits import (  # noqa: E402
    detect_all_visits_for_zones,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.walls_past_only import (  # noqa: E402
    select_ask_wall_above,
    select_bid_wall_below,
)
from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.zones import (  # noqa: E402
    load_closed_30m_mp_levels,
    zones_as_visit_clusters,
)


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def test_import_paths_under_worktree():
    """Core package modules must resolve under the clean worktree."""
    import obfull_research_engine.btc_30m_generic_defense_episode_builder_v1 as pkg
    import obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.config as cfg
    import obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.pipeline as pipe

    for m in (pkg, cfg, pipe):
        assert str(m.__file__).startswith(WORKTREE_ROOT)
    bad = assert_worktree_imports()
    bad_ours = [b for b in bad if "btc_30m_generic_defense_episode_builder_v1" in b]
    assert not bad_ours, bad_ours


def test_contract_hash_unchanged():
    assert CONTRACT_HASH == EXPECTED_CONTRACT_HASH
    assert verify_contract_hash() == EXPECTED_CONTRACT_HASH


def test_forbid_research_visit_count_and_first_touch_iso_in_config():
    with pytest.raises(ValueError):
        assert_no_forbidden_calc_inputs({"research_visit_count": 3})
    with pytest.raises(ValueError):
        assert_no_forbidden_calc_inputs({"FIRST_TOUCH_ISO": "2026-09-06T20:19:00Z"})
    with pytest.raises(ValueError):
        assert_no_forbidden_calc_inputs({"extra": {"known_episode_id": "ep:x"}})
    assert assert_no_forbidden_calc_inputs(BuilderConfig().to_dict()) == []
    for k in FORBIDDEN_CALC_INPUT_KEYS:
        assert k not in BuilderConfig().to_dict()


def test_load_zones_filter_30m_closed_only(tmp_path: Path):
    csv_path = tmp_path / "mp.csv"
    fieldnames = [
        "level_id",
        "logical_level_id",
        "level_class",
        "symbol",
        "timeframe",
        "profile_state",
        "tpo_type",
        "available_at",
        "level_price",
        "level_zone_low",
        "level_zone_high",
    ]
    rows = [
        {
            "level_id": "lvl:CLOSED:30m:TPO_VAL:1",
            "logical_level_id": "ll1",
            "level_class": "CLOSED_30M_TPO_VAL",
            "symbol": "BTCUSDT",
            "timeframe": "30m",
            "profile_state": "CLOSED",
            "tpo_type": "TPO_VAL",
            "available_at": "2026-09-06T20:00:00Z",
            "level_price": "100.0",
            "level_zone_low": "99.5",
            "level_zone_high": "100.5",
        },
        {
            "level_id": "lvl:CLOSED:1h:TPO_VAL:1",
            "logical_level_id": "ll2",
            "level_class": "CLOSED_1H_TPO_VAL",
            "symbol": "BTCUSDT",
            "timeframe": "1h",
            "profile_state": "CLOSED",
            "tpo_type": "TPO_VAL",
            "available_at": "2026-09-06T20:00:00Z",
            "level_price": "200.0",
            "level_zone_low": "199.5",
            "level_zone_high": "200.5",
        },
        {
            "level_id": "lvl:DEVELOPING:30m:TPO_VAL:1",
            "logical_level_id": "ll3",
            "level_class": "DEVELOPING_30M_TPO_VAL",
            "symbol": "BTCUSDT",
            "timeframe": "30m",
            "profile_state": "DEVELOPING",
            "tpo_type": "TPO_VAL",
            "available_at": "2026-09-06T20:00:00Z",
            "level_price": "110.0",
            "level_zone_low": "109.5",
            "level_zone_high": "110.5",
        },
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    levels = load_closed_30m_mp_levels(csv_path)
    assert len(levels) == 1
    assert levels[0]["level_id"] == "lvl:CLOSED:30m:TPO_VAL:1"
    zones = zones_as_visit_clusters(levels, [])
    assert zones[0]["timeframe"] == "30m"
    assert zones[0]["profile_state"] == "CLOSED"


def test_visit_detection_finds_multiple_visits_without_visit_count_selector():
    zone = {
        "zone_id": "z1",
        "persistent_cluster_id": "pc_test",
        "level_cluster_id": "cl_test",
        "cluster_price_low": 100.0,
        "cluster_price_high": 101.0,
        "zone_low": 100.0,
        "zone_high": 101.0,
        "zone_available_at": "2026-09-06T19:00:00Z",
        "zone_available_at_dt": _utc("2026-09-06T19:00:00Z"),
        "tpo_type": "TPO_VAL",
        "member_level_types": ["TPO_VAL"],
    }
    trades = [
        {"ts": "2026-09-06T19:05:00Z", "price": 99.0, "trade_id": "a"},
        {"ts": "2026-09-06T19:05:01Z", "price": 100.5, "trade_id": "b"},
        {"ts": "2026-09-06T19:05:30Z", "price": 102.0, "trade_id": "c"},
        {"ts": "2026-09-06T19:10:00Z", "price": 100.6, "trade_id": "d"},
        {"ts": "2026-09-06T19:10:30Z", "price": 103.0, "trade_id": "e"},
    ]
    candles = [
        {"open_time": "2026-09-06T19:06:00Z", "low": 102.0, "high": 103.0, "open": 102.0, "close": 102.5},
        {"open_time": "2026-09-06T19:07:00Z", "low": 102.0, "high": 103.0, "open": 102.5, "close": 102.0},
        {"open_time": "2026-09-06T19:08:00Z", "low": 102.0, "high": 103.0, "open": 102.0, "close": 102.0},
        {"open_time": "2026-09-06T19:09:00Z", "low": 102.0, "high": 103.0, "open": 102.0, "close": 102.0},
    ]
    visits = detect_all_visits_for_zones(
        zones=[zone],
        trade_events=trades,
        candles_1m=candles,
        window_start=_utc("2026-09-06T19:00:00Z"),
        window_end=_utc("2026-09-06T20:00:00Z"),
    )
    assert len(visits) >= 2
    assert all(v.get("used_research_visit_count_as_selector") is not True for v in visits)
    ids = [v["episode_id"] for v in visits]
    assert len(set(ids)) == len(ids)


def test_attack_cluster_gap_grouping():
    base = _utc("2026-09-06T19:00:00Z")
    visits = []
    for offset_s in (0, 30, 120):
        ts = base + timedelta(seconds=offset_s)
        visits.append(
            {
                "episode_id": f"ep:z:{int(ts.timestamp())}",
                "zone_id": "z1",
                "persistent_cluster_id": "pc",
                "first_touch_ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "episode_close_ts": (ts + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "zone_low": 1.0,
                "zone_high": 2.0,
                "zone_available_at": "2026-09-06T18:00:00Z",
            }
        )
    visits.append(
        {
            "episode_id": "ep:z2:1",
            "zone_id": "z2",
            "persistent_cluster_id": "pc2",
            "first_touch_ts": "2026-09-06T19:00:10Z",
            "episode_close_ts": "2026-09-06T19:00:15Z",
            "zone_low": 1.0,
            "zone_high": 2.0,
            "zone_available_at": "2026-09-06T18:00:00Z",
        }
    )
    clusters = group_attack_clusters(visits, gap_ms=60_000)
    assert len(clusters) == 3
    z1 = [c for c in clusters if c["zone_id"] == "z1"]
    assert sorted(c["n_visits"] for c in z1) == [1, 2]
    assert clusters == sorted(clusters, key=lambda c: c["cluster_start"])


def test_bid_ask_wall_selection_mirroring_synthetic_book():
    asks = {100.5: 1.0, 101.0: 50.0, 102.0: 2.0, 200.0: 999.0}
    bids = {99.5: 1.0, 99.0: 40.0, 98.0: 2.0, 50.0: 999.0}
    ask = select_ask_wall_above(asks, zone_high=100.0, tick_size=0.1, distance_cap_ticks=50)
    bid = select_bid_wall_below(bids, zone_low=100.0, tick_size=0.1, distance_cap_ticks=50)
    assert ask is not None and ask["wall_side"] == "ask"
    assert bid is not None and bid["wall_side"] == "bid"
    assert ask["wall_price"] == 101.0
    assert bid["wall_price"] == 99.0
    assert ask["wall_price"] > 100.0
    assert bid["wall_price"] < 100.0


def test_handoff_required_fields():
    zone = {
        "zone_id": "pc_x",
        "zone_low": 100.0,
        "zone_high": 101.0,
        "zone_available_at": "2026-09-06T19:00:00Z",
    }
    zone_touch = {
        "episode_id": "ep:pc_x:1",
        "zone_id": "pc_x",
        "zone_low": 100.0,
        "zone_high": 101.0,
        "zone_available_at": "2026-09-06T19:00:00Z",
        "exchange_event_time": "2026-09-06T19:05:00Z",
        "event_available_at": "2026-09-06T19:05:00.1Z",
        "trigger_record_id": "t1",
    }
    wall_touch = {
        "wall_id": "w_1",
        "wall_generation_id": "wg_1",
        "wall_side": "ask",
        "wall_price": 101.5,
        "replay_epoch": 1,
        "wall_generation_index": 0,
        "generation_start_exchange_time": "2026-09-06T19:04:00Z",
        "exchange_event_time": "2026-09-06T19:05:10Z",
        "event_available_at": "2026-09-06T19:05:10.1Z",
        "trigger_record_id": "t2",
        "qty_at_zone_touch": 12.0,
    }
    detection = {
        "available": True,
        "exchange_event_time": "2026-09-06T19:06:00Z",
        "event_available_at": "2026-09-06T19:06:00Z",
        "reaction_class": "TOUCH_UNRESOLVED",
        "reason": "test",
        "reaction": {"reaction_class": "TOUCH_UNRESOLVED", "reason": "test"},
    }
    h = build_defense_handoff(
        symbol="BTCUSDT",
        zone=zone,
        zone_touch=zone_touch,
        wall_touch=wall_touch,
        detection=detection,
    )
    assert h.episode_id == "ep:pc_x:1"
    assert "NOT used as a selector" in h.detector_research_visit_count_note
    assert "NOT used" in GENERIC_VISIT_COUNT_NOTE


def test_feature_outcome_path_separation(tmp_path: Path):
    cfg = BuilderConfig(
        out_root=str(tmp_path),
        run_key="gdeb1_sep_test",
        max_pilot_clusters=0,
    )
    zone = {
        "zone_id": "z_sep",
        "persistent_cluster_id": "z_sep",
        "level_cluster_id": "z_sep",
        "cluster_price_low": 100.0,
        "cluster_price_high": 101.0,
        "zone_low": 100.0,
        "zone_high": 101.0,
        "zone_available_at": "2026-09-06T19:00:00Z",
        "zone_available_at_dt": _utc("2026-09-06T19:00:00Z"),
        "tpo_type": "TPO_VAL",
        "member_level_types": ["TPO_VAL"],
        "timeframe": "30m",
        "profile_state": "CLOSED",
    }
    result = run_builder(
        cfg,
        dry_discovery_only=True,
        synthetic={"zones": [zone], "trades": [], "candles": [], "mid_events": []},
    )
    out = Path(result["out_dir"])
    feat = out / "features"
    outc = out / "outcomes"
    assert feat.is_dir()
    assert outc.is_dir()
    assert feat.resolve() != outc.resolve()
    for p in feat.rglob("*"):
        if p.is_file():
            assert "outcome_label" not in p.name
            assert "outcomes_complete" not in p.name


def test_snapshot_causality_no_future_in_features():
    zone_touch = {
        "episode_id": "ep:1",
        "exchange_event_time": "2026-09-06T19:05:00Z",
        "event_available_at": "2026-09-06T19:05:00Z",
    }
    wall_touch = {
        "exchange_event_time": "2026-09-06T19:05:05Z",
        "event_available_at": "2026-09-06T19:05:05Z",
    }
    timeline = [
        {
            "decision_time": "2026-09-06T19:05:00Z",
            "available_at": "2026-09-06T19:05:00Z",
            "event_available_at": "2026-09-06T19:05:00Z",
            "midprice": 100.0,
            "microprice": 100.0,
            "best_bid": 99.9,
            "best_ask": 100.1,
        },
        {
            "decision_time": "2026-09-06T19:05:10Z",
            "available_at": "2026-09-06T19:06:00Z",
            "event_available_at": "2026-09-06T19:06:00Z",
            "midprice": 999.0,
            "microprice": 999.0,
            "best_bid": 998.0,
            "best_ask": 1000.0,
        },
    ]
    snaps = build_decision_snapshots(
        episode_id="ep:1",
        attack_cluster_id="ac_1",
        zone_touch=zone_touch,
        wall_touch=wall_touch,
        detection={"available": False},
        timeline_rows=timeline,
        wall_side="ask",
        replay_epoch=1,
        source_manifest_hash="abc",
    )
    z0 = [
        s
        for s in snaps
        if s.get("anchor_type") == "ZONE_FIRST_TOUCH" and s.get("offset_s") == 0 and s.get("available")
    ]
    assert z0
    assert z0[0].get("midprice") != 999.0
    for s in snaps:
        if not s.get("available") or s.get("event_available_at") is None:
            continue
        if s.get("midprice") is None and s.get("best_bid") is None:
            continue
        assert _utc(s["event_available_at"]) <= _utc(s["decision_time"])


def test_headroom_keys_present_no_rule():
    from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.headroom_schema import (
        HEADROOM_RULE_ACTIVATED,
        HEADROOM_SCHEMA_KEYS,
        empty_headroom_placeholders,
    )

    assert HEADROOM_RULE_ACTIVATED is False
    ph = empty_headroom_placeholders(wall_side="ask", decision_time="2026-09-06T19:00:00Z")
    for k in HEADROOM_SCHEMA_KEYS:
        assert k in ph
    assert ph["headroom_rule_activated"] is False
    # Note may mention 0.41% only to document that it is NOT activated.
    assert "not activated" in str(ph.get("headroom_rule_note", "")).lower() or (
        "no trading rule" in str(ph.get("headroom_rule_note", "")).lower()
    )


def test_pilot_mode_effective_limit_default_20():
    cfg = BuilderConfig(pilot=True)
    assert cfg.run_mode() == "pilot"
    assert cfg.effective_cluster_limit() == 20
    assert cfg.max_clusters is None


def test_full_mode_effective_limit_unlimited():
    cfg = BuilderConfig(pilot=False, input_contract="full_run_generic_v1")
    assert cfg.run_mode() == "full"
    assert cfg.effective_cluster_limit() is None


def test_max_clusters_overrides_both_modes():
    pilot = BuilderConfig(pilot=True, max_clusters=7, max_pilot_clusters=20)
    full = BuilderConfig(pilot=False, max_clusters=3, input_contract="full_run_generic_v1")
    assert pilot.effective_cluster_limit() == 7
    assert full.effective_cluster_limit() == 3


def test_e2e_cli_pilot_sets_pilot_not_silent_full(tmp_path: Path, monkeypatch):
    from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1 import e2e as e2e_mod

    captured: dict[str, Any] = {}

    def fake_run(cfg, dry_discovery_only=False):
        captured["pilot"] = cfg.pilot
        captured["limit"] = cfg.effective_cluster_limit()
        captured["max_clusters"] = cfg.max_clusters
        out = tmp_path / "BTCUSDT" / (cfg.run_key or "gdeb1_x")
        out.mkdir(parents=True, exist_ok=True)
        return {
            "ok": True,
            "run_key": cfg.run_key,
            "out_dir": str(out),
            "run_mode": cfg.run_mode(),
            "effective_cluster_limit": cfg.effective_cluster_limit(),
            "outcome_contract_hash": "efd620a305e8252e61f03c516c69de364049153e6fb02c091caf6137e4e7e46a",
        }

    monkeypatch.setattr(e2e_mod, "run_builder", fake_run)
    rc = e2e_mod.main(["--pilot", "--run-key", "gdeb1_cli_pilot", "--out-dir", str(tmp_path)])
    assert rc == 0
    assert captured["pilot"] is True
    assert captured["limit"] == 20
    assert captured["max_clusters"] is None


def test_e2e_cli_full_with_max_clusters(tmp_path: Path, monkeypatch):
    from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1 import e2e as e2e_mod

    captured: dict[str, Any] = {}

    def fake_run(cfg, dry_discovery_only=False):
        captured["pilot"] = cfg.pilot
        captured["limit"] = cfg.effective_cluster_limit()
        captured["max_clusters"] = cfg.max_clusters
        out = tmp_path / "BTCUSDT" / (cfg.run_key or "gdeb1_x")
        out.mkdir(parents=True, exist_ok=True)
        return {
            "ok": True,
            "run_key": cfg.run_key,
            "out_dir": str(out),
            "run_mode": cfg.run_mode(),
            "effective_cluster_limit": cfg.effective_cluster_limit(),
            "outcome_contract_hash": "efd620a305e8252e61f03c516c69de364049153e6fb02c091caf6137e4e7e46a",
        }

    monkeypatch.setattr(e2e_mod, "run_builder", fake_run)
    rc = e2e_mod.main(["--max-clusters", "5", "--run-key", "gdeb1_cli_full5", "--out-dir", str(tmp_path)])
    assert rc == 0
    assert captured["pilot"] is False
    assert captured["max_clusters"] == 5
    assert captured["limit"] == 5


def test_full_mode_refuses_pilot_fixture_paths(tmp_path: Path):
    from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.input_coverage import (
        FullRunInputError,
        validate_run_inputs,
    )

    cfg = BuilderConfig(
        pilot=False,
        input_contract="full_run_generic_v1",
        out_root=str(tmp_path),
        run_key="gdeb1_refuse_fixtures",
    )
    with pytest.raises(FullRunInputError) as ei:
        validate_run_inputs(cfg, skip_disk=False)
    assert "pilot fixture" in str(ei.value).lower() or "refuses" in str(ei.value).lower()


def test_full_mode_refuses_wrong_input_contract():
    from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.input_coverage import (
        FullRunInputError,
        validate_run_inputs,
    )

    cfg = BuilderConfig(pilot=False, input_contract="pilot_fixture_v1")
    with pytest.raises(FullRunInputError) as ei:
        validate_run_inputs(cfg, skip_disk=False)
    assert "full_run_generic_v1" in str(ei.value)


def test_full_mode_refuses_empty_coverage(tmp_path: Path):
    from obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.input_coverage import (
        FullRunInputError,
        validate_run_inputs,
    )

    mp = tmp_path / "mp.csv"
    lc = tmp_path / "lc.csv"
    tr = tmp_path / "trades.jsonl"
    ca = tmp_path / "candles.jsonl"
    arch = tmp_path / "full_ob"
    arch.mkdir()
    mp.write_text("level_id,available_at\n", encoding="utf-8")
    lc.write_text("level_cluster_id\n", encoding="utf-8")
    tr.write_text("", encoding="utf-8")
    ca.write_text("", encoding="utf-8")
    cfg = BuilderConfig(
        pilot=False,
        input_contract="full_run_generic_v1",
        mp_events_path=str(mp),
        level_clusters_path=str(lc),
        trades_path=str(tr),
        candles_path=str(ca),
        full_ob_archive=str(arch),
        existing_persist_fallback="",
        out_root=str(tmp_path / "out"),
        run_key="gdeb1_empty_cov",
    )
    with pytest.raises(FullRunInputError) as ei:
        validate_run_inputs(cfg, skip_disk=False)
    msg = str(ei.value).lower()
    assert "empty" in msg or "missing" in msg or "no usable" in msg


def test_manifest_records_mode_limit_and_window(tmp_path: Path):
    cfg = BuilderConfig(
        pilot=True,
        max_pilot_clusters=20,
        out_root=str(tmp_path),
        run_key="gdeb1_manifest_mode",
        window_start="2026-09-06T19:00:00Z",
        window_end="2026-09-06T23:00:00Z",
    )
    zone = {
        "zone_id": "z_m",
        "persistent_cluster_id": "z_m",
        "level_cluster_id": "z_m",
        "cluster_price_low": 100.0,
        "cluster_price_high": 101.0,
        "zone_low": 100.0,
        "zone_high": 101.0,
        "zone_available_at": "2026-09-06T19:00:00Z",
        "zone_available_at_dt": _utc("2026-09-06T19:00:00Z"),
        "tpo_type": "TPO_VAL",
        "member_level_types": ["TPO_VAL"],
        "timeframe": "30m",
        "profile_state": "CLOSED",
    }
    result = run_builder(
        cfg,
        dry_discovery_only=True,
        synthetic={"zones": [zone], "trades": [], "candles": [], "mid_events": []},
    )
    man = json.loads((Path(result["out_dir"]) / "run_manifest.json").read_text(encoding="utf-8"))
    assert man["run_mode"] == "pilot"
    assert man["effective_cluster_limit"] == 20
    assert man["window_start"] == "2026-09-06T19:00:00Z"
    assert man["window_end"] == "2026-09-06T23:00:00Z"
    assert man["input_contract"] == "pilot_fixture_v1"
    assert result["run_mode"] == "pilot"
    assert result["effective_cluster_limit"] == 20


def test_pipeline_full_mode_no_implicit_20_cap_in_manifest(tmp_path: Path):
    cfg = BuilderConfig(
        pilot=False,
        input_contract="full_run_generic_v1",
        out_root=str(tmp_path),
        run_key="gdeb1_full_nolimit",
        # Avoid default fixture paths for uses_pilot_fixture_paths in skip_disk metadata.
        mp_events_path=str(tmp_path / "mp.csv"),
        level_clusters_path=str(tmp_path / "lc.csv"),
        trades_path=str(tmp_path / "tr.jsonl"),
        candles_path=str(tmp_path / "ca.jsonl"),
    )
    assert cfg.effective_cluster_limit() is None
    zone = {
        "zone_id": "z_f",
        "persistent_cluster_id": "z_f",
        "level_cluster_id": "z_f",
        "cluster_price_low": 100.0,
        "cluster_price_high": 101.0,
        "zone_low": 100.0,
        "zone_high": 101.0,
        "zone_available_at": "2026-09-06T19:00:00Z",
        "zone_available_at_dt": _utc("2026-09-06T19:00:00Z"),
        "tpo_type": "TPO_VAL",
        "member_level_types": ["TPO_VAL"],
        "timeframe": "30m",
        "profile_state": "CLOSED",
    }
    result = run_builder(
        cfg,
        dry_discovery_only=True,
        synthetic={"zones": [zone], "trades": [], "candles": [], "mid_events": []},
    )
    man = json.loads((Path(result["out_dir"]) / "run_manifest.json").read_text(encoding="utf-8"))
    assert man["run_mode"] == "full"
    assert man["effective_cluster_limit"] is None
    assert man["max_pilot_clusters"] == 20  # retained for audit, not applied


def test_episode1_id_not_selection_input_in_config():
    cfg = BuilderConfig(pilot=True)
    d = cfg.to_dict()
    assert "known_episode_id" not in d
    assert "FIRST_TOUCH_ISO" not in d
    assert "research_visit_count" not in d
    assert_no_forbidden_calc_inputs(cfg)


def _wall_ask():
    return {
        "wall_id": "w_ask",
        "wall_side": "ask",
        "wall_price": 101.5,
        "qty": 10.0,
        "wall_generation_id": "wg",
        "replay_epoch": 1,
        "wall_generation_index": 0,
        "generation_start_exchange_time": "2026-09-06T19:00:00Z",
    }


def _fake_walls_ok(*args, **kwargs):
    ask = _wall_ask()
    return {"ok": True, "has_any_wall": True, "ask_wall": ask, "bid_wall": None, "ask": ask, "bid": None}


def _fake_touches_ok(*args, **kwargs):
    cluster = kwargs.get("attack_cluster") or {}
    primary = cluster.get("primary_visit") or {}
    ask = kwargs.get("ask_wall") or _wall_ask()
    return {
        "zone_touch": {
            "episode_id": primary.get("episode_id"),
            "zone_id": primary.get("zone_id"),
            "zone_low": primary.get("zone_low"),
            "zone_high": primary.get("zone_high"),
            "zone_available_at": primary.get("zone_available_at"),
            "exchange_event_time": primary.get("exchange_event_time"),
            "event_available_at": primary.get("event_available_at"),
            "trigger_record_id": primary.get("trigger_record_id"),
            "first_touch_ts": primary.get("first_touch_ts"),
        },
        "wall_touches": {
            "ask": {
                **ask,
                "exchange_event_time": "2026-09-06T19:10:05Z",
                "event_available_at": "2026-09-06T19:10:05Z",
                "trigger_record_id": "wt1",
                "qty_at_zone_touch": 10.0,
            }
        },
        "detection": {"available": False},
        "book_ok": True,
        "errors": [],
    }


class _FakeHandoff:
    episode_id = "ep:z:0"
    wall_side = "ask"
    wall_price = 101.5
    wall_id = "w_ask"
    wall_generation_id = "wg"
    replay_epoch = 1
    zone_id = "z_lim"
    attack_cluster_id = "ac_0"

    def to_dict(self):
        return {"episode_id": self.episode_id, "wall_side": self.wall_side}


def _patch_enrichment_short_circuit(pipe, monkeypatch, tmp_path):
    monkeypatch.setattr(
        pipe,
        "persist_episode_book",
        lambda *a, **k: {
            "ok": True,
            "reused": True,
            "persist_dir": str(tmp_path / "book"),
            "replay": {"ok": True, "window_end": "2026-09-06T23:00:00Z", "states": []},
        },
    )
    monkeypatch.setattr(pipe, "select_walls_at_zone_touch", _fake_walls_ok)
    monkeypatch.setattr(pipe, "enrich_cluster_touches", _fake_touches_ok)
    monkeypatch.setattr(
        pipe,
        "run_wall_flow_stage",
        lambda *a, **k: {"ok": True, "timeline_rows": [], "out_dir": str(tmp_path)},
    )
    monkeypatch.setattr(
        pipe,
        "run_price_response_stage",
        lambda *a, **k: {"ok": True, "timeline_rows": [], "reclaim_status": "RECLAIM_NOT_CALIBRATED"},
    )
    monkeypatch.setattr(pipe, "run_defense_chain_stage", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(pipe, "build_decision_snapshots", lambda *a, **k: [])
    monkeypatch.setattr(
        pipe,
        "apply_outcomes_for_episode",
        lambda *a, **k: {"n_complete": 0, "n_censored": 0, "complete": [], "censored": []},
    )
    monkeypatch.setattr(pipe, "build_defense_handoff", lambda *a, **k: _FakeHandoff())


def _synth_clusters(n: int, zone_id: str = "z_lim"):
    visits = []
    clusters = []
    for i in range(n):
        v = {
            "episode_id": f"ep:z:{i}",
            "zone_id": zone_id,
            "persistent_cluster_id": zone_id,
            "zone_low": 100.0,
            "zone_high": 101.0,
            "zone_available_at": "2026-09-06T19:00:00Z",
            "visit_start": f"2026-09-06T19:1{i}:00Z",
            "visit_end": f"2026-09-06T19:1{i}:30Z",
            "first_touch_ts": f"2026-09-06T19:1{i}:00Z",
            "exchange_event_time": f"2026-09-06T19:1{i}:00Z",
            "event_available_at": f"2026-09-06T19:1{i}:00Z",
            "trigger_record_id": f"t{i}",
        }
        visits.append(v)
        clusters.append(
            {
                "attack_cluster_id": f"ac_{i}",
                "zone_id": zone_id,
                "persistent_cluster_id": zone_id,
                "zone_low": 100.0,
                "zone_high": 101.0,
                "zone_available_at": "2026-09-06T19:00:00Z",
                "cluster_start": v["visit_start"],
                "primary_visit": v,
                "visits": [v],
                "episode_ids": [v["episode_id"]],
            }
        )
    zone = {
        "zone_id": zone_id,
        "persistent_cluster_id": zone_id,
        "level_cluster_id": zone_id,
        "cluster_price_low": 100.0,
        "cluster_price_high": 101.0,
        "zone_low": 100.0,
        "zone_high": 101.0,
        "zone_available_at": "2026-09-06T19:00:00Z",
        "zone_available_at_dt": _utc("2026-09-06T19:00:00Z"),
        "tpo_type": "TPO_VAL",
        "member_level_types": ["TPO_VAL"],
        "timeframe": "30m",
        "profile_state": "CLOSED",
    }
    return visits, clusters, zone


def test_pipeline_stops_at_pilot_limit_with_injected_valid_clusters(tmp_path: Path, monkeypatch):
    """Prove pilot ceiling stops enrichment after N valid clusters (synthetic)."""
    import obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.pipeline as pipe

    cfg = BuilderConfig(
        pilot=True,
        max_pilot_clusters=2,
        out_root=str(tmp_path),
        run_key="gdeb1_limit2",
    )
    visits, clusters, zone = _synth_clusters(4)
    monkeypatch.setattr(pipe, "detect_all_visits_for_zones", lambda **kwargs: visits)
    monkeypatch.setattr(pipe, "group_attack_clusters", lambda *a, **k: clusters)
    _patch_enrichment_short_circuit(pipe, monkeypatch, tmp_path)

    result = run_builder(
        cfg,
        dry_discovery_only=False,
        synthetic={"zones": [zone], "trades": [], "candles": [], "mid_events": []},
    )
    assert result["ok"] is True
    assert result["run_mode"] == "pilot"
    assert result["effective_cluster_limit"] == 2
    assert result["n_valid_enriched"] == 2
    assert result["funnel"]["n_valid_clusters"] == 2


def test_pipeline_full_mode_processes_all_valid_without_cap(tmp_path: Path, monkeypatch):
    import obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.pipeline as pipe

    cfg = BuilderConfig(
        pilot=False,
        input_contract="full_run_generic_v1",
        out_root=str(tmp_path),
        run_key="gdeb1_full_all",
        mp_events_path=str(tmp_path / "mp.csv"),
        level_clusters_path=str(tmp_path / "lc.csv"),
        trades_path=str(tmp_path / "tr.jsonl"),
        candles_path=str(tmp_path / "ca.jsonl"),
        max_pilot_clusters=20,
    )
    visits, clusters, zone = _synth_clusters(4, zone_id="z_all")
    monkeypatch.setattr(pipe, "detect_all_visits_for_zones", lambda **kwargs: visits)
    monkeypatch.setattr(pipe, "group_attack_clusters", lambda *a, **k: clusters)
    _patch_enrichment_short_circuit(pipe, monkeypatch, tmp_path)

    result = run_builder(
        cfg,
        dry_discovery_only=False,
        synthetic={"zones": [zone], "trades": [], "candles": [], "mid_events": []},
    )
    assert result["effective_cluster_limit"] is None
    assert result["n_valid_enriched"] == 4
    assert result["funnel"]["n_valid_clusters"] == 4


def test_pipeline_full_mode_respects_max_clusters(tmp_path: Path, monkeypatch):
    import obfull_research_engine.btc_30m_generic_defense_episode_builder_v1.pipeline as pipe

    cfg = BuilderConfig(
        pilot=False,
        input_contract="full_run_generic_v1",
        max_clusters=3,
        out_root=str(tmp_path),
        run_key="gdeb1_full_max3",
        mp_events_path=str(tmp_path / "mp.csv"),
        level_clusters_path=str(tmp_path / "lc.csv"),
        trades_path=str(tmp_path / "tr.jsonl"),
        candles_path=str(tmp_path / "ca.jsonl"),
    )
    visits, clusters, zone = _synth_clusters(5, zone_id="z_m3")
    monkeypatch.setattr(pipe, "detect_all_visits_for_zones", lambda **kwargs: visits)
    monkeypatch.setattr(pipe, "group_attack_clusters", lambda *a, **k: clusters)
    _patch_enrichment_short_circuit(pipe, monkeypatch, tmp_path)

    result = run_builder(
        cfg,
        dry_discovery_only=False,
        synthetic={"zones": [zone], "trades": [], "candles": [], "mid_events": []},
    )
    assert result["effective_cluster_limit"] == 3
    assert result["n_valid_enriched"] == 3
