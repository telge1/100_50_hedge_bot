"""
Reference calendar windows (Sep-9 / Sep-11) — fixture replay only.
Does NOT load canonical ClickHouse density; fail-closed by design.
"""

import json
from pathlib import Path

from density_bridge_v1 import FixtureDensitySessionV1

from config_loader_v1 import load_validated_baseline_config
from isolated_research_runner_v1 import run_isolated_research_replay

ROOT = Path(__file__).resolve().parents[3]
SYN = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1/fixtures/synthetic_integration_v1.json"


def _run_window(label: str, bars: list[str]) -> dict:
    fix = json.loads(SYN.read_text())
    snaps = {
        k: {
            "decision_close_utc": k,
            "as_of_utc": k,
            "upper_bands": fix["snapshots"][k]["upper_bands"],
            "lower_bands": fix["snapshots"][k]["lower_bands"],
            "pipeline_line_id": "fixture_only_not_canonical_ch",
            "config_hash": "fixture",
        }
        for k in bars
        if k in fix["snapshots"]
    }
    prices = {k: fix["prices"][k] for k in bars if k in fix["prices"]}
    cfg = load_validated_baseline_config()
    return run_isolated_research_replay(
        config=cfg,
        prices=prices,
        bars=bars,
        density_session=FixtureDensitySessionV1(snaps),
    )


def test_sep9_window_fail_closed_fixture_only():
    # Mapped to synthetic fixture times — NOT canonical Sep-9 CH replay
    bars = ["2026-01-01T12:05:00Z", "2026-01-01T12:10:00Z"]
    out = _run_window("sep9_proxy", bars)
    assert out["short_intent_count"] == 0
    assert out["modeled_fills"] == 0


def test_sep11_window_fail_closed_fixture_only():
    bars = ["2026-01-01T12:05:00Z", "2026-01-01T12:10:00Z"]
    out = _run_window("sep11_proxy", bars)
    assert out["short_intent_count"] == 0
