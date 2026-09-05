"""Lean vs --heavy-detail-csv output wiring (no calculation changes)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from research.btc_ob_fight.config import RunConfig
from research.btc_ob_fight.reporting import write_all_outputs

HEAVY_CSVS = (
    "wall_observations.csv",
    "wall_tracks.csv",
    "wall_transitions.csv",
    "wall_trade_matches.csv",
    "wall_events.csv",
    "same_timestamp_multistate_groups.csv",
    "edge_book_coverage.csv",
    "edge_region_depth_samples.csv",
    "edge_region_consumption_events.csv",
    "exact_refill_events.csv",
    "nearby_liquidity_increase_events.csv",
)


def _payload() -> dict:
    return {
        "summary": {"schema_version": "btc_ob_fight_facts_v2_0", "analysis_status": "FACTS_READY_RULES_UNFROZEN"},
        "manifest": {"rules_frozen": False},
        "coverage": {"eligibility": {"eligibility_status": "DATA_COMPLETE"}},
        "profiles": {},
        "level_events": [],
        "trade_buckets": [],
        "wall_facts": [{"side": "ASK", "event": "x"}],
        "wall_bundle": {
            "observations": [{"id": 1}],
            "tracks": [{"id": "t1"}],
            "transitions": [{"id": "tr1"}],
            "trade_matches": [{"id": "m1"}],
            "summary": {"book_samples_total": 3},
        },
        "oi_liq": {"liquidation_count": 0},
        "reasons": [{"code": "R1", "severity": "info"}],
        "german": [{"code": "R1", "text": "de"}],
        "fight_facts": {"manifest": {"edge_consumption_count": 1}, "fight_episodes": [], "fight_episode_summary": []},
        "sequence_validation": {
            "verdict": "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY",
            "fight_sequence_summary": {"canonical_outside_count": 1},
            "same_timestamp_multistate_groups": [{"ts": "2026-08-31T19:00:00Z"}],
            "edge_book_coverage": [{"scope": "EXACT_LEVEL_TICK"}],
            "edge_region_depth_samples": [{"sample_index": 0}],
            "edge_region_consumption_events": [{"scope": "TPO_EDGE_BIN"}],
            "exact_refill_events": [{"id": "rf1"}],
            "nearby_liquidity_increase_events": [{"side": "ASK"}],
        },
    }


def _write(run_dir: Path, *, heavy: bool) -> None:
    kwargs = dict(_payload())
    write_all_outputs(run_dir, heavy_detail_csv=heavy, **kwargs)


def test_default_lean_run_omits_heavy_csvs(tmp_path: Path):
    _write(tmp_path, heavy=False)
    for name in HEAVY_CSVS:
        assert not (tmp_path / name).exists(), name
    assert (tmp_path / "wall_detail_io.json").exists()
    assert (tmp_path / "edge_detail_io.json").exists()
    assert (tmp_path / "summary.json").exists()
    assert (tmp_path / "wall_summary.json").exists()
    assert (tmp_path / "fight_sequence_summary.json").exists()


def test_heavy_flag_writes_all_heavy_artifacts(tmp_path: Path):
    _write(tmp_path, heavy=True)
    for name in HEAVY_CSVS:
        path = tmp_path / name
        assert path.exists(), name
        assert path.stat().st_size > 0, name
    assert not (tmp_path / "wall_detail_io.json").exists()
    assert not (tmp_path / "edge_detail_io.json").exists()


def test_summary_golden_parity_identical_lean_vs_heavy(tmp_path: Path):
    lean = tmp_path / "lean"
    heavy = tmp_path / "heavy"
    lean.mkdir()
    heavy.mkdir()
    _write(lean, heavy=False)
    _write(heavy, heavy=True)
    assert (lean / "summary.json").read_text(encoding="utf-8") == (heavy / "summary.json").read_text(
        encoding="utf-8"
    )
    assert (lean / "fight_sequence_summary.json").read_text(encoding="utf-8") == (
        heavy / "fight_sequence_summary.json"
    ).read_text(encoding="utf-8")
    assert (lean / "wall_summary.json").read_text(encoding="utf-8") == (heavy / "wall_summary.json").read_text(
        encoding="utf-8"
    )
    assert (lean / "fight_facts_manifest.json").read_text(encoding="utf-8") == (
        heavy / "fight_facts_manifest.json"
    ).read_text(encoding="utf-8")


def test_research_db_runner_passes_cfg_flag():
    import inspect

    from research.btc_ob_fight.research_db_cli import run_research_db_analysis

    src = inspect.getsource(run_research_db_analysis)
    assert "heavy_detail_csv=cfg.heavy_detail_csv" in src
    assert "heavy_detail_csv=False" not in src


def test_runconfig_default_heavy_false():
    cfg = RunConfig(
        symbol="BTCUSDT",
        anchor=datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        before_minutes=30,
        after_minutes=30,
        out_root=Path("/tmp"),
    )
    assert cfg.heavy_detail_csv is False
