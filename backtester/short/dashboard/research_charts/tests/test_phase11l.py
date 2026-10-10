"""Fail-closed checks for the Phase 11L publish proof. No engine run and no ClickHouse."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research_charts.live_micro_lld_v2_ch_test.immutable_snapshot_candle_session import (
    ImmutableInputCoverageError,
    _ClickHouseFallbackRefused,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.compare import compare_published_close
from research_charts.live_micro_lld_v2_ch_test.phase11l.frozen import (
    PRICE_HASH,
    REVISION_HASH,
    SNAPSHOT_DIR,
    SNAPSHOT_ID,
    FrozenInputError,
    required_source_start_unix,
    verify_manifest,
)
from research_charts.live_micro_lld_v2_ch_test.phase11l.publish import (
    disjoint_blocks,
    merge_worker_payloads,
    planned_closes,
    repeat_digest,
)

CLOSE = "2026-09-30T11:20:00+00:00"


def _record(close: str, pool_id: str = "p1", side: str = "upper") -> dict:
    return {
        "decision_close": close,
        "active_state_fingerprint": "fp-" + close,
        "active_pool_ids": [pool_id],
        "pools": {
            pool_id: {
                "pool_id": pool_id,
                "side": side,
                "top_price": 0.2,
                "bottom_price": 0.1,
                "strength": 1.5,
                "strength_class": "strong",
                "known_at": close,
                "row_fingerprint": "row-" + pool_id,
            }
        },
    }


def _payload(worker_id: int, closes: list[str], revision: str = REVISION_HASH) -> dict:
    return {
        "status": "PASS",
        "worker_id": worker_id,
        "snapshot_id": SNAPSHOT_ID,
        "price_hash": PRICE_HASH,
        "revision_hash": revision,
        "clickhouse_candle_reads": 0,
        "records": [_record(close, pool_id=f"w{worker_id}") for close in closes],
    }


def test_official_snapshot_file_identity():
    manifest_path = SNAPSHOT_DIR / f"{SNAPSHOT_ID}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == SNAPSHOT_ID
    assert manifest["ohlcv_content_hash"] == PRICE_HASH
    assert manifest["revision_metadata_hash"] == REVISION_HASH
    data = SNAPSHOT_DIR / manifest["data_file"]
    assert hashlib.sha256(data.read_bytes()).hexdigest() == manifest["file_checksum"]


def test_missing_and_changed_input(tmp_path: Path):
    with pytest.raises(FrozenInputError, match="missing manifest"):
        verify_manifest(
            tmp_path,
            need_from=datetime(2026, 5, 23, tzinfo=timezone.utc),
            need_to=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
    manifest = {
        "snapshot_id": SNAPSHOT_ID,
        "ohlcv_content_hash": "0" * 64,
        "revision_metadata_hash": REVISION_HASH,
        "symbol": "DOGEUSDT",
        "snapshot_completeness_status": "COMPLETE",
        "data_file": "x.parquet",
        "file_checksum": "abc",
        "range_start": "2026-05-23T05:25:00.000Z",
        "range_end_exclusive": "2026-09-30T12:01:00.000Z",
    }
    (tmp_path / f"{SNAPSHOT_ID}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FrozenInputError, match="price hash"):
        verify_manifest(
            tmp_path,
            need_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
            need_to=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
    manifest["ohlcv_content_hash"] = PRICE_HASH
    data = tmp_path / "x.parquet"
    data.write_bytes(b"changed")
    manifest["file_checksum"] = "deadbeef"
    (tmp_path / f"{SNAPSHOT_ID}.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FrozenInputError, match="checksum"):
        verify_manifest(
            tmp_path,
            need_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
            need_to=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )


def test_warmup_fits_inside_snapshot():
    closes = planned_closes()
    start = datetime.fromtimestamp(required_source_start_unix(min(closes), 10, "5m"), tz=timezone.utc)
    assert start >= datetime(2026, 5, 23, 5, 25, tzinfo=timezone.utc)
    assert start < datetime(2026, 6, 1, tzinfo=timezone.utc)
    assert max(closes) <= datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def test_clickhouse_candle_source_is_refused():
    with pytest.raises(ImmutableInputCoverageError, match="clickhouse fallback refused"):
        _ClickHouseFallbackRefused().get_1m_candles("DOGEUSDT")


def test_disjoint_blocks_cover_every_close_once():
    closes = planned_closes()
    blocks = disjoint_blocks(closes)
    assert len(blocks) == 3
    assert [len(block) for block in blocks] == [3, 3, 3]
    flat = [c for block in blocks for c in block]
    assert flat == closes
    assert len({c.isoformat() for c in flat}) == 9


def test_merge_rejects_revision_mismatch_duplicates_and_worker_failure():
    closes = planned_closes()
    isos = [c.isoformat() for c in closes]
    blocks = [isos[0:3], isos[3:6], isos[6:9]]
    good = [_payload(i, block) for i, block in enumerate(blocks)]
    merged = merge_worker_payloads(good, closes)
    assert repeat_digest(merged["records"]) == merged["merge_digest"]
    again = merge_worker_payloads(list(reversed(good)), closes)
    assert again["merge_digest"] == merged["merge_digest"]

    drifted = [_payload(0, blocks[0], revision="a" * 64), good[1], good[2]]
    with pytest.raises(Exception, match="frozen input"):
        merge_worker_payloads(drifted, closes)

    short = [_payload(0, blocks[0][:2]), good[1], good[2]]
    with pytest.raises(Exception, match="coverage"):
        merge_worker_payloads(short, closes)

    dup = [_payload(0, blocks[0]), _payload(1, blocks[0]), good[2]]
    with pytest.raises(Exception, match="duplicate"):
        merge_worker_payloads(dup, closes)

    failed = [dict(good[0], status="FAIL", error="worker down", records=[]), good[1], good[2]]
    with pytest.raises(Exception, match="worker down"):
        merge_worker_payloads(failed, closes)


def test_publication_parity_fails_closed():
    local = _record(CLOSE)
    published = {"decision_close": CLOSE, "active_state_fingerprint": "fp-" + CLOSE, "snapshot_id": "s"}
    catalog = [
        {
            "pool_id": "p1",
            "side": "upper",
            "top_price": 0.2,
            "bottom_price": 0.1,
            "strength": 1.5,
            "strength_class": "strong",
            "known_at": CLOSE,
            "row_fingerprint": "row-p1",
        }
    ]
    assert compare_published_close(local, published, catalog) == []
    changed = dict(local)
    changed["pools"] = {"p1": dict(local["pools"]["p1"], side="lower")}
    assert any(item.startswith("side") for item in compare_published_close(changed, published, catalog))
    other_fp = dict(published, active_state_fingerprint="other")
    assert any("membership" in item for item in compare_published_close(local, other_fp, catalog))
    assert any("missing_catalog" in item for item in compare_published_close(local, published, []))
