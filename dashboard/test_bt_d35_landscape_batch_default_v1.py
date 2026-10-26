"""BT-D35 — Phase 5S batch prefill default for landscape window."""

from __future__ import annotations

import json
import os

import pytest

from pd1_landscape_viewer_v1.historical_window_v1 import (
    _landscape_phase5s_prefill_enabled,
    build_landscape_window,
)


def test_landscape_batch_prefill_default_on():
    os.environ.pop("PD1_LANDSCAPE_BATCH_READER", None)
    os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    assert _landscape_phase5s_prefill_enabled() is True


def test_landscape_batch_prefill_opt_out():
    os.environ["PD1_LANDSCAPE_BATCH_READER"] = "0"
    assert _landscape_phase5s_prefill_enabled() is False
    os.environ.pop("PD1_LANDSCAPE_BATCH_READER", None)


@pytest.mark.skipif(
    os.environ.get("BT_D35_SLOW_PARITY", "").strip() != "1",
    reason="set BT_D35_SLOW_PARITY=1 for CH-backed legacy vs batch parity",
)
def test_pools_parity_legacy_per_close_vs_phase5s_batch():
    """Sep-11 8h reference: per-close loader vs default batch prefill."""
    window = ("2026-09-11T07:34:51Z", "2026-09-11T15:34:51Z")

    os.environ["PD1_LANDSCAPE_BATCH_READER"] = "0"
    os.environ["HISTORICAL_LMV2_BATCH_READER"] = "0"
    legacy = build_landscape_window(
        window[0], window[1], timeframe="5m", pool_detail=1, compute_mode="exact_origin_v1"
    )

    os.environ.pop("PD1_LANDSCAPE_BATCH_READER", None)
    os.environ.pop("HISTORICAL_LMV2_BATCH_READER", None)
    batch = build_landscape_window(
        window[0], window[1], timeframe="5m", pool_detail=1, compute_mode="exact_origin_v1"
    )

    assert legacy["provenance"]["phase5s_batch_reader_active"] is False
    assert batch["provenance"]["phase5s_batch_reader_active"] is True
    assert json.dumps(legacy["pools"], sort_keys=True) == json.dumps(batch["pools"], sort_keys=True)
