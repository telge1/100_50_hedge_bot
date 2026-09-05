"""Deterministic offline Full-OB replay from event package (multi-epoch aware)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import orjson

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_edge_flight_recorder.continuity_contract import (
    CHECKPOINT_KINDS,
    RECORD_BOOK_DELTA,
    RECORD_INITIAL_CHECKPOINT,
    RECORD_RESYNC_BOUNDARY,
    RECORD_RESYNC_CHECKPOINT,
    book_content_hash,
    is_book_delta_record,
)
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None


def _read_zst_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if path.suffixes[-2:] == [".json", ".zst"] or str(path).endswith(".json.zst"):
        data = zstd.ZstdDecompressor().decompress(raw)
        return orjson.loads(data)
    return orjson.loads(raw)


def _iter_zst_jsonl(path: Path):
    dctx = zstd.ZstdDecompressor()
    with open(path, "rb") as fh:
        with dctx.stream_reader(fh) as reader:
            buf = b""
            while True:
                chunk = reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        yield orjson.loads(line)
            if buf.strip():
                yield orjson.loads(buf)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_event_root(event_dir: Path) -> Path:
    snap = event_dir / "rest_full_snapshot.json.zst"
    if snap.exists():
        return event_dir
    parent = event_dir.parent
    if (parent / "rest_full_snapshot.json.zst").exists():
        return parent
    return event_dir


def _epoch_summary(book: FullBookState, *, epoch_id: int, applied: int, gaps: int, start_u, start_seq, seed_hash: str | None) -> dict[str, Any]:
    bb, ba = book.best_bid(), book.best_ask()
    crossed = bb is not None and ba is not None and bb >= ba
    # Rebuild string levels from float book for hash (best-effort); prefer stored seed hash.
    bids = [[str(p), str(q)] for p, q in sorted(book.bids.items(), reverse=True)]
    asks = [[str(p), str(q)] for p, q in sorted(book.asks.items())]
    return {
        "continuity_epoch_id": epoch_id,
        "start_u": start_u,
        "end_u": book.update_id,
        "start_seq": start_seq,
        "end_seq": book.seq,
        "applied_deltas": applied,
        "apply_epoch_u_gap_count": gaps,
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
        "best_bid": bb,
        "best_ask": ba,
        "crossed": crossed,
        "seed_book_hash": seed_hash,
        "final_book_hash": book_content_hash(bids=bids, asks=asks),
        "ok": gaps == 0 and not crossed,
    }


def replay_event_directory(event_dir: Path) -> dict[str, Any]:
    """Replay REST/initial + optional resync epochs; never claim continuous across gaps."""
    if zstd is None:
        raise RuntimeError("zstandard required")
    root = _resolve_event_root(event_dir)
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        segs = sorted(root.glob("cont_*/manifest.json"))
        if segs:
            manifest_path = segs[0]
        else:
            raise FileNotFoundError("manifest.json missing")
    manifest = orjson.loads(manifest_path.read_bytes())
    event_manifest_path = root / "event_manifest.json"
    event_manifest = orjson.loads(event_manifest_path.read_bytes()) if event_manifest_path.exists() else {}
    snap_path = root / "rest_full_snapshot.json.zst"
    delta_paths = []
    root_delta = root / "full_ob_raw_deltas.jsonl.zst"
    if root_delta.exists():
        delta_paths.append(root_delta)
    delta_paths.extend(sorted(root.glob("cont_*/full_ob_raw_deltas.jsonl.zst")))
    if not snap_path.exists() and not any(True for _ in []):
        # May still replay from INITIAL_CHECKPOINT in stream.
        pass
    if not delta_paths and not snap_path.exists():
        return {"ok": False, "status": "INCOMPLETE_DISK_ERROR", "error": "missing_deltas_and_snapshot"}

    for delta_path in delta_paths:
        man = delta_path.parent / "manifest.json"
        if not man.exists():
            continue
        expected = (orjson.loads(man.read_bytes()).get("sha256") or {}).get("full_ob_raw_deltas.jsonl.zst")
        if expected and sha256_file(delta_path) != expected:
            return {"ok": False, "status": "INCOMPLETE_DISK_ERROR", "error": "sha256_mismatch_deltas"}

    symbol = str(manifest.get("symbol") or event_manifest.get("symbol_event_id") or "UNKNOWN")
    if "|" in symbol or "_" in symbol:
        # fight id style — prefer manifest symbol field
        symbol = str(manifest.get("symbol") or "BTCUSDT")

    records: list[dict[str, Any]] = []
    for delta_path in delta_paths:
        for obj in _iter_zst_jsonl(delta_path):
            records.append(obj)

    has_stream_checkpoint = any(
        r.get("record_kind") in CHECKPOINT_KINDS for r in records
    )
    epochs: list[dict[str, Any]] = []
    unobserved: list[dict[str, Any]] = []
    continuous_capture = True
    replayable_by_epochs = True

    book = FullBookState(symbol=symbol)
    epoch_id = 0
    applied = 0
    gaps = 0
    start_u = None
    start_seq = None
    seed_hash = None
    seeded = False

    def close_epoch() -> None:
        nonlocal applied, gaps, start_u, start_seq, seed_hash, seeded
        if not seeded:
            return
        epochs.append(
            _epoch_summary(
                book,
                epoch_id=epoch_id,
                applied=applied,
                gaps=gaps,
                start_u=start_u,
                start_seq=start_seq,
                seed_hash=seed_hash,
            )
        )
        if gaps:
            nonlocal_replay_fail()
        applied = 0
        gaps = 0

    def nonlocal_replay_fail() -> None:
        nonlocal replayable_by_epochs
        replayable_by_epochs = False

    def seed_from_snapshot(snap: dict[str, Any], *, expected_hash: str | None, eid: int) -> None:
        nonlocal epoch_id, start_u, start_seq, seed_hash, seeded, book
        bids = snap.get("b") or []
        asks = snap.get("a") or []
        digest = book_content_hash(bids=bids, asks=asks)
        if expected_hash and expected_hash != digest:
            nonlocal_replay_fail()
            raise ValueError(f"checkpoint_book_hash_mismatch epoch={eid}")
        book = FullBookState(symbol=symbol)
        book.apply_snapshot(
            bids=bids,
            asks=asks,
            u=snap.get("u"),
            seq=snap.get("seq"),
            ts_ms=snap.get("ts") or snap.get("cts"),
            cts_ms=snap.get("cts"),
            mark_ready=True,
        )
        epoch_id = eid
        start_u = book.update_id
        start_seq = book.seq
        seed_hash = digest
        seeded = True

    try:
        if has_stream_checkpoint:
            for obj in records:
                kind = obj.get("record_kind")
                if kind == RECORD_RESYNC_BOUNDARY or obj.get("marker_type") == "RESYNC_BOUNDARY":
                    continuous_capture = False
                    unobserved.append(
                        {
                            "prev_u": obj.get("prev_u"),
                            "reason": obj.get("resync_reason"),
                            "disconnect_ts": obj.get("disconnect_ts"),
                            "reconnect_ts": obj.get("reconnect_ts"),
                        }
                    )
                    close_epoch()
                    seeded = False
                    continue
                if kind in CHECKPOINT_KINDS:
                    if seeded:
                        close_epoch()
                    snap = obj.get("data") or {}
                    seed_from_snapshot(
                        snap,
                        expected_hash=obj.get("book_hash"),
                        eid=int(obj.get("continuity_epoch_id") or (1 if kind == RECORD_RESYNC_CHECKPOINT else 0)),
                    )
                    continue
                if obj.get("channel") in {"lifecycle", "marker"} or obj.get("marker_type"):
                    continue
                if not seeded:
                    # Hold deltas until a checkpoint appears (should not happen with gate).
                    continuous_capture = False
                    continue
                if not is_book_delta_record(obj) and kind != RECORD_BOOK_DELTA:
                    continue
                data = obj.get("data") or {}
                out = book.apply_delta(
                    bids=data.get("b") or [],
                    asks=data.get("a") or [],
                    u=data.get("u"),
                    seq=data.get("seq"),
                    ts_ms=obj.get("ts") or data.get("ts"),
                    cts_ms=obj.get("cts") or data.get("cts"),
                    enforce_continuity=True,
                )
                if out is DeltaOutcome.GAP:
                    gaps += 1
                    continuous_capture = False
                    close_epoch()
                    return {
                        "ok": False,
                        "status": "INCOMPLETE_SEQUENCE_GAP",
                        "error": f"gap_at_u={data.get('u')} epoch={epoch_id}",
                        "epochs": epochs,
                        "continuous_capture": False,
                        "replayable_by_epochs": False,
                        "continuity_epoch_count": len(epochs),
                        "unobserved_intervals": unobserved,
                        "research_eligible": False,
                    }
                if out is DeltaOutcome.APPLIED:
                    applied += 1
            close_epoch()
        else:
            # Legacy single-seed path (pre-contract captures).
            if not snap_path.exists():
                return {"ok": False, "status": "NO_VALID_INITIAL_SNAPSHOT", "error": "missing_snapshot"}
            snap = _read_zst_json(snap_path)
            seed_from_snapshot(snap, expected_hash=None, eid=0)
            for obj in records:
                if obj.get("archive_event") or obj.get("channel") in {"lifecycle", "marker"}:
                    continue
                if obj.get("marker_type"):
                    continue
                data = obj.get("data") or {}
                if data.get("u") is None:
                    continue
                out = book.apply_delta(
                    bids=data.get("b") or [],
                    asks=data.get("a") or [],
                    u=data.get("u"),
                    seq=data.get("seq"),
                    ts_ms=obj.get("ts") or data.get("ts"),
                    cts_ms=obj.get("cts") or data.get("cts"),
                    enforce_continuity=True,
                )
                if out is DeltaOutcome.GAP:
                    gaps += 1
                    continuous_capture = False
                    close_epoch()
                    return {
                        "ok": False,
                        "status": "INCOMPLETE_SEQUENCE_GAP",
                        "error": f"gap_at_u={data.get('u')}",
                        "applied": applied,
                        "epochs": epochs,
                        "continuous_capture": False,
                        "replayable_by_epochs": False,
                        "research_eligible": False,
                    }
                if out is DeltaOutcome.APPLIED:
                    applied += 1
            close_epoch()
    except ValueError as exc:
        return {
            "ok": False,
            "status": "CHECKPOINT_HASH_MISMATCH",
            "error": str(exc),
            "epochs": epochs,
            "continuous_capture": False,
            "replayable_by_epochs": False,
            "research_eligible": False,
        }

    if not epochs:
        return {"ok": False, "status": "NO_EPOCHS", "continuous_capture": False, "replayable_by_epochs": False}

    replayable_by_epochs = all(e.get("ok") for e in epochs) and replayable_by_epochs
    # continuous_capture only if single epoch and no unobserved and no gaps
    if len(epochs) > 1 or unobserved:
        continuous_capture = False
    research_eligible = bool(
        continuous_capture
        and replayable_by_epochs
        and event_manifest.get("trigger_quality", "REAL_CROSS_IN") == "REAL_CROSS_IN"
    )
    last = epochs[-1]
    return {
        "ok": replayable_by_epochs,
        "status": "COMPLETE_REPLAYABLE_BY_EPOCHS" if replayable_by_epochs else "INCOMPLETE",
        "applied_deltas": sum(e["applied_deltas"] for e in epochs),
        "final_u": last["end_u"],
        "final_seq": last["end_seq"],
        "best_bid": last["best_bid"],
        "best_ask": last["best_ask"],
        "crossed": last["crossed"],
        "bid_levels": last["bid_levels"],
        "ask_levels": last["ask_levels"],
        "delta_files": [str(p) for p in delta_paths],
        "epochs": epochs,
        "continuous_capture": continuous_capture,
        "replayable_by_epochs": replayable_by_epochs,
        "continuity_epoch_count": len(epochs),
        "unobserved_intervals": unobserved,
        "research_eligible": research_eligible,
    }
