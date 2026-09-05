"""Tests for OB200 hour-boundary seed resolution."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from research.btc_doge_research.config import OB200_ROOT
from research.btc_doge_research.ob200_boundary import (
    PARTIAL_TRUE_GAP,
    Ob200FileIndex,
    audit_raw_file,
    collect_ob200_snapshots,
)
from research.btc_doge_research.ob200_segments import build_ob200_segments_from_discovery
from research.btc_doge_research.source_file_registry import load_source_file


def test_zero_duration_not_import_segment():
    row = {
        "symbol": "BTCUSDT",
        "segment_start": "2026-08-27T06:00:00Z",
        "segment_end": "2026-08-27T06:00:00Z",
        "relative_path": "BTCUSDT/2026/08/27/stub.zst",
        "source_fingerprint": "fp",
        "zero_duration": True,
        "bytes": 1,
    }
    assert build_ob200_segments_from_discovery([row]) == []


def test_full_hour_links_boundary_auxiliary():
    stub = {
        "symbol": "BTCUSDT",
        "segment_start": "2026-08-27T06:00:00Z",
        "segment_end": "2026-08-27T06:00:00Z",
        "relative_path": "BTCUSDT/2026/08/27/stub.zst",
        "source_fingerprint": "stubfp",
        "zero_duration": True,
        "bytes": 1,
    }
    full = {
        "symbol": "BTCUSDT",
        "segment_start": "2026-08-27T06:00:00Z",
        "segment_end": "2026-08-27T07:00:00Z",
        "relative_path": "BTCUSDT/2026/08/27/full.zst",
        "source_fingerprint": "fullfp",
        "zero_duration": False,
        "bytes": 100,
    }
    segs = build_ob200_segments_from_discovery([stub, full])
    assert len(segs) == 1
    assert segs[0]["boundary_auxiliary_path"] == stub["relative_path"]


@pytest.mark.skipif(
    not (OB200_ROOT / "BTCUSDT/2026/08/27/BTCUSDT_20260827T060000Z_20260827T070000Z_ob200_v3.zst").is_file(),
    reason="Aug 27 OB200 fixture not present",
)
def test_aug27_hour_missing_second_is_mid_hour_gap():
    path = OB200_ROOT / "BTCUSDT/2026/08/27/BTCUSDT_20260827T060000Z_20260827T070000Z_ob200_v3.zst"
    source = load_source_file(path, OB200_ROOT)
    start = datetime(2026, 8, 27, 6, tzinfo=timezone.utc)
    end = datetime(2026, 8, 27, 7, tzinfo=timezone.utc)
    files = [
        {
            "symbol": "BTCUSDT",
            "segment_start": "2026-08-27T06:00:00Z",
            "segment_end": "2026-08-27T06:00:00Z",
            "relative_path": "BTCUSDT/2026/08/27/BTCUSDT_20260827T060000Z_20260827T060000Z_ob200_v3.zst",
            "source_fingerprint": "x",
            "zero_duration": True,
        },
        {
            "symbol": "BTCUSDT",
            "segment_start": "2026-08-27T05:00:00Z",
            "segment_end": "2026-08-27T06:00:00Z",
            "relative_path": "BTCUSDT/2026/08/27/BTCUSDT_20260827T050000Z_20260827T060000Z_ob200_v3.zst",
            "source_fingerprint": "y",
            "zero_duration": False,
        },
    ]
    index = Ob200FileIndex.from_discovery(files)
    collected = collect_ob200_snapshots(
        source=source, symbol="BTCUSDT", start=start, end=end, index=index
    )
    assert start in collected.by_second
    assert collected.classification == PARTIAL_TRUE_GAP
    assert "2026-08-27T06:42:23Z" in collected.source_gaps
    assert collected.boundary_seed is None


@pytest.mark.skipif(
    not (OB200_ROOT / "BTCUSDT/2026/08/27/BTCUSDT_20260827T060000Z_20260827T060000Z_ob200_v3.zst").is_file(),
    reason="Aug 27 OB200 fixture not present",
)
def test_boundary_stub_audit_is_single_delta():
    path = OB200_ROOT / "BTCUSDT/2026/08/27/BTCUSDT_20260827T060000Z_20260827T060000Z_ob200_v3.zst"
    audit = audit_raw_file(path)
    assert audit["record_count"] == 1
    assert audit["record_types"] == {"delta": 1}
    assert audit["writer_meaning"] == "hour_rollover_boundary_stub"
