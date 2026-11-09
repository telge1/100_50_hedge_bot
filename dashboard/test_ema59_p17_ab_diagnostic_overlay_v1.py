"""P17AB — Research chart overlay from P17AA executables."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

POOLS_DEFAULT = Path(__file__).resolve().parents[2] / "pools+ob+delta_bot"


@pytest.fixture
def pools_root() -> Path:
    root = Path(os.environ.get("POOLS_OB_DELTA_BOT_ROOT") or POOLS_DEFAULT)
    os.environ["POOLS_OB_DELTA_BOT_ROOT"] = str(root)
    return root


def test_p17aa_json_present(pools_root: Path) -> None:
    from research_charts.ema59_p17_ab_diagnostic_overlay_v1 import p17aa_results_path

    assert p17aa_results_path(pools_root).is_file()


def test_p17ab_overlay_counts(pools_root: Path) -> None:
    from research_charts.ema59_p17_ab_diagnostic_overlay_v1 import (
        FIRST_IN_EPISODE_UTCS,
        build_overlay_bundle,
        load_p17aa_executables,
    )

    all9 = build_overlay_bundle(filter_mode="all", root=pools_root)
    first5 = build_overlay_bundle(filter_mode="first_only", root=pools_root)
    assert len(load_p17aa_executables(pools_root)) == 9
    assert len(all9["position_specs"]) == 9
    assert len(first5["position_specs"]) == 5
    assert len(FIRST_IN_EPISODE_UTCS) == 5


def test_x04_x07_levels(pools_root: Path) -> None:
    from research_charts.ema59_p17_ab_diagnostic_overlay_v1 import load_p17aa_executables

    rows = load_p17aa_executables(pools_root)
    x04 = next(r for r in rows if r["entry_utc"] == "2026-07-07T11:10:00Z")
    assert x04["cross_id"] == "6M-X-04"
    assert abs(float(x04["entry_price"]) - 0.07448) < 1e-5
    assert abs(float(x04["sl_price"]) - 0.07359) < 1e-4
    assert abs(float(x04["upper_lo"]) - 0.07558) < 1e-5
    x07 = next(r for r in rows if r["entry_utc"] == "2026-07-08T09:25:00Z")
    assert x07["cross_id"] == "6M-X-07"
    assert abs(float(x07["entry_price"]) - 0.07116) < 1e-5


def test_workspace_import_first_only(pools_root: Path) -> None:
    from research_charts.workspace_session import reset_workspace_for_tests

    ws = reset_workspace_for_tests()
    snap = ws.set_p17_ab_diagnostic_overlay(True, "DOGEUSDT", filter_mode="first_only")
    assert snap.get("p17_ab_diagnostic", {}).get("loaded_positions") == 5
    snap2 = ws.set_p17_ab_diagnostic_overlay(False, "DOGEUSDT")
    assert snap2.get("p17_ab_diagnostic", {}).get("visible") is False
