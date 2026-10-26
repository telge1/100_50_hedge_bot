"""Provider (fixture) vs validated_cache must yield identical BT-D7 outputs."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D8 = Path(__file__).resolve().parents[1]
for p in (str(BT_D6), str(BT_D7), str(BT_D8), str(ROOT / "tools/phase4d_pool_landscape_backtester_v2")):
    if p not in sys.path:
        sys.path.insert(0, p)

from cache_builder_v1 import build_cache_bundle  # noqa: E402
from config_validate_v1 import load_and_validate_config  # noqa: E402
from density_bridge_v1 import FixtureDensitySessionV1, create_density_session  # noqa: E402
from short_research_engine_v1 import run_short_research_backtest  # noqa: E402

FIXTURE = BT_D7 / "fixtures/synthetic_integration_v1.json"
CFG = BT_D7 / "configs/synthetic_fixture_v1.json"


def _enrich_snapshot(close: str, raw: dict) -> dict:
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


def test_fixture_provider_matches_cache_backtest(tmp_path):
    fix = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snaps = {k: _enrich_snapshot(k, v) for k, v in fix["snapshots"].items()}
    bundle_dir = tmp_path / "fixture_bundle"
    build_cache_bundle(
        bundle_dir=bundle_dir,
        decision_closes=sorted(snaps.keys()),
        collect_fn=lambda c: snaps[c],
    )

    cfg = load_and_validate_config(CFG)
    cfg_cache = deepcopy(cfg)
    cfg_cache["density"]["density_source"] = "validated_cache"
    cfg_cache["density"]["validated_cache"] = {"bundle_path": str(bundle_dir)}

    r_fix = run_short_research_backtest(cfg, FixtureDensitySessionV1(fix["snapshots"]), fix["prices"])
    r_cache = run_short_research_backtest(cfg_cache, create_density_session(cfg_cache), fix["prices"])

    assert r_fix["signals"] == r_cache["signals"]
    assert r_fix["orders"] == r_cache["orders"]
    assert r_fix["trades"] == r_cache["trades"]
    assert r_fix["pnl"] == r_cache["pnl"]
