#!/usr/bin/env python3
"""Offline smoke: synthetic BTCUSDT raw archive → replay → feature parity."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.orderbook_v2.book import apply_delta, apply_snapshot
from orderbook_analyse.orderbook_v2_live.collector import OrderbookV3LiveCollector
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock
from orderbook_analyse.orderbook_v2_live.raw_archive.config import RawArchiveSettings
from orderbook_analyse.orderbook_v2_live.raw_archive.events import serialize_rotation_checkpoint
from orderbook_analyse.orderbook_v2_live.raw_archive.manager import RawArchiveManager
from orderbook_analyse.orderbook_v2_live.raw_archive.replay import (
    iter_book_level_events,
    load_manifest,
)
from orderbook_analyse.orderbook_v2_live.settings import LiveCollectorSettings
from orderbook_analyse.orderbook_replay import OrderBookReplayer

T0 = 1_750_000_000_000
OUT = REPO / "results" / "orderbook_v3_raw_archive" / "offline_smoke"


def _event(ts: int, type_: str, bids, asks, u: int, seq: int) -> dict:
    return {
        "topic": "orderbook.200.BTCUSDT",
        "type": type_,
        "ts": ts,
        "cts": None,
        "data": {"s": "BTCUSDT", "b": bids, "a": asks, "u": u, "seq": seq},
    }


def _messages() -> list[dict]:
    return [
        _event(T0 + 10, "snapshot", [["90000", "5"], ["89999", "3"]], [["90001", "4"]], 1, 100),
        _event(T0 + 100, "delta", [["90000", "2"]], [], 2, 101),
        _event(T0 + 200, "delta", [["90000", "4"]], [], 3, 102),
        _event(T0 + 300, "delta", [["90000", "0"]], [], 4, 103),
        _event(T0 + 400, "delta", [["89998", "1"]], [["90001", "6"]], 5, 104),
        _event(T0 + 500, "delta", [["89997", "2"]], [["90002", "1"]], 6, 105),
    ]


async def _run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    archive_root = Path(tempfile.mkdtemp(prefix="archive_", dir=OUT))
    settings = RawArchiveSettings(
        enabled=True,
        archive_root=archive_root,
        symbols=frozenset({"BTCUSDT"}),
        queue_size=128,
        rotation="hour",
        compression="zstd",
    )
    manager = RawArchiveManager(settings)
    manager.start()

    live_settings = LiveCollectorSettings(
        bybit_ws_url="wss://example",
        clickhouse_host="127.0.0.1",
        clickhouse_http_port=8123,
        clickhouse_database="orderbook_analysis",
        clickhouse_user="",
        clickhouse_password="",
        symbols=("BTCUSDT",),
        mode="ada",
        lock_path=OUT / "lock",
        pid_path=OUT / "pid",
        health_path=None,
        ada_only_pilot=False,
    )
    collector = OrderbookV3LiveCollector(live_settings, raw_archive=manager)
    collector._reset_runtimes()
    rt = collector.runtimes["BTCUSDT"]
    rt.dropping_until_subscribe_ack = False
    rt.active_generation = rt.clock.generation

    clock_direct = LiveSecondClock("BTCUSDT")
    clock_replay = LiveSecondClock("BTCUSDT")
    direct_rows: list[dict] = []
    for msg in _messages():
        received = datetime.fromtimestamp(msg["ts"] / 1000, tz=timezone.utc)
        collector._ingest_ready(rt, msg, received)
        direct_rows.extend(clock_direct.ingest(msg["type"], msg["ts"], msg["data"]))
    direct_rows.extend(clock_direct.close_through(T0 + 2000))

    book_before_rotation = rt.clock.book
    await asyncio.sleep(0.05)
    await manager.rotate_with_checkpoint(
        "BTCUSDT",
        book_before_rotation,
        ts_ms=T0 + 600,
        received_at=datetime.fromtimestamp((T0 + 600) / 1000, tz=timezone.utc),
        topic="orderbook.200.BTCUSDT",
    )
    post = _event(T0 + 700, "delta", [["89996", "1"]], [], 7, 106)
    received = datetime.fromtimestamp(post["ts"] / 1000, tz=timezone.utc)
    collector._ingest_ready(rt, post, received)
    direct_rows.extend(clock_direct.ingest(post["type"], post["ts"], post["data"]))
    direct_rows.extend(clock_direct.close_through(T0 + 3000))

    while manager._queue.qsize() > 0:
        await asyncio.sleep(0.01)
    await manager.stop()

    segments = sorted(archive_root.glob("**/*.zst"))
    all_events = []
    segment_manifests = []
    for seg in segments:
        all_events.extend(iter_book_level_events(seg, expected_symbol="BTCUSDT"))
        segment_manifests.append(load_manifest(seg))
    replay_book = OrderBookReplayer().replay(all_events)

    for msg in _messages() + [post]:
        clock_replay.ingest(msg["type"], msg["ts"], msg["data"])
    replay_rows = clock_replay.close_through(T0 + 3000)

    summary = {
        "archive_root": str(archive_root),
        "segment_count": len(segments),
        "direct_book_bids": len(clock_direct.book.bids),
        "replay_book_bids": len(replay_book.bids) if replay_book else 0,
        "direct_rows": len(direct_rows),
        "replay_rows": len(replay_rows),
        "books_match": clock_direct.book.bids == replay_book.bids
        and clock_direct.book.asks == replay_book.asks,
        "manifests": segment_manifests,
    }
    (OUT / "smoke_summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> int:
    summary = asyncio.run(_run())
    print(json.dumps(summary, indent=2))
    if not summary["books_match"]:
        return 1
    if summary["segment_count"] < 2:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
