"""Materialize NVDA OB1000 fixture via live symbol validation (offline)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from research.btc_doge_research.ob1000_live_symbols import validate_ob1000_live_symbol
from research.btc_doge_research.ob1000_materializer import materialize_segment
from research.btc_doge_research.source_file_registry import SourceFile
from research.btc_doge_research.contracts import parse_utc


def _ndjson_records() -> list[dict]:
    return [
        {
            "format_version": "ob200_v3_live_archive/v1",
            "type": "rotation_checkpoint",
            "ts": 1788199200000,
            "local_receive_ts": "2026-08-31T18:00:00.010000Z",
            "data": {
                "s": "NVDAUSDT",
                "u": 10,
                "seq": 100,
                "b": [["100", "2"], ["99", "3"]],
                "a": [["101", "4"], ["102", "5"]],
            },
        },
        {
            "format_version": "ob200_v3_live_archive/v1",
            "type": "delta",
            "ts": 1788199200500,
            "local_receive_ts": "2026-08-31T18:00:00.510000Z",
            "data": {
                "s": "NVDAUSDT",
                "u": 11,
                "seq": 500,
                "b": [["100", "1"]],
                "a": [["101", "6"]],
            },
        },
    ]


@pytest.fixture()
def nvda_segment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    cfg = tmp_path / "ob1000_live_symbols.json"
    cfg.write_text(
        json.dumps({"symbols": ["BTCUSDT", "DOGEUSDT", "NVDAUSDT"]}),
        encoding="utf-8",
    )
    ticks = tmp_path / "ob1000_tick_sizes.json"
    ticks.write_text(
        json.dumps({"tick_sizes": {"NVDAUSDT": "0.01"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OB1000_SYMBOLS_FILE", str(cfg))
    monkeypatch.setenv("OB1000_TICK_SIZES_FILE", str(ticks))
    from research.btc_doge_research.ob1000_live_ticks import clear_ob1000_tick_cache

    clear_ob1000_tick_cache()

    day = tmp_path / "NVDAUSDT" / "2026-08-31"
    day.mkdir(parents=True)
    path = day / "NVDAUSDT_20260831T180000Z_20260831T190000Z_ob1000_v1.ndjson"
    path.write_text("".join(json.dumps(r) + "\n" for r in _ndjson_records()), encoding="utf-8")
    return {
        "symbol": "NVDAUSDT",
        "start": parse_utc("2026-08-31T18:00:00Z"),
        "end": parse_utc("2026-08-31T19:00:00Z"),
        "path": path,
        "relative_path": str(path.relative_to(tmp_path)),
        "is_open": False,
    }


def test_materialize_segment_accepts_registered_nvda(
    nvda_segment: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert validate_ob1000_live_symbol("NVDAUSDT") == "NVDAUSDT"

    client = MagicMock()
    inserted: list = []

    def fake_insert(_client, table, batch, columns):  # noqa: ANN001
        inserted.extend(batch)

    monkeypatch.setattr(
        "research.btc_doge_research.ob1000_materializer._existing_seconds",
        lambda *a, **k: set(),
    )
    monkeypatch.setattr(
        "research.btc_doge_research.ob1000_materializer._warmup_sources_for_segment",
        lambda *a, **k: ((), None),
    )
    monkeypatch.setattr(
        "research.btc_doge_research.ob1000_materializer.insert",
        fake_insert,
    )
    monkeypatch.setattr(
        "research.btc_doge_research.ob1000_materializer.load_ob1000_source_file",
        lambda path, root: SourceFile(
            path=path,
            relative_path=str(path.relative_to(root)),
            fingerprint="c" * 64,
            source_file_id="d" * 64,
            size=path.stat().st_size,
            manifest={
                "format_version": "ob200_v3_live_archive/v1",
                "parser_version": "ob200_v3",
                "event_count": 2,
                "replayable": False,
            },
            segment_start=nvda_segment["start"],
            segment_end=nvda_segment["end"],
        ),
    )

    out = materialize_segment(
        client,
        segment=nvda_segment,
        root=tmp_path,
        now=datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        expected_depth=2,
    )
    assert out["symbol"] == "NVDAUSDT"
    assert out["rows_inserted"] >= 1
    assert inserted
    assert inserted[0][0] == "NVDAUSDT"
