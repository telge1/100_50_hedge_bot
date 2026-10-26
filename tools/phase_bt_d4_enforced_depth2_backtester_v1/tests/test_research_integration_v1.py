from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from config_enforcement_v1 import ResearchBacktesterConfigV1
from integration_verification_v1 import verify_sep25_golden
from research_backtester_v1 import ResearchDensityRunV1
from research_density_gateway_v1 import ResearchDensityGatewayError, fetch_density_bands_enforced

ROOT = Path(__file__).resolve().parents[3]
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
GOLDEN = (
    ROOT
    / "results/phase_bt_d3_canonical_depth2_provider_v1/golden_20260925_1100_chart_path.json"
)

requires_ssot = pytest.mark.skipif(
    not Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard").is_dir(),
    reason="SSOT dashboard path missing",
)


@requires_ssot
def test_e2e_sep25_through_research_backtester_not_direct_provider():
    """Bands must flow through ResearchDensityRunV1.collect_at_close → gateway."""
    session = ResearchDensityRunV1.from_config_path()
    snap = session.collect_at_close("2026-09-25T11:00:00Z")
    assert snap["upper_band_count"] == 39
    assert snap["lower_band_count"] == 176
    assert snap["total_band_count"] == 215
    assert GOLDEN.is_file()
    gate = verify_sep25_golden(snap, GOLDEN)
    assert gate["ok"], gate


@requires_ssot
def test_five_additional_closes_via_research_mode():
    cfg = ResearchBacktesterConfigV1.load()
    session = ResearchDensityRunV1(cfg)
    for close in cfg.additional_parity_closes:
        snap = session.collect_at_close(close)
        assert snap["total_band_count"] > 0
        assert snap["pipeline_line_id"] == cfg.expected_pipeline_line_id


@requires_ssot
def test_causal_sequence_distinct_snapshots():
    cfg = ResearchBacktesterConfigV1.load()
    session = ResearchDensityRunV1(cfg)
    hashes = []
    for close in cfg.causal_sequence_closes:
        snap = session.collect_at_close(close)
        hashes.append(json.dumps(snap, sort_keys=True))
    assert len(set(hashes)) >= 2


@requires_ssot
def test_wrong_pipeline_fail_closed(tmp_path):
    base = json.loads(
        (Path(__file__).resolve().parents[1] / "research_config_v1.json").read_text(encoding="utf-8")
    )
    base["expected_pipeline_line_id"] = "wrong_pipeline_line_id"
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    cfg = ResearchBacktesterConfigV1.load(p)
    with pytest.raises(ResearchDensityGatewayError) as ei:
        fetch_density_bands_enforced(cfg, "2026-09-25T11:00:00Z", use_parity_fixture_window=True)
    assert ei.value.code == "PIPELINE_MISMATCH"


@requires_ssot
def test_unpublished_close_fail_closed():
    cfg = ResearchBacktesterConfigV1.load()
    with pytest.raises(ResearchDensityGatewayError) as ei:
        fetch_density_bands_enforced(cfg, "2025-01-01T12:00:00Z")
    assert ei.value.code in ("OUT_OF_SCOPE", "INVALID_REQUEST")


@requires_ssot
def test_deterministic_repeat_through_research_mode():
    session_a = ResearchDensityRunV1.from_config_path()
    session_b = ResearchDensityRunV1.from_config_path()
    sa = session_a.collect_at_close("2026-09-10T14:00:00Z")
    sb = session_b.collect_at_close("2026-09-10T14:00:00Z")
    assert sa["upper_bands"] == sb["upper_bands"]
    assert sa["lower_bands"] == sb["lower_bands"]


def test_phase4d_strategy_config_unchanged():
    cfg_path = PHASE4D / "strategy_config_v2.json"
    raw = cfg_path.read_text(encoding="utf-8")
    assert '"min_depth": 2' in raw
    # BT-D4 does not wire min_depth into engine; file must remain as before integration.
    assert "density_method" not in raw


def test_phase4d_unit_regression():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(PHASE4D / "test_phase4d_strict.py"),
            "-q",
            "-k",
            "not integration",
        ],
        cwd=str(PHASE4D),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
