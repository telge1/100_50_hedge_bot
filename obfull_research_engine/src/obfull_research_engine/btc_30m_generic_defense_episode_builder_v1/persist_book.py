"""Persist book window around episode; optional reuse of existing persist if covered."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json
from ..drilldown.aggregation_100ms import build_states_100ms
from ..drilldown.replay import replay_window
from ..level_first_episode1_corrected_sms1_persist_v1.persist import write_book_tables
from ..timeparse import format_utc_z
from . import BOOK_SOURCE
from .hashing import sha256_json


def episode_persist_window(
    *,
    zone_touch_ts: datetime | str,
    detection_ts: datetime | str | None,
) -> tuple[datetime, datetime]:
    zt = parse_utc(zone_touch_ts)
    start = zt - timedelta(minutes=5)
    end_candidates = [zt + timedelta(minutes=3)]
    if detection_ts is not None:
        end_candidates.append(parse_utc(detection_ts))
    end = max(end_candidates)
    if end <= start:
        end = start + timedelta(minutes=3)
    return start, end


def existing_persist_covers(
    persist_dir: Path,
    *,
    need_start: datetime,
    need_end: datetime,
) -> bool:
    """Best-effort: check STATUS / run_manifest for coverage; require core files exist."""
    required = (
        "states_100ms.jsonl.zst",
        "level_changes.jsonl.zst",
        "initial_book.jsonl.zst",
    )
    if not persist_dir.is_dir():
        return False
    if not all((persist_dir / name).exists() for name in required):
        return False
    manifest_path = persist_dir / "run_manifest.json"
    if not manifest_path.exists():
        # Files present — allow reuse only if caller accepts unknown window
        return False
    try:
        import json

        man = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False
    # Try common keys
    ws = man.get("window_start") or man.get("persist_window_start") or (man.get("config") or {}).get(
        "window_start"
    )
    we = man.get("window_end") or man.get("persist_window_end") or (man.get("config") or {}).get("window_end")
    if not ws or not we:
        return False
    try:
        covered_start = parse_utc(ws)
        covered_end = parse_utc(we)
    except Exception:  # noqa: BLE001
        return False
    return covered_start <= need_start and covered_end >= need_end


def _builder_kwargs(replay: dict[str, Any]) -> dict[str, Any]:
    return {
        "window_start": replay["window_start"],
        "window_end": replay["window_end"],
        "timeline": replay.get("timeline") or [],
        "level_changes": replay.get("level_changes") or [],
        "trades": replay.get("trades") or [],
        "book_snapshots_by_time": replay.get("book_snapshots_by_time") or {},
        "initial_bids": replay.get("initial_bids") or {},
        "initial_asks": replay.get("initial_asks") or {},
        "book_resets": replay.get("book_resets"),
        "evidence_start": replay.get("window_start"),
        "initial_update_id": replay.get("initial_update_id"),
        "initial_sequence_id": replay.get("initial_sequence_id"),
        "initial_replay_epoch": replay.get("initial_replay_epoch"),
        "initial_checkpoint_id": replay.get("initial_checkpoint_id"),
    }


def persist_episode_book(
    *,
    symbol: str,
    out_dir: Path,
    zone_touch_ts: datetime | str,
    detection_ts: datetime | str | None,
    archive_root: Path | str,
    trades: list[dict[str, Any]] | None = None,
    existing_persist_fallback: Path | str | None = None,
    allow_reuse: bool = True,
) -> dict[str, Any]:
    """
    Persist [zone_touch-5m, max(detection, zone_touch+3m)] via replay_window.
    Reuse existing persist dir if window covered (read-only fallback).
    """
    need_start, need_end = episode_persist_window(
        zone_touch_ts=zone_touch_ts, detection_ts=detection_ts
    )
    fallback = Path(existing_persist_fallback) if existing_persist_fallback else None
    if allow_reuse and fallback is not None and existing_persist_covers(
        fallback, need_start=need_start, need_end=need_end
    ):
        return {
            "ok": True,
            "reused": True,
            "persist_dir": str(fallback),
            "window_start": format_utc_z(need_start),
            "window_end": format_utc_z(need_end),
            "book_source": BOOK_SOURCE,
            "reuse_of": str(fallback),
        }

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    replay = replay_window(
        symbol=symbol,
        window_start=need_start,
        window_end=need_end,
        archive_root=Path(archive_root),
        trades=trades,
    )
    if not replay.get("ok", True):
        return {
            "ok": False,
            "reused": False,
            "error": replay.get("error") or "replay_window failed",
            "window_start": format_utc_z(need_start),
            "window_end": format_utc_z(need_end),
        }
    states = build_states_100ms(**_builder_kwargs(replay))
    cfg_hash = sha256_json(
        {
            "symbol": symbol,
            "window_start": format_utc_z(need_start),
            "window_end": format_utc_z(need_end),
            "book_source": BOOK_SOURCE,
        }
    )
    hashes = write_book_tables(
        out_dir,
        states=states,
        replay=replay,
        config_hash=cfg_hash[:16],
        input_hash=cfg_hash[16:32],
    )
    meta = {
        "ok": True,
        "reused": False,
        "persist_dir": str(out_dir),
        "window_start": format_utc_z(need_start),
        "window_end": format_utc_z(need_end),
        "book_source": BOOK_SOURCE,
        "table_hashes": hashes,
    }
    atomic_write_json(out_dir / "persist_meta.json", meta)
    return {**meta, "replay": replay}
