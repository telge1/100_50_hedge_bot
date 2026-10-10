"""Bind every worker to one immutable 1m snapshot. ClickHouse candles are refused."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..identity import build_live_micro_lld_v2_config
from ..immutable_candle_snapshot_v1 import (
    ohlcv_content_hash,
    require_snapshot,
    revision_metadata_hash,
)
from ..immutable_snapshot_candle_session import ImmutableSnapshotCandleSession
from ..active_snapshot.rolling_calendar import rolling_history_from_close
from ...service import SOURCE_TF, _timeframe_seconds
from ...trp_import import ensure_trp_on_path, load_trp

SNAPSHOT_ID = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
PRICE_HASH = "093b2bd57f6525a7886c0bc3c76cd27c24ef4e51d640f155fe410b435e4acd28"
REVISION_HASH = "df049cde8eb41cf916847b9063f7f4ddd1a894d574ba713e9568c2b4c8bf4f25"
SNAPSHOT_DIR = (
    Path(__file__).resolve().parents[1]
    / "research_runs"
    / "phase11r_20261009T121404Z_defdfaea"
)


class FrozenInputError(RuntimeError):
    pass


def required_source_start_unix(decision_close: datetime, pool_detail: int, timeframe: str) -> int:
    cfg = build_live_micro_lld_v2_config(int(pool_detail))
    warmup_bars = int(cfg.volume_lookback) + int(cfg.swing_len) + 64
    tf_sec = _timeframe_seconds(timeframe)
    history = rolling_history_from_close(decision_close, 4)
    load_start = max(0, int(history.timestamp()) - warmup_bars * tf_sec)
    ensure_trp_on_path()
    need = int(load_trp()["expected_source_bars"](SOURCE_TF, timeframe))
    return int(load_start) - need * 60


def verify_manifest(directory: Path, *, need_from: datetime, need_to: datetime) -> dict:
    path = directory / f"{SNAPSHOT_ID}.manifest.json"
    if not path.is_file():
        raise FrozenInputError(f"missing manifest {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != SNAPSHOT_ID:
        raise FrozenInputError("snapshot id mismatch")
    if manifest.get("ohlcv_content_hash") != PRICE_HASH:
        raise FrozenInputError("price hash mismatch")
    if manifest.get("revision_metadata_hash") != REVISION_HASH:
        raise FrozenInputError("revision hash mismatch")
    if manifest.get("symbol") != "DOGEUSDT" or manifest.get("snapshot_completeness_status") != "COMPLETE":
        raise FrozenInputError("snapshot is not the complete DOGEUSDT input")
    data = directory / str(manifest["data_file"])
    digest = hashlib.sha256(data.read_bytes()).hexdigest()
    if digest != manifest.get("file_checksum"):
        raise FrozenInputError("parquet checksum mismatch")
    start = datetime.fromisoformat(str(manifest["range_start"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(manifest["range_end_exclusive"]).replace("Z", "+00:00"))
    if need_from < start or need_to >= end:
        raise FrozenInputError(
            f"coverage {need_from.isoformat()}..{need_to.isoformat()} outside {start.isoformat()}..{end.isoformat()}"
        )
    rows = require_snapshot(directory, SNAPSHOT_ID, ohlcv_hash=PRICE_HASH)
    if ohlcv_content_hash(rows) != PRICE_HASH or revision_metadata_hash(rows) != REVISION_HASH:
        raise FrozenInputError("recomputed hashes do not match the manifest")
    return {
        "snapshot_id": SNAPSHOT_ID,
        "price_hash": PRICE_HASH,
        "revision_hash": REVISION_HASH,
        "file_checksum": digest,
        "row_count": len(rows),
        "directory": str(directory),
    }


def open_session(directory: Path) -> ImmutableSnapshotCandleSession:
    """Canonical incremental session. The ClickHouse source is the refusing stub."""
    return ImmutableSnapshotCandleSession(
        directory,
        SNAPSHOT_ID,
        ohlcv_hash=PRICE_HASH,
        symbol="DOGEUSDT",
        timeframe="5m",
    )


def worker_identity(directory: Path) -> dict:
    rows = require_snapshot(directory, SNAPSHOT_ID, ohlcv_hash=PRICE_HASH)
    price = ohlcv_content_hash(rows)
    revision = revision_metadata_hash(rows)
    if price != PRICE_HASH or revision != REVISION_HASH:
        raise FrozenInputError("worker input identity diverged")
    return {
        "snapshot_id": SNAPSHOT_ID,
        "price_hash": price,
        "revision_hash": revision,
        "clickhouse_candle_reads": 0,
    }
