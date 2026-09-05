"""Tests for OB200 file-based segments and backfill source validation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from research.btc_doge_research.backfill_plan import build_backfill_plan
from research.btc_doge_research.full_history_contracts import (
    LIVE_PRODUCER_ID,
    LIVE_RAW_FROM,
    SHADOW_ARCHIVE_PRODUCER_ID,
    segment_build_id,
)
from research.btc_doge_research.full_history_runner import (
    _attempt_load_segment,
    _filter_plan,
    _plan_row_to_context,
    _ready_exists,
)
from research.btc_doge_research.ob200_segments import (
    SourceVanishedError,
    build_ob200_segments_from_discovery,
    ob_segment_from_file,
    validate_source_file_at_batch,
)


def _file_row(
    *,
    start: str,
    end: str,
    relative_path: str,
    fingerprint: str = "abc123",
    zero_duration: bool = False,
) -> dict:
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return {
        "symbol": "BTCUSDT",
        "segment_start": start,
        "segment_end": end,
        "utc_day": start_dt.strftime("%Y-%m-%d"),
        "relative_path": relative_path,
        "source_fingerprint": fingerprint,
        "zero_duration": zero_duration,
        "bytes": 1000,
    }


def test_partial_first_hour_clips_to_live_raw_from():
    row = _file_row(
        start="2026-08-24T22:47:53Z",
        end="2026-08-24T23:00:00Z",
        relative_path="BTCUSDT/2026/08/24/BTCUSDT_20260824T224753Z_20260824T230000Z_ob200_v3.zst",
    )
    seg = ob_segment_from_file(row)
    assert seg is not None
    assert seg["segment_start"] == LIVE_RAW_FROM.isoformat().replace("+00:00", "Z")
    assert seg["segment_end"] == "2026-08-24T23:00:00Z"
    assert seg["expected_rows"] == 726
    assert seg["status"] == "PARTIAL"
    assert seg["source_path"] == row["relative_path"]


def test_zero_duration_file_skipped():
    row = _file_row(
        start="2026-08-24T23:00:00Z",
        end="2026-08-24T23:00:00Z",
        relative_path="BTCUSDT/2026/08/24/BTCUSDT_20260824T230000Z_20260824T230000Z_ob200_v3.zst",
        zero_duration=True,
    )
    assert ob_segment_from_file(row) is None
    assert build_ob200_segments_from_discovery([row]) == []


def test_full_hour_uses_real_filename_not_synthetic():
    row = _file_row(
        start="2026-08-24T23:00:00Z",
        end="2026-08-25T00:00:00Z",
        relative_path="BTCUSDT/2026/08/24/BTCUSDT_20260824T230000Z_20260825T000000Z_ob200_v3.zst",
    )
    seg = ob_segment_from_file(row)
    assert seg is not None
    assert "T220000Z" not in seg["source_path"]
    assert seg["expected_rows"] == 3600
    assert seg["status"] == "READY"


def test_build_id_includes_concrete_source_path():
    start = datetime(2026, 8, 24, 22, 47, 54, tzinfo=timezone.utc)
    end = datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc)
    path = "BTCUSDT/2026/08/24/BTCUSDT_20260824T224753Z_20260824T230000Z_ob200_v3.zst"
    with_path = segment_build_id(
        "BTCUSDT", "OB200", start, end, LIVE_PRODUCER_ID, "fp1", source_path=path
    )
    without_path = segment_build_id(
        "BTCUSDT", "OB200", start, end, LIVE_PRODUCER_ID, "fp1"
    )
    assert with_path != without_path


def test_missing_source_at_validate_without_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "research.btc_doge_research.config.OB200_ROOT",
        tmp_path,
    )
    row = {
        "source_path": "BTCUSDT/missing.zst",
        "source_fingerprint": "",
    }
    with pytest.raises(FileNotFoundError):
        validate_source_file_at_batch(row)


def test_vanished_fingerprinted_source_is_global_stop(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "research.btc_doge_research.config.OB200_ROOT",
        tmp_path,
    )
    row = {
        "source_path": "BTCUSDT/vanished.zst",
        "source_fingerprint": "deadbeef" * 8,
    }
    with pytest.raises(SourceVanishedError):
        validate_source_file_at_batch(row)


def test_validate_source_file_at_batch_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "research.btc_doge_research.config.OB200_ROOT",
        tmp_path,
    )
    rel = "BTCUSDT/2026/08/24/test.zst"
    path = tmp_path / rel
    path.parent.mkdir(parents=True)
    path.write_bytes(b"payload")

    with patch("research.btc_doge_research.source_file_registry.load_source_file") as load:
        source = MagicMock()
        source.relative_path = rel
        source.fingerprint = "fp" * 32
        source.segment_start = datetime(2026, 8, 24, 23, tzinfo=timezone.utc)
        source.segment_end = datetime(2026, 8, 25, 0, tzinfo=timezone.utc)
        load.return_value = source
        out = validate_source_file_at_batch({"source_path": rel, "source_fingerprint": source.fingerprint})
    assert out["relative_path"] == rel


def test_local_source_gap_does_not_raise(monkeypatch):
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-24T22:47:54Z",
        "segment_end": "2026-08-24T23:00:00Z",
        "producer_id": LIVE_PRODUCER_ID,
        "source_path": "BTCUSDT/missing.zst",
        "source_fingerprint": "",
        "source_semantics": "raw_ob200_event_time_eos_v1",
        "expected_rows": 726,
    }
    client = MagicMock()
    with patch(
        "research.btc_doge_research.full_history_runner.validate_source_file_at_batch",
        side_effect=FileNotFoundError("missing"),
    ):
        result = _attempt_load_segment(client, row, datetime.now(timezone.utc))
    assert result["status"] == "FAILED"
    assert result["reason"] == "SOURCE_GAP"


def test_plan_from_mocked_discovery_uses_real_paths():
    partial = _file_row(
        start="2026-08-24T22:47:53Z",
        end="2026-08-24T23:00:00Z",
        relative_path="BTCUSDT/2026/08/24/BTCUSDT_20260824T224753Z_20260824T230000Z_ob200_v3.zst",
    )
    full = _file_row(
        start="2026-08-24T23:00:00Z",
        end="2026-08-25T00:00:00Z",
        relative_path="BTCUSDT/2026/08/24/BTCUSDT_20260824T230000Z_20260825T000000Z_ob200_v3.zst",
        fingerprint="fullfp",
    )
    segments = build_ob200_segments_from_discovery([partial, full])
    with patch("research.btc_doge_research.modality_coverage.build_source_discovery") as disc:
        disc.return_value = {"ob200_files": [partial, full]}
        with patch("research.btc_doge_research.modality_coverage.connect") as conn:
            client = MagicMock()
            conn.return_value = client
            with patch(
                "research.btc_doge_research.modality_coverage._ch_day_metrics",
                return_value={
                    "trade_count": 0,
                    "oi_count": 0,
                    "oi_unique": 0,
                    "candle_count": 0,
                    "liq_count": 0,
                },
            ):
                plan = build_backfill_plan()
    ob_rows = [r for r in plan if r["modality"] == "OB200" and r.get("import_eligible")]
    paths = {r["source_path"] for r in ob_rows}
    assert "BTCUSDT/2026/08/24/BTCUSDT_20260824T224753Z_20260824T230000Z_ob200_v3.zst" in paths
    assert all("T220000Z_20260824T230000Z" not in p for p in paths)


def test_context_carries_source_path_and_expected_rows():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-24T23:00:00Z",
        "segment_end": "2026-08-25T00:00:00Z",
        "producer_id": SHADOW_ARCHIVE_PRODUCER_ID,
        "source_path": "BTCUSDT/2026/08/24/BTCUSDT_20260824T230000Z_20260825T000000Z_ob200_v3.zst",
        "source_fingerprint": "fp",
        "source_semantics": "raw_ob200_event_time_eos_v1",
        "expected_rows": 3600,
    }
    ctx = _plan_row_to_context(row)
    assert ctx.source_path == row["source_path"]
    assert ctx.expected_rows == 3600


def test_resume_ready_skip(monkeypatch):
    row = {
        "symbol": "BTCUSDT",
        "modality": "PUBLIC_TRADES",
        "segment_start": "2026-07-19T00:00:00Z",
        "segment_end": "2026-07-20T00:00:00Z",
        "producer_id": "CLICKHOUSE_CANONICAL",
        "source_fingerprint": "fp",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
    }
    with patch(
        "research.btc_doge_research.full_history_runner._ready_exists",
        return_value=True,
    ):
        client = MagicMock()
        ctx = _plan_row_to_context(row)
        assert _ready_exists(client, ctx.batch_id, ctx.build_id) is True
