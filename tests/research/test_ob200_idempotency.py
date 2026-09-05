"""Idempotency and batch-claim guards for OB200 / full-history reimports."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from research.btc_doge_research.contracts import TARGET_DATABASE
from research.btc_doge_research.full_history_runner import (
    CLAIM_STALE_AFTER,
    _batch_status_for_result,
    _foreign_build_conflict,
    _load_segment,
    _terminal_exists,
    decide_claim_winner,
)
from research.btc_doge_research.full_history_contracts import ModalityContractError
from research.btc_doge_research.segment_loader import SegmentContext, load_ob_file, load_segment


def _ctx(**overrides):
    base = dict(
        symbol="BTCUSDT",
        modality="OB200",
        segment_start=datetime(2026, 8, 27, 6, tzinfo=timezone.utc),
        segment_end=datetime(2026, 8, 27, 7, tzinfo=timezone.utc),
        batch_id="fh:BTCUSDT:OB200:20260827T060000Z:20260827T070000Z:BYBIT_OB200_",
        build_id="a" * 64,
        contract_version="btc_doge_research_full_history_v1",
        producer_id="BYBIT_OB200_SHADOW_ARCHIVE_V3",
        source_semantics_version="raw_ob200_event_time_eos_v1",
        source_fingerprint="f" * 64,
        source_path="BTCUSDT/2026/08/27/x.zst",
        expected_rows=3600,
    )
    base.update(overrides)
    return SegmentContext(**base)


def test_decide_claim_winner_single_writer():
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    a = (now, "aaa")
    b = (now, "bbb")
    assert decide_claim_winner([b, a], now=now) == "aaa"


def test_decide_claim_winner_stale_allows_newest_resume():
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    old = (now - CLAIM_STALE_AFTER - timedelta(minutes=1), "oldtoken")
    new = (now - timedelta(minutes=1), "newtoken")
    assert decide_claim_winner([old, new], now=now) == "newtoken"


def test_batch_status_partial_preserved():
    ctx = _ctx()
    assert _batch_status_for_result(ctx, {"coverage_status": "PARTIAL", "rows": 3599}) == "PARTIAL"
    assert _batch_status_for_result(ctx, {"coverage_status": "COMPLETE", "rows": 3600}) == "READY"


def test_identical_reimport_skips_when_terminal_ready():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-31T18:00:00Z",
        "segment_end": "2026-08-31T19:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "fp",
        "source_path": "p.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
    }
    with (
        patch("research.btc_doge_research.full_history_runner._foreign_build_conflict", return_value=None),
        patch("research.btc_doge_research.full_history_runner._terminal_exists", return_value=True),
        patch("research.btc_doge_research.full_history_runner.load_segment") as load,
    ):
        out = _load_segment(MagicMock(), row, datetime.now(timezone.utc))
        assert out["status"] == "IDEMPOTENT_SKIP"
        assert out["reason"] == "TERMINAL_EXISTS"
        load.assert_not_called()


def test_partial_reimport_skips_when_terminal_partial():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-27T06:00:00Z",
        "segment_end": "2026-08-27T07:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "fp",
        "source_path": "p.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
        "expected_rows": 3600,
    }
    with (
        patch("research.btc_doge_research.full_history_runner._foreign_build_conflict", return_value=None),
        patch("research.btc_doge_research.full_history_runner._terminal_exists", return_value=True),
        patch("research.btc_doge_research.full_history_runner.load_segment") as load,
    ):
        out = _load_segment(MagicMock(), row, datetime.now(timezone.utc))
        assert out["status"] == "IDEMPOTENT_SKIP"
        load.assert_not_called()


def test_audit_smoke_reimport_skips_existing_rows_without_insert():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-27T06:00:00Z",
        "segment_end": "2026-08-27T07:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "fp",
        "source_path": "p.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
        "expected_rows": 3600,
    }
    with (
        patch("research.btc_doge_research.full_history_runner._foreign_build_conflict", return_value=None),
        patch("research.btc_doge_research.full_history_runner._terminal_exists", return_value=False),
        patch("research.btc_doge_research.full_history_runner._existing_build_rows", return_value=3599),
        patch(
            "research.btc_doge_research.full_history_runner._coverage_status_from_existing",
            return_value="PARTIAL",
        ),
        patch("research.btc_doge_research.full_history_runner._finalize_terminal") as fin,
        patch("research.btc_doge_research.full_history_runner.load_segment") as load,
    ):
        fin.return_value = {
            "status": "PARTIAL",
            "symbol": "BTCUSDT",
            "modality": "OB200",
            "segment_start": row["segment_start"],
            "batch_id": "b",
            "build_id": "g",
            "result": {"rows": 3599, "coverage_status": "PARTIAL"},
            "output_fingerprint": "0" * 64,
        }
        out = _load_segment(MagicMock(), row, datetime.now(timezone.utc))
        assert out["status"] == "IDEMPOTENT_SKIP"
        assert out["reason"] == "EXISTING_ROWS_RECOVERED"
        assert out["terminal_status"] == "PARTIAL"
        load.assert_not_called()
        fin.assert_called_once()


def test_same_key_different_fingerprint_conflicts():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-31T18:00:00Z",
        "segment_end": "2026-08-31T19:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "other",
        "source_path": "other.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
    }
    with patch(
        "research.btc_doge_research.full_history_runner._foreign_build_conflict",
        return_value="ed650f8048cea17b8ecc921b0108f7308696e581504a9c891dff824256fdc0d1",
    ):
        with pytest.raises(RuntimeError, match="CONFLICT"):
            _load_segment(MagicMock(), row, datetime.now(timezone.utc))


def test_parallel_batch_claim_exactly_one_writer():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-31T18:00:00Z",
        "segment_end": "2026-08-31T19:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "fp",
        "source_path": "p.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
    }
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    with (
        patch("research.btc_doge_research.full_history_runner._foreign_build_conflict", return_value=None),
        patch("research.btc_doge_research.full_history_runner._terminal_exists", return_value=False),
        patch("research.btc_doge_research.full_history_runner._existing_build_rows", return_value=0),
        patch("research.btc_doge_research.full_history_runner._register_running_claim"),
        patch(
            "research.btc_doge_research.full_history_runner._list_running_claims",
            return_value=[(now, "aaa111"), (now, "zzz999")],
        ),
        patch("research.btc_doge_research.full_history_runner.uuid.uuid4") as uid,
        patch("research.btc_doge_research.full_history_runner.load_segment") as load,
    ):
        uid.return_value = MagicMock(hex="zzz999")
        out = _load_segment(MagicMock(), row, now)
        assert out["status"] == "IDEMPOTENT_SKIP"
        assert out["reason"] == "ALREADY_RUNNING"
        load.assert_not_called()


def test_crash_after_data_before_status_no_duplicates():
    """Existing rows trigger recovery terminalization without loader insert."""
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-27T06:00:00Z",
        "segment_end": "2026-08-27T07:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "fp",
        "source_path": "p.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
        "expected_rows": 3600,
    }
    with (
        patch("research.btc_doge_research.full_history_runner._foreign_build_conflict", return_value=None),
        patch("research.btc_doge_research.full_history_runner._terminal_exists", return_value=False),
        patch("research.btc_doge_research.full_history_runner._existing_build_rows", return_value=3599),
        patch(
            "research.btc_doge_research.full_history_runner._coverage_status_from_existing",
            return_value="PARTIAL",
        ),
        patch(
            "research.btc_doge_research.full_history_runner._finalize_terminal",
            return_value={
                "status": "PARTIAL",
                "symbol": "BTCUSDT",
                "modality": "OB200",
                "segment_start": row["segment_start"],
                "batch_id": "b",
                "build_id": "g",
                "result": {"rows": 3599},
                "output_fingerprint": "0" * 64,
            },
        ),
        patch("research.btc_doge_research.full_history_runner.load_segment") as load,
    ):
        out = _load_segment(MagicMock(), row, datetime.now(timezone.utc))
        assert out["status"] == "IDEMPOTENT_SKIP"
        load.assert_not_called()


def test_source_gaps_remain_partial_on_success_path():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-27T06:00:00Z",
        "segment_end": "2026-08-27T07:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "fp",
        "source_path": "p.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
        "expected_rows": 3600,
    }
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    with (
        patch("research.btc_doge_research.full_history_runner._foreign_build_conflict", return_value=None),
        patch("research.btc_doge_research.full_history_runner._terminal_exists", side_effect=[False, False]),
        patch("research.btc_doge_research.full_history_runner._existing_build_rows", side_effect=[0, 0]),
        patch("research.btc_doge_research.full_history_runner._register_running_claim"),
        patch(
            "research.btc_doge_research.full_history_runner._list_running_claims",
            return_value=[(now, "only")],
        ),
        patch("research.btc_doge_research.full_history_runner.uuid.uuid4") as uid,
        patch(
            "research.btc_doge_research.full_history_runner.load_segment",
            return_value={
                "rows": 3599,
                "coverage_status": "PARTIAL",
                "missing_seconds": ["2026-08-27T06:42:23Z"],
            },
        ),
        patch(
            "research.btc_doge_research.full_history_runner._finalize_terminal",
            return_value={
                "status": "PARTIAL",
                "symbol": "BTCUSDT",
                "modality": "OB200",
                "segment_start": row["segment_start"],
                "batch_id": "b",
                "build_id": "g",
                "result": {"rows": 3599, "coverage_status": "PARTIAL"},
                "output_fingerprint": "0" * 64,
            },
        ) as fin,
    ):
        uid.return_value = MagicMock(hex="only")
        out = _load_segment(MagicMock(), row, now)
        assert out["status"] == "PARTIAL"
        assert fin.call_args.kwargs["batch_status"] == "PARTIAL"


def test_complete_stays_complete():
    row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-31T18:00:00Z",
        "segment_end": "2026-08-31T19:00:00Z",
        "producer_id": "BYBIT_OB200_SHADOW_ARCHIVE_V3",
        "source_fingerprint": "fp",
        "source_path": "p.zst",
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
        "expected_rows": 3600,
    }
    now = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    with (
        patch("research.btc_doge_research.full_history_runner._foreign_build_conflict", return_value=None),
        patch("research.btc_doge_research.full_history_runner._terminal_exists", side_effect=[False, False]),
        patch("research.btc_doge_research.full_history_runner._existing_build_rows", side_effect=[0, 0]),
        patch("research.btc_doge_research.full_history_runner._register_running_claim"),
        patch(
            "research.btc_doge_research.full_history_runner._list_running_claims",
            return_value=[(now, "only")],
        ),
        patch("research.btc_doge_research.full_history_runner.uuid.uuid4") as uid,
        patch(
            "research.btc_doge_research.full_history_runner.load_segment",
            return_value={"rows": 3600, "coverage_status": "COMPLETE"},
        ),
        patch(
            "research.btc_doge_research.full_history_runner._finalize_terminal",
            return_value={
                "status": "READY",
                "symbol": "BTCUSDT",
                "modality": "OB200",
                "segment_start": row["segment_start"],
                "batch_id": "b",
                "build_id": "g",
                "result": {"rows": 3600, "coverage_status": "COMPLETE"},
                "output_fingerprint": "0" * 64,
            },
        ) as fin,
    ):
        uid.return_value = MagicMock(hex="only")
        out = _load_segment(MagicMock(), row, now)
        assert out["status"] == "READY"
        assert fin.call_args.kwargs["batch_status"] == "READY"


def test_loader_ob_skips_insert_when_rows_exist():
    ctx = _ctx()
    client = MagicMock()
    with patch(
        "research.btc_doge_research.segment_loader.segment_counts",
        return_value={"research_ob200_snapshots_1s": 3599},
    ), patch(
        "research.btc_doge_research.segment_loader.rows",
        return_value=[("PARTIAL",)],
    ), patch(
        "research.btc_doge_research.segment_loader.insert"
    ) as ins:
        out = load_ob_file(client, ctx, datetime.now(timezone.utc))
        assert out["status"] == "IDEMPOTENT_SKIP"
        assert out["rows"] == 3599
        assert out["coverage_status"] == "PARTIAL"
        ins.assert_not_called()


def test_candles_still_blocked_from_loader():
    ctx = _ctx(modality="CANDLES")
    with pytest.raises(ModalityContractError):
        load_segment(None, ctx, datetime.now(timezone.utc))


def test_foreign_build_conflict_query_shape():
    client = MagicMock()
    with patch(
        "research.btc_doge_research.full_history_runner.rows",
        return_value=[("otherbuild" + "0" * 54,)],
    ) as q:
        got = _foreign_build_conflict(client, "batch", "a" * 64)
        assert got.startswith("otherbuild")
        assert TARGET_DATABASE in q.call_args.args[1]


def test_terminal_exists_includes_partial():
    client = MagicMock()
    with patch(
        "research.btc_doge_research.full_history_runner.rows",
        return_value=[(1,)],
    ) as q:
        assert _terminal_exists(client, "b", "g") is True
        sql = q.call_args.args[1]
        assert "READY" in sql and "PARTIAL" in sql
