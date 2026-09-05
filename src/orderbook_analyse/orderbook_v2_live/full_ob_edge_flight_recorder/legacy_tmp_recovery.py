"""Offline recovery of orphaned Full-OB Flight Recorder .tmp streams.

Never writes the original .tmp. Refuses if any process still holds an fd on it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import orjson

from orderbook_analyse.orderbook_v2_live.full_book_state import FullBookState
from orderbook_analyse.orderbook_v2_live.full_ob_sync import DeltaOutcome

try:
    import zstandard as zstd
except ImportError:  # pragma: no cover
    zstd = None

FINALIZATION_REASON = "INTERRUPTED_BY_LEGACY_COLLECTOR_RESTART"
QUALITY_COMPLETE = "COMPLETE_RECOVERED"
QUALITY_INCOMPLETE = "INCOMPLETE_AT_LEGACY_RESTART"
OUTCOME_UNRESOLVED = "UNRESOLVED_INTERRUPTED_CAPTURE"


class RecoveryBlocked(RuntimeError):
    """Fail-closed: original still open or I/O policy violated."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pids_holding_path(path: Path) -> list[int]:
    """Return PIDs whose /proc/pid/fd symlink resolves to path."""
    target = path.resolve()
    holders: list[int] = []
    proc = Path("/proc")
    if not proc.exists():
        return holders
    target_s = str(target)
    for ent in proc.iterdir():
        if not ent.name.isdigit():
            continue
        fd_dir = ent / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                if link == target_s:
                    holders.append(int(ent.name))
                    break
                try:
                    if Path(link).resolve() == target:
                        holders.append(int(ent.name))
                        break
                except OSError:
                    continue
        except OSError:
            continue
    return sorted(set(holders))


def assert_no_open_fd(path: Path) -> None:
    holders = pids_holding_path(path)
    if holders:
        raise RecoveryBlocked(f"open_fd path={path} pids={holders}")


def _extract_jsonl_from_zstd_bytes(data: bytes) -> dict[str, Any]:
    if zstd is None:
        raise RuntimeError("zstandard required")
    dctx = zstd.ZstdDecompressor()
    out = bytearray()
    consumed = 0
    zstd_complete = True
    zstd_error = None
    dobj = dctx.decompressobj()
    n = len(data)
    i = 0
    while i < n:
        try:
            piece = dobj.decompress(data[i : i + 1])
            out.extend(piece)
            i += 1
            consumed = i
        except zstd.ZstdError as exc:
            zstd_complete = False
            zstd_error = str(exc)
            break
    try:
        tail = dobj.flush()
        out.extend(tail)
    except zstd.ZstdError as exc:
        zstd_complete = False
        zstd_error = zstd_error or str(exc)
    eof = bool(getattr(dobj, "eof", False))
    if not eof:
        zstd_complete = False
    unused = getattr(dobj, "unused_data", b"") or b""
    if unused:
        zstd_complete = False

    text = bytes(out)
    records: list[dict[str, Any]] = []
    last_complete_offset = 0
    pos = 0
    incomplete_json_tail = False
    while True:
        nl = text.find(b"\n", pos)
        if nl < 0:
            if pos < len(text) and text[pos:].strip():
                incomplete_json_tail = True
            break
        line = text[pos:nl]
        last_complete_offset = nl + 1
        pos = nl + 1
        if not line.strip():
            continue
        try:
            records.append(orjson.loads(line))
        except orjson.JSONDecodeError:
            incomplete_json_tail = True
            break

    trailing_unrecoverable = max(0, n - consumed)
    return {
        "records": records,
        "zstd_complete": bool(zstd_complete and eof and trailing_unrecoverable == 0 and not unused),
        "zstd_error": zstd_error,
        "eof": eof,
        "trailing_unrecoverable_bytes": trailing_unrecoverable,
        "last_complete_record_offset": last_complete_offset,
        "decompressed_bytes": len(text),
        "incomplete_json_tail": incomplete_json_tail,
        "consumed_compressed_bytes": consumed,
        "source_bytes": n,
    }


def _write_recovered_zst(records: list[dict[str, Any]], dest: Path) -> None:
    if zstd is None:
        raise RuntimeError("zstandard required")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as fh:
        writer = zstd.ZstdCompressor(level=3).stream_writer(fh, closefd=False)
        for rec in records:
            writer.write(orjson.dumps(rec) + b"\n")
        writer.flush(zstd.FLUSH_FRAME)
        writer.close()
        fh.flush()
        os.fsync(fh.fileno())


def _is_book_delta(obj: dict[str, Any]) -> bool:
    if obj.get("archive_event") or obj.get("channel") in {"lifecycle", "marker"}:
        return False
    data = obj.get("data") or {}
    return "u" in data or "u" in obj


def replay_records(records: list[dict[str, Any]], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    book = FullBookState(symbol=str((snapshot or {}).get("s") or "UNKNOWN"))
    if snapshot:
        book.apply_snapshot(
            bids=snapshot.get("b") or [],
            asks=snapshot.get("a") or [],
            u=snapshot.get("u"),
            seq=snapshot.get("seq"),
            ts_ms=snapshot.get("ts") or snapshot.get("cts"),
            cts_ms=snapshot.get("cts"),
            mark_ready=True,
        )
    applied = 0
    stale = 0
    gaps = 0
    first_u = None
    last_u = None
    first_seq = None
    last_seq = None
    gap_at = None
    for obj in records:
        if not _is_book_delta(obj):
            continue
        data = obj.get("data") or {}
        u = data.get("u")
        seq = data.get("seq")
        if u is not None:
            ui = int(u)
            if first_u is None:
                first_u = ui
            last_u = ui
        if seq is not None:
            si = int(seq)
            if first_seq is None:
                first_seq = si
            last_seq = si
        if not book.snapshot_loaded:
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
            gap_at = u
            break
        if out in {
            DeltaOutcome.IGNORED_STALE_U,
            DeltaOutcome.IGNORED_DUP_U,
            DeltaOutcome.IGNORED_DECREASING_SEQ,
        }:
            stale += 1
            continue
        if out is DeltaOutcome.APPLIED:
            applied += 1
    bb, ba = book.best_bid(), book.best_ask()
    crossed = bb is not None and ba is not None and bb >= ba
    ok = gaps == 0 and not crossed and snapshot is not None
    return {
        "ok": ok,
        "status": "COMPLETE_REPLAYABLE" if ok else ("UNRESOLVED" if crossed else "INCOMPLETE_SEQUENCE_GAP"),
        "applied_deltas": applied,
        "stale_or_dup": stale,
        "u_gap_count": gaps,
        "gap_at_u": gap_at,
        "first_u": first_u if first_u is not None else (snapshot.get("u") if snapshot else None),
        "last_u": last_u if last_u is not None else (snapshot.get("u") if snapshot else None),
        "first_seq": first_seq,
        "last_seq": last_seq,
        "best_bid": bb,
        "best_ask": ba,
        "book_crossed": crossed,
        "bid_levels": len(book.bids),
        "ask_levels": len(book.asks),
    }


def _load_snapshot_bytes(raw: bytes) -> dict[str, Any] | None:
    if zstd is None or not raw:
        return None
    try:
        return orjson.loads(zstd.ZstdDecompressor().decompress(raw))
    except Exception:
        return None


@dataclass
class LegacyRecoveryResult:
    blocked: bool = False
    block_reason: str | None = None
    out_dir: Path | None = None
    manifest: dict[str, Any] = field(default_factory=dict)


def recover_legacy_tmp(
    *,
    original_delta_tmp: Path,
    out_dir: Path,
    original_snapshot_tmp: Path | None = None,
    symbol: str = "",
    fight_event_id: str = "",
) -> LegacyRecoveryResult:
    original_delta_tmp = Path(original_delta_tmp)
    out_dir = Path(out_dir)
    try:
        assert_no_open_fd(original_delta_tmp)
        if original_snapshot_tmp is not None and original_snapshot_tmp.exists():
            assert_no_open_fd(original_snapshot_tmp)
    except RecoveryBlocked as exc:
        return LegacyRecoveryResult(blocked=True, block_reason=str(exc))

    orig_sha = sha256_file(original_delta_tmp)
    st = original_delta_tmp.stat()
    orig_size = st.st_size
    orig_mtime = st.st_mtime

    copy_root = out_dir / "original_tmp_copy"
    copy_root.mkdir(parents=True, exist_ok=True)
    delta_copy = copy_root / original_delta_tmp.name
    shutil.copy2(original_delta_tmp, delta_copy)
    copy_sha = sha256_file(delta_copy)
    if copy_sha != orig_sha:
        raise RuntimeError("copy_sha_mismatch")
    if sha256_file(original_delta_tmp) != orig_sha:
        raise RuntimeError("original_changed_during_copy")

    snap_copy = None
    snapshot = None
    snap_sha = None
    if original_snapshot_tmp is not None and original_snapshot_tmp.exists():
        snap_copy = copy_root / original_snapshot_tmp.name
        shutil.copy2(original_snapshot_tmp, snap_copy)
        snap_sha = sha256_file(snap_copy)
        snapshot = _load_snapshot_bytes(snap_copy.read_bytes())

    extracted = _extract_jsonl_from_zstd_bytes(delta_copy.read_bytes())
    records: list[dict[str, Any]] = extracted["records"]
    recovered_path = out_dir / "recovered_deltas.jsonl.zst"
    _write_recovered_zst(records, recovered_path)

    replay = replay_records(records, snapshot)
    complete = (
        extracted["zstd_complete"]
        and not extracted["incomplete_json_tail"]
        and replay.get("u_gap_count", 1) == 0
        and not replay.get("book_crossed")
        and bool(replay.get("ok"))
        and extracted["trailing_unrecoverable_bytes"] == 0
    )
    quality = QUALITY_COMPLETE if complete else QUALITY_INCOMPLETE
    first_ts = last_ts = None
    for rec in records:
        if not _is_book_delta(rec):
            continue
        ts = rec.get("ts") or (rec.get("data") or {}).get("ts")
        if ts is None:
            continue
        if first_ts is None:
            first_ts = ts
        last_ts = ts

    manifest = {
        "finalization_reason": FINALIZATION_REASON,
        "data_quality": quality,
        "outcome_status": OUTCOME_UNRESOLVED,
        "symbol": symbol or None,
        "fight_event_id": fight_event_id or None,
        "original_delta_tmp": str(original_delta_tmp),
        "original_size_bytes": orig_size,
        "original_mtime": orig_mtime,
        "original_sha256": orig_sha,
        "copy_sha256": copy_sha,
        "snapshot_sha256": snap_sha,
        "zstd_complete": extracted["zstd_complete"],
        "zstd_error": extracted["zstd_error"],
        "incomplete_json_tail": extracted["incomplete_json_tail"],
        "trailing_unrecoverable_bytes": extracted["trailing_unrecoverable_bytes"],
        "last_complete_record_offset": extracted["last_complete_record_offset"],
        "consumed_compressed_bytes": extracted["consumed_compressed_bytes"],
        "recovered_record_count": len(records),
        "first_u": replay.get("first_u"),
        "last_u": replay.get("last_u"),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "u_gap_count": replay.get("u_gap_count"),
        "stale_or_dup": replay.get("stale_or_dup"),
        "replay_status": replay.get("status"),
        "book_crossed": replay.get("book_crossed"),
        "natural_fight_outcome_complete": False,
        "recovered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    (out_dir / "recovery_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out_dir / "replay_report.json").write_text(json.dumps(replay, indent=2, sort_keys=True) + "\n")
    sums = [
        f"{orig_sha}  original_tmp_copy/{original_delta_tmp.name}",
        f"{sha256_file(recovered_path)}  recovered_deltas.jsonl.zst",
        f"{sha256_file(out_dir / 'recovery_manifest.json')}  recovery_manifest.json",
        f"{sha256_file(out_dir / 'replay_report.json')}  replay_report.json",
    ]
    if snap_copy is not None and original_snapshot_tmp is not None:
        sums.insert(1, f"{snap_sha}  original_tmp_copy/{original_snapshot_tmp.name}")
    (out_dir / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    return LegacyRecoveryResult(blocked=False, out_dir=out_dir, manifest=manifest)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Recover orphaned Full-OB FR .tmp (copy-only)")
    p.add_argument("--delta-tmp", required=True)
    p.add_argument("--snapshot-tmp", default="")
    p.add_argument("--out-dir", required=True)
    p.add_argument("--symbol", default="")
    p.add_argument("--fight-event-id", default="")
    args = p.parse_args(argv)
    snap = Path(args.snapshot_tmp) if args.snapshot_tmp else None
    res = recover_legacy_tmp(
        original_delta_tmp=Path(args.delta_tmp),
        original_snapshot_tmp=snap,
        out_dir=Path(args.out_dir),
        symbol=args.symbol,
        fight_event_id=args.fight_event_id,
    )
    if res.blocked:
        print(json.dumps({"ok": False, "blocked": True, "reason": res.block_reason}))
        return 2
    print(json.dumps({"ok": True, "data_quality": res.manifest.get("data_quality"), "out": str(res.out_dir)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
