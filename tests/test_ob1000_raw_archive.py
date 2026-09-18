"""OB1000 parallel raw-archive settings and segment naming."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import orjson
import pytest

from orderbook_analyse.orderbook_v2_live.raw_archive.config import (
    OB1000_PARSER_VERSION,
    RawArchiveSettings,
    load_ob1000_raw_archive_settings,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.manager import RawArchiveManager


def test_ob1000_config_default_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OB_V3_OB1000_RAW_ARCHIVE_ENABLE", raising=False)
    cfg = load_ob1000_raw_archive_settings()
    assert cfg.enabled is False
    assert cfg.depth == 1000
    assert cfg.parser_version == OB1000_PARSER_VERSION
    assert cfg.health_nest_key == "ob1000_raw_archive"


def test_ob1000_config_inherits_ob200_symbols(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OB_V3_OB1000_RAW_ARCHIVE_ENABLE", "true")
    monkeypatch.delenv("OB_V3_OB1000_RAW_ARCHIVE_SYMBOLS", raising=False)
    monkeypatch.setenv("OB_V3_RAW_ARCHIVE_SYMBOLS", "BTCUSDT,DOGEUSDT")
    cfg = load_ob1000_raw_archive_settings()
    assert cfg.enabled is True
    assert cfg.symbols == frozenset({"BTCUSDT", "DOGEUSDT"})


def test_ob1000_segment_uses_parser_version(tmp_path: Path) -> None:
    settings = RawArchiveSettings(
        enabled=True,
        archive_root=tmp_path,
        symbols=frozenset({"BTCUSDT"}),
        queue_size=32,
        rotation="hour",
        compression="zstd",
        depth=1000,
        format_version="ob1000_v1_live_archive/v1",
        parser_version=OB1000_PARSER_VERSION,
        health_nest_key="ob1000_raw_archive",
    )

    async def _run() -> None:
        mgr = RawArchiveManager(settings)
        mgr.start()
        now = datetime.now(timezone.utc)
        payload = {
            "topic": "orderbook.1000.BTCUSDT",
            "type": "snapshot",
            "ts": int(now.timestamp() * 1000),
            "data": {
                "s": "BTCUSDT",
                "b": [["100.0", "1.0"]],
                "a": [["100.1", "1.0"]],
                "u": 1,
                "seq": 1,
            },
        }
        mgr.try_enqueue_market("BTCUSDT", payload, now)
        await asyncio.sleep(0.15)
        await mgr.stop()
        closed = list(tmp_path.rglob(f"*_{OB1000_PARSER_VERSION}.zst"))
        assert len(closed) == 1
        man_path = closed[0].with_suffix(".manifest.json")
        assert man_path.exists(), list(tmp_path.rglob("*"))
        manifest = orjson.loads(man_path.read_bytes())
        assert manifest["depth"] == 1000
        assert manifest["parser_version"] == OB1000_PARSER_VERSION
        health = mgr.health_dict()
        assert "ob1000_raw_archive" in health

    asyncio.run(_run())
