"""Read compressed raw archive segments for replay parity tests."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any, Iterator

import orjson

from orderbook_analyse.ob_data_source.ndjson_parse import parse_ob200_obj
from orderbook_analyse.orderbook_replay import BookLevelEvent, OrderBookReplayer
from orderbook_analyse.orderbook_v2_live.raw_archive.events import is_replayable_line, line_to_replay_payload

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None  # type: ignore[assignment]


def _read_segment_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if path.suffix == ".zst" or path.name.endswith(".zst"):
        if zstd is None:
            raise RuntimeError("zstandard not installed")
        with zstd.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
            return reader.read()
    return raw


def iter_segment_lines(path: Path) -> Iterator[dict[str, Any]]:
    data = _read_segment_bytes(path)
    for line_no, line in enumerate(data.splitlines(), start=1):
        if not line.strip():
            continue
        obj = orjson.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"invalid line {line_no} in {path}")
        yield obj


def iter_book_level_events(path: Path, *, expected_symbol: str | None = None) -> list[BookLevelEvent]:
    events: list[BookLevelEvent] = []
    for line_no, obj in enumerate(iter_segment_lines(path), start=1):
        if not is_replayable_line(obj):
            continue
        payload = line_to_replay_payload(obj)
        msg = parse_ob200_obj(
            payload,
            expected_symbol=expected_symbol,
            source_file=str(path),
            source_line=line_no,
        )
        events.extend(msg.to_book_level_events())
    return events


def replay_segment(path: Path, *, expected_symbol: str | None = None):
    replayer = OrderBookReplayer()
    events = iter_book_level_events(path, expected_symbol=expected_symbol)
    return replayer.replay(events)


def load_manifest(path: Path) -> dict[str, Any]:
    manifest_path = path.with_suffix(".manifest.json")
    if not manifest_path.is_file():
        manifest_path = Path(str(path) + ".manifest.json")
    if not manifest_path.is_file():
        raise FileNotFoundError(f"missing manifest for {path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))
