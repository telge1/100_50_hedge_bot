import json
from pathlib import Path

from density_bridge_v1 import FixtureDensitySessionV1

from config_loader_v1 import load_validated_baseline_config
from isolated_research_runner_v1 import run_isolated_research_replay

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1/fixtures/synthetic_integration_v1.json"


def _enrich(close: str, raw: dict) -> dict:
    return {
        "decision_close_utc": close,
        "as_of_utc": close,
        "upper_bands": raw["upper_bands"],
        "lower_bands": raw["lower_bands"],
        "upper_band_count": len(raw["upper_bands"]),
        "lower_band_count": len(raw["lower_bands"]),
        "total_band_count": len(raw["upper_bands"]) + len(raw["lower_bands"]),
        "pipeline_line_id": "test_pipeline_fixture_v1",
        "config_hash": "fixture_config_hash_v1",
        "data_provenance": "synthetic_fixture",
        "provider_manifest": {},
    }


def test_default_replay_zero_intents():
    fix = json.loads(FIXTURE.read_text())
    snaps = {k: _enrich(k, v) for k, v in fix["snapshots"].items()}
    cfg = load_validated_baseline_config()
    cfg["backtest"]["start_utc"] = "2026-01-01T12:00:00Z"
    cfg["backtest"]["end_utc"] = "2026-01-01T12:15:00Z"
    cfg["backtest"]["end_utc_exclusive"] = False
    bars = sorted(fix["prices"].keys())
    out = run_isolated_research_replay(
        config=cfg,
        prices=fix["prices"],
        bars=bars,
        density_session=FixtureDensitySessionV1(snaps),
    )
    assert out["short_intent_count"] == 0
    assert out["pending_orders_created"] == 0
    assert out["modeled_fills"] == 0


def test_provenance_retained_in_gate_decisions():
    fix = json.loads(FIXTURE.read_text())
    snaps = {k: _enrich(k, v) for k, v in fix["snapshots"].items()}
    cfg = load_validated_baseline_config()
    bars = ["2026-01-01T12:05:00Z"]
    out = run_isolated_research_replay(
        config=cfg,
        prices=fix["prices"],
        bars=bars,
        density_session=FixtureDensitySessionV1(snaps),
    )
    row = out["gate_decisions"][0]
    assert row["density_provenance"]["pipeline_line_id"] == "test_pipeline_fixture_v1"
