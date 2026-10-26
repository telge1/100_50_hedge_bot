"""Live provider vs cache — skipped unless RUN_BT_D8_LIVE=1."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BT_D8 = Path(__file__).resolve().parents[1]
if str(BT_D8) not in sys.path:
    sys.path.insert(0, str(BT_D8))

from parity_runner_v1 import parity_at_close, parity_against_golden  # noqa: E402
from research_backtester_v1 import ResearchDensityRunV1  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("RUN_BT_D8_LIVE") != "1", reason="set RUN_BT_D8_LIVE=1")


def test_live_provider_cache_readback_golden(tmp_path):
    close = "2026-09-25T11:00:00Z"
    r = parity_at_close(close, bundle_parent=tmp_path)
    assert r["ok"] is True


def test_live_golden_215_bands():
    session = ResearchDensityRunV1.from_config_path()
    close = session.config.parity_fixture_decision_closes[0]
    snap = session.collect_at_close(close)
    gcmp = parity_against_golden(snap, session.config.golden_reference_path)
    assert gcmp["ok"] is True
    assert gcmp["band_compare"]["expected_count"] == 215
