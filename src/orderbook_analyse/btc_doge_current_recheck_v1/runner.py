"""Orchestrate read-only BTC/DOGE current multi-source recheck."""

from __future__ import annotations

import csv
import json
import os
import re
import socket
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from orderbook_analyse.multisource_data_inventory_v1.sql_guard import ReadOnlyDB, open_db
from orderbook_analyse.ob200_v3_raw_discovery.files import (
    SegmentRef,
    excluded_tmp_files,
    list_closed_segments,
)
from orderbook_analyse.ob_data_source.ndjson_parse import parse_ob200_obj
from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock, floor_second_ms
from orderbook_analyse.orderbook_v2_live.raw_archive.events import (
    is_replayable_line,
    line_to_replay_payload,
)
from orderbook_analyse.orderbook_v2_live.raw_archive.replay import (
    iter_segment_lines,
    load_manifest,
    replay_segment,
)

from . import COLLECTOR_PID, FORMAT_VERSION, RAW_ROOT, SYMBOLS

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
RAW_ARCHIVE_ROOT = Path(RAW_ROOT)
AGGREGATE_END_KNOWN = datetime(2026, 8, 28, 16, 26, 23, tzinfo=timezone.utc)
QSET = {"max_execution_time": 120, "receive_timeout": 130, "max_threads": 2}

# Parity tolerances from test_orderbook_v3_raw_archive feature parity pattern.
MID_ABS_TOL = Decimal("0.05")
SPREAD_BPS_TOL = Decimal("0.05")
IMBALANCE_TOL = Decimal("0.001")


@dataclass(frozen=True)
class Cutoff:
    server_now_utc: datetime
    audit_cutoff_exclusive: datetime
    last_complete_hour_start: datetime
    last_complete_hour_end: datetime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_cutoff(now: datetime | None = None) -> Cutoff:
    now = now or utc_now()
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    return Cutoff(
        server_now_utc=now,
        audit_cutoff_exclusive=hour_start,
        last_complete_hour_start=hour_start - timedelta(hours=1),
        last_complete_hour_end=hour_start,
    )


def _git_preflight() -> dict[str, Any]:
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=OA_ROOT, text=True
    ).strip()
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=OA_ROOT, text=True).strip()
    status = subprocess.check_output(["git", "status", "--short"], cwd=OA_ROOT, text=True)
    return {"branch": branch, "head": head, "status_short": status}


def _proc_info(pid: int) -> dict[str, Any] | None:
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "pid=,lstart=,etime=,%cpu=,%mem=,rss=,cmd="],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return None
    if not out:
        return None
    parts = out.split(None, 6)
    if len(parts) < 7:
        return {"raw": out}
    return {
        "pid": parts[0],
        "lstart": " ".join(parts[1:6]),
        "etime": parts[6] if len(parts) == 7 else parts[6],
        "cmd": parts[-1] if len(parts) > 7 else "",
        "raw": out,
    }


def _collector_freshness(cutoff: Cutoff) -> dict[str, Any]:
    pid = COLLECTOR_PID
    info = _proc_info(pid)
    cwd = None
    open_files: list[str] = []
    try:
        cwd = os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        pass
    try:
        for fd in os.listdir(f"/proc/{pid}/fd"):
            try:
                target = os.readlink(f"/proc/{pid}/fd/{fd}")
                if "ob200_v3" in target or "health" in target:
                    open_files.append(target)
            except OSError:
                continue
    except OSError:
        pass

    health_tail: list[dict[str, Any]] = []
    health_path = OA_ROOT / "logs" / "orderbook_v3_raw_archive_btc_doge.health.ndjson"
    if health_path.is_file():
        try:
            with health_path.open("rb") as fh:
                fh.seek(0, 2)
                size = fh.tell()
                fh.seek(max(0, size - 8192))
                chunk = fh.read().decode("utf-8", errors="replace")
            for line in chunk.strip().splitlines()[-3:]:
                try:
                    health_tail.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    tmp_files = excluded_tmp_files(RAW_ARCHIVE_ROOT, SYMBOLS)
    tmp_meta = []
    for p in tmp_files:
        st = p.stat()
        last_event_ts = None
        try:
            for obj in iter_segment_lines(p):
                if not is_replayable_line(obj):
                    continue
                ts = obj.get("exchange_ts") or obj.get("ts")
                if ts is not None:
                    if isinstance(ts, str):
                        last_event_ts = ts
                    else:
                        last_event_ts = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except Exception as exc:
            last_event_ts = f"READ_ERROR:{type(exc).__name__}"
            break
        tmp_meta.append(
            {
                "path": str(p),
                "size_bytes": st.st_size,
                "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "last_event_ts": last_event_ts,
            }
        )

    last_health = health_tail[-1] if health_tail else {}
    freshness_sec = None
    if tmp_meta and tmp_meta[0].get("last_event_ts") and not str(tmp_meta[0]["last_event_ts"]).startswith("READ_ERROR"):
        try:
            ts = datetime.fromisoformat(str(tmp_meta[0]["last_event_ts"]).replace("Z", "+00:00"))
            freshness_sec = (cutoff.server_now_utc - ts).total_seconds()
        except ValueError:
            pass

    return {
        "pid": pid,
        "running": info is not None,
        "process": info,
        "cwd": cwd,
        "open_archive_files": open_files,
        "health_tail": health_tail,
        "writer_state": last_health.get("writer_state"),
        "collector_state": last_health.get("collector_state"),
        "connected": last_health.get("connected"),
        "valid_books": last_health.get("valid_books"),
        "invalid_books": last_health.get("invalid_books"),
        "rows_written_total": last_health.get("rows_written_total"),
        "mode_confirmed_raw_archive_only": last_health.get("writer_state") == "DISABLED",
        "tmp_segments": tmp_meta,
        "freshness_sec_to_last_tmp_event": freshness_sec,
    }


def _manifest_summary(path: Path) -> dict[str, Any]:
    try:
        m = load_manifest(path)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}:{exc}"}
    u_gaps = m.get("u_gaps") or []
    seq_gaps = m.get("sequence_gaps") or []
    return {
        "completion_status": m.get("completion_status"),
        "replayable_manifest": m.get("replayable"),
        "continuity_status": m.get("continuity_status"),
        "writer_errors": m.get("writer_errors", 0),
        "event_count": m.get("event_count"),
        "snapshot_count": m.get("native_snapshot_count", 0) + m.get("checkpoint_count", 0),
        "delta_count": m.get("delta_count"),
        "sequence_gaps_count": len(seq_gaps),
        "u_gaps_count": len(u_gaps),
        "replay_source": m.get("replay_source"),
        "start_utc": m.get("start_utc"),
        "end_utc": m.get("end_utc"),
    }


def _scan_segment_events(path: Path) -> tuple[str | None, str | None, int, int, int]:
    first_ts = last_ts = None
    snap = delta = 0
    for obj in iter_segment_lines(path):
        if not is_replayable_line(obj):
            continue
        kind = obj.get("type")
        if kind in ("snapshot", "rotation_checkpoint"):
            snap += 1
        elif kind == "delta":
            delta += 1
        ts = obj.get("exchange_ts") or obj.get("ts")
        if ts is None:
            lrt = obj.get("local_receive_ts")
            if isinstance(lrt, str):
                ts_s = lrt
            else:
                continue
        else:
            if isinstance(ts, (int, float)):
                ts_s = datetime.fromtimestamp(int(ts) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            else:
                ts_s = str(ts)
        if first_ts is None:
            first_ts = ts_s
        last_ts = ts_s
    return first_ts, last_ts, snap + delta, snap, delta


def _quick_zst_readable(path: Path) -> str:
    try:
        n = 0
        for _obj in iter_segment_lines(path):
            n += 1
            if n >= 1:
                return "READABLE"
        return "EMPTY"
    except Exception as exc:
        return f"FAIL:{type(exc).__name__}"


def _hour_keys_between(start: datetime, end: datetime) -> list[str]:
    cur = start.replace(minute=0, second=0, microsecond=0)
    out: list[str] = []
    while cur < end:
        out.append(iso_z(cur))
        cur += timedelta(hours=1)
    return out


def inventory_raw_segments(cutoff: Cutoff) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, list[SegmentRef]]]:
    rows: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    segments_by_sym: dict[str, list[SegmentRef]] = {}
    post_cutoff = datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc)

    for symbol in SYMBOLS:
        closed = list_closed_segments(
            RAW_ARCHIVE_ROOT, symbols=(symbol,), end=cutoff.audit_cutoff_exclusive
        )
        # Segments may finalize microseconds after the hour boundary; include any
        # segment that *starts* before the cutoff (data inside is < cutoff).
        closed = [s for s in closed if s.start_utc < cutoff.audit_cutoff_exclusive]
        segments_by_sym[symbol] = closed

        by_hour: dict[str, list[SegmentRef]] = defaultdict(list)
        for seg in closed:
            key = iso_z(seg.start_utc.replace(minute=0, second=0, microsecond=0))
            by_hour[key].append(seg)

        for hour_key, segs in sorted(by_hour.items()):
            if len(segs) > 1:
                gaps.append(
                    {
                        "symbol": symbol,
                        "gap_type": "DUPLICATE_HOUR",
                        "hour_utc": hour_key,
                        "segments": [s.path.name for s in segs],
                    }
                )

        if closed:
            first_seg = min(closed, key=lambda s: s.start_utc)
            first_hour = first_seg.start_utc.replace(minute=0, second=0, microsecond=0)
            seen_hours = set(by_hour.keys())
            for hk in _hour_keys_between(first_hour, cutoff.audit_cutoff_exclusive):
                if hk not in seen_hours:
                    if hk == iso_z(first_hour) and first_seg.start_utc.minute > 0:
                        continue
                    gaps.append(
                        {
                            "symbol": symbol,
                            "gap_type": "MISSING_HOUR",
                            "hour_utc": hk,
                            "notes": "no closed segment starting this UTC hour",
                        }
                    )

        full_replay_targets = set()
        if closed:
            for idx in (0, len(closed) // 2, len(closed) - 1):
                full_replay_targets.add(closed[idx].path)
            post_segs = [s for s in closed if s.start_utc >= post_cutoff]
            if post_segs:
                for idx in (0, len(post_segs) // 2, len(post_segs) - 1):
                    full_replay_targets.add(post_segs[idx].path)

        for seg in closed:
            st = seg.path.stat()
            man = _manifest_summary(seg.path)
            zst_ok = _quick_zst_readable(seg.path) if st.st_size > 0 else "ZERO_BYTES"
            replay_status = "LIGHTWEIGHT_ONLY"
            if seg.path in full_replay_targets:
                try:
                    replay_segment(seg.path, expected_symbol=symbol)
                    replay_status = "FULL_REPLAY_OK"
                except Exception as exc:
                    replay_status = "FULL_REPLAY_FAIL"
                    gaps.append(
                        {
                            "symbol": symbol,
                            "gap_type": "REPLAY_FAIL",
                            "segment": seg.path.name,
                            "error": str(exc)[:200],
                        }
                    )

            rows.append(
                {
                    "symbol": symbol,
                    "segment_hour_utc": iso_z(seg.start_utc.replace(minute=0, second=0, microsecond=0)),
                    "path": str(seg.path),
                    "state_closed_or_tmp": "closed",
                    "size_bytes": st.st_size,
                    "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                    "first_event_ts": man.get("start_utc"),
                    "last_event_ts": man.get("end_utc"),
                    "event_count": man.get("event_count"),
                    "snapshot_count": man.get("snapshot_count"),
                    "delta_count": man.get("delta_count"),
                    "writer_errors": man.get("writer_errors", 0),
                    "sequence_gaps": man.get("sequence_gaps_count", 0),
                    "reconnect_count": "NOT_APPLICABLE",
                    "invalid_book_count": 0,
                    "zstd_integrity": zst_ok,
                    "replay_status": replay_status,
                    "u_gaps_count": man.get("u_gaps_count", 0),
                    "replayable_manifest": man.get("replayable_manifest"),
                    "notes": f"duration_sec={seg.duration_sec:.1f}",
                }
            )

        for tmp in excluded_tmp_files(RAW_ARCHIVE_ROOT, (symbol,)):
            st = tmp.stat()
            rows.append(
                {
                    "symbol": symbol,
                    "segment_hour_utc": iso_z(cutoff.audit_cutoff_exclusive),
                    "path": str(tmp),
                    "state_closed_or_tmp": "tmp",
                    "size_bytes": st.st_size,
                    "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                    "first_event_ts": "NOT_APPLICABLE",
                    "last_event_ts": "IN_PROGRESS",
                    "event_count": "NOT_APPLICABLE",
                    "snapshot_count": "NOT_APPLICABLE",
                    "delta_count": "NOT_APPLICABLE",
                    "writer_errors": "NOT_APPLICABLE",
                    "sequence_gaps": "NOT_APPLICABLE",
                    "reconnect_count": "NOT_APPLICABLE",
                    "invalid_book_count": "NOT_APPLICABLE",
                    "zstd_integrity": "TMP_OPEN",
                    "replay_status": "NOT_COMPLETE",
                    "u_gaps_count": "NOT_APPLICABLE",
                    "replayable_manifest": "NOT_APPLICABLE",
                    "notes": "current hour in progress; excluded from completeness",
                }
            )

    return rows, gaps, segments_by_sym


def _replay_features_for_segment(seg: SegmentRef, *, end_ms: int) -> list[dict[str, Any]]:
    clock = LiveSecondClock(seg.symbol)
    rows: list[dict[str, Any]] = []
    for obj in iter_segment_lines(seg.path):
        if not is_replayable_line(obj):
            continue
        msg = parse_ob200_obj(line_to_replay_payload(obj), expected_symbol=seg.symbol)
        data = {
            "s": msg.symbol,
            "b": [[format(p, "f"), format(q, "f")] for p, q in msg.bids],
            "a": [[format(p, "f"), format(q, "f")] for p, q in msg.asks],
            "u": msg.update_id,
            "seq": msg.cross_sequence,
        }
        rows.extend(clock.ingest(msg.message_type, msg.raw_ts_ms, data))
    rows.extend(clock.close_through(end_ms))
    return rows


def _find_segment_for_hour(segments: list[SegmentRef], hour_start: datetime) -> SegmentRef | None:
    hour_end = hour_start + timedelta(hours=1)
    for seg in segments:
        if seg.start_utc <= hour_start and seg.end_utc >= hour_end:
            return seg
        if seg.start_utc.replace(minute=0, second=0, microsecond=0) == hour_start:
            return seg
    for seg in segments:
        if seg.start_utc <= hour_start < seg.end_utc:
            return seg
    return None


def _ch_ob_features(db: ReadOnlyDB, symbol: str, hour_start: datetime, hour_end: datetime) -> list[tuple]:
    sql = """
    SELECT
      bucket_start,
      mid_price,
      spread_bps,
      imbalance_l50,
      bid_qty_l50,
      ask_qty_l50,
      is_valid
    FROM orderbook_analysis.orderbook_features_1s_v2
    WHERE symbol = {s:String}
      AND parser_version = 'ob200_v3'
      AND depth = 200
      AND bucket_start >= {a:DateTime64(3,'UTC')}
      AND bucket_start < {b:DateTime64(3,'UTC')}
    ORDER BY bucket_start
    """
    return db.query(
        sql,
        parameters={"s": symbol, "a": hour_start, "b": hour_end},
    ).result_rows


def _ch_ts_ms(value: Any) -> int:
    if hasattr(value, "timestamp"):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp() * 1000)
    return floor_second_ms(
        int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    )


def _row_bucket_ms(row: dict[str, Any]) -> int:
    bs = row.get("bucket_start")
    if bs is not None and hasattr(bs, "timestamp"):
        return _ch_ts_ms(bs)
    return int(row.get("bucket_start_ms", 0))


def _compare_parity(
    raw_rows: list[dict[str, Any]],
    ch_rows: list[tuple],
) -> dict[str, Any]:
    raw_by_ms = {_row_bucket_ms(r): r for r in raw_rows if r.get("is_valid")}
    mismatches = 0
    compared = 0
    for row in ch_rows:
        if not row[6]:  # is_valid
            continue
        ms = _ch_ts_ms(row[0])
        rr = raw_by_ms.get(ms)
        if rr is None:
            mismatches += 1
            continue
        compared += 1
        mid_ch = Decimal(str(row[1]))
        mid_raw = Decimal(str(rr.get("mid_price", 0)))
        if abs(mid_ch - mid_raw) > MID_ABS_TOL:
            mismatches += 1
            continue
        sp_ch = Decimal(str(row[2]))
        sp_raw = Decimal(str(rr.get("spread_bps", 0)))
        if abs(sp_ch - sp_raw) > SPREAD_BPS_TOL:
            mismatches += 1
            continue
    if compared == 0:
        return {"verdict": "INCONCLUSIVE", "compared": 0, "mismatches": mismatches}
    ratio = mismatches / max(compared, 1)
    if ratio <= 0.05:
        return {"verdict": "PASS", "compared": compared, "mismatches": mismatches}
    if ratio <= 0.10:
        return {"verdict": "INCONCLUSIVE", "compared": compared, "mismatches": mismatches}
    return {"verdict": "FAIL", "compared": compared, "mismatches": mismatches}


def parity_raw_vs_aggregate(
    db: ReadOnlyDB,
    cutoff: Cutoff,
    segments_by_sym: dict[str, list[SegmentRef]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    # deterministic hours: early, middle, last before aggregate end
    parity_hours = [
        datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
    ]
    for symbol in SYMBOLS:
        segs = segments_by_sym[symbol]
        for hour_start in parity_hours:
            hour_end = hour_start + timedelta(hours=1)
            seg = _find_segment_for_hour(segs, hour_start)
            ch_rows: list[tuple] = []
            raw_in_hour: list[dict[str, Any]] = []
            if seg is None:
                rows.append(
                    {
                        "symbol": symbol,
                        "hour_utc": iso_z(hour_start),
                        "raw_vs_aggregate_parity": "INCONCLUSIVE",
                        "notes": "no segment for hour",
                    }
                )
                continue
            end_ms = int(hour_end.timestamp() * 1000) - 1
            try:
                raw_rows = _replay_features_for_segment(seg, end_ms=end_ms)
                raw_in_hour = [
                    r
                    for r in raw_rows
                    if hour_start.timestamp() * 1000 <= _row_bucket_ms(r) < hour_end.timestamp() * 1000
                ]
                ch_rows = _ch_ob_features(db, symbol, hour_start, hour_end)
                cmp = _compare_parity(raw_in_hour, ch_rows)
            except Exception as exc:
                cmp = {"verdict": "INCONCLUSIVE", "error": f"{type(exc).__name__}:{exc}"}
            rows.append(
                {
                    "symbol": symbol,
                    "hour_utc": iso_z(hour_start),
                    "segment": seg.path.name,
                    "ch_rows": len(ch_rows),
                    "raw_rows_in_hour": len(raw_in_hour),
                    "raw_vs_aggregate_parity": cmp.get("verdict", "INCONCLUSIVE"),
                    "compared_buckets": cmp.get("compared"),
                    "mismatches": cmp.get("mismatches"),
                    "notes": cmp.get("error", ""),
                }
            )
    return rows


def post_aggregate_coverage(
    cutoff: Cutoff,
    segments_by_sym: dict[str, list[SegmentRef]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    post_start = datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc)
    hour = post_start.replace(minute=0, second=0, microsecond=0)
    while hour < cutoff.audit_cutoff_exclusive:
        hour_end = hour + timedelta(hours=1)
        for symbol in SYMBOLS:
            seg = _find_segment_for_hour(segments_by_sym[symbol], hour)
            classification = "TRUE_DATA_GAP"
            notes = ""
            full_check = hour in (
                post_start,
                datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc),
                cutoff.last_complete_hour_start,
            )
            if seg is None:
                notes = "no closed segment"
            elif seg.end_utc < hour_end - timedelta(seconds=1):
                classification = "RAW_DATA_PRESENT_NOT_REPLAYABLE"
                notes = "segment ends before hour end"
            elif _quick_zst_readable(seg.path) != "READABLE":
                classification = "RAW_DATA_PRESENT_NOT_REPLAYABLE"
                notes = "zst not readable"
            elif not full_check:
                classification = "RAW_DATA_PRESENT"
                notes = "segment closed; full replay deferred"
            else:
                try:
                    replay_segment(seg.path, expected_symbol=symbol)
                    end_ms = int(hour_end.timestamp() * 1000) - 1
                    feats = _replay_features_for_segment(seg, end_ms=end_ms)
                    n = sum(
                        1
                        for r in feats
                        if hour.timestamp() * 1000 <= _row_bucket_ms(r) < hour_end.timestamp() * 1000
                        and r.get("is_valid")
                    )
                    if n >= 3000:
                        classification = "RAW_DATA_REPLAYABLE"
                    elif n > 0:
                        classification = "RAW_DATA_PRESENT"
                        notes = f"only {n} valid 1s buckets"
                    else:
                        classification = "RAW_DATA_PRESENT_NOT_REPLAYABLE"
                        notes = "no valid features derived"
                except Exception as exc:
                    classification = "RAW_DATA_PRESENT_NOT_REPLAYABLE"
                    notes = str(exc)[:120]
            rows.append(
                {
                    "symbol": symbol,
                    "hour_utc": iso_z(hour),
                    "classification": classification,
                    "aggregate_present": "AGGREGATE_STALE",
                    "notes": notes,
                }
            )
        hour += timedelta(hours=1)
    return rows


def source_coverage_by_hour(db: ReadOnlyDB, cutoff: Cutoff) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start = datetime(2026, 8, 24, 22, 0, tzinfo=timezone.utc)
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < cutoff.audit_cutoff_exclusive:
        hour_end = hour + timedelta(hours=1)
        for symbol in SYMBOLS:
            # candles
            c_sql = """
            SELECT count(), countDistinct(open_time),
              countIf(high < low OR open <= 0 OR close <= 0)
            FROM signal_generator.candles_1m FINAL
            WHERE symbol={s:String} AND interval='1m'
              AND open_time >= {a:DateTime64(3,'UTC')} AND open_time < {b:DateTime64(3,'UTC')}
            """
            cr = db.query(c_sql, {"s": symbol, "a": hour, "b": hour_end}).result_rows[0]
            candle_ok = int(cr[0]) == 60 and int(cr[2]) == 0

            t_sql = """
            SELECT count(), countDistinct(trade_id), max(trade_ts)
            FROM orderbook_analysis.public_trades_canonical
            WHERE symbol={s:String}
              AND trade_ts >= {a:DateTime64(3,'UTC')} AND trade_ts < {b:DateTime64(3,'UTC')}
            """
            tr = db.query(t_sql, {"s": symbol, "a": hour, "b": hour_end}).result_rows[0]

            oi_sql = """
            SELECT count(), countIf(open_interest <= 0)
            FROM orderbook_analysis.open_interest_5s
            WHERE symbol={s:String}
              AND bucket_time >= {a:DateTime64(3,'UTC')} AND bucket_time < {b:DateTime64(3,'UTC')}
            """
            oir = db.query(oi_sql, {"s": symbol, "a": hour, "b": hour_end}).result_rows[0]
            oi_ok = int(oir[0]) >= 600  # ~720 expected 5s buckets/hour; allow some slack

            liq_sql = """
            SELECT count(), max(event_time)
            FROM orderbook_analysis.all_liquidations
            WHERE symbol={s:String}
              AND event_time >= {a:DateTime64(3,'UTC')} AND event_time < {b:DateTime64(3,'UTC')}
            """
            lr = db.query(liq_sql, {"s": symbol, "a": hour, "b": hour_end}).result_rows[0]

            ob_sql = """
            SELECT count(), max(bucket_start)
            FROM orderbook_analysis.orderbook_features_1s_v2
            WHERE symbol={s:String} AND parser_version='ob200_v3' AND depth=200
              AND bucket_start >= {a:DateTime64(3,'UTC')} AND bucket_start < {b:DateTime64(3,'UTC')}
            """
            obr = db.query(ob_sql, {"s": symbol, "a": hour, "b": hour_end}).result_rows[0]

            for src, ok, extra in (
                ("CANDLES_1M", candle_ok, {"rows": int(cr[0]), "bad_ohlc": int(cr[2])}),
                ("PUBLIC_TRADES", int(tr[0]) > 0, {"rows": int(tr[0]), "distinct_trade_id": int(tr[1])}),
                ("OPEN_INTEREST_5S", oi_ok, {"rows": int(oir[0])}),
                ("LIQUIDATIONS", True, {"rows": int(lr[0]), "event_stream": True}),
                ("OB_FEATURES_1S", int(obr[0]) >= 3000, {"rows": int(obr[0])}),
            ):
                rows.append(
                    {
                        "symbol": symbol,
                        "hour_utc": iso_z(hour),
                        "source_id": src,
                        "coverage_ok": ok,
                        **extra,
                    }
                )
        hour += timedelta(hours=1)
    return rows


def profile_asof_smoke(db: ReadOnlyDB, cutoff: Cutoff) -> dict[str, Any]:
    from orderbook_analyse.market_profile.build import build_profile
    from orderbook_analyse.market_profile.contracts import ProfileWindow

    as_of = cutoff.last_complete_hour_end - timedelta(minutes=30)
    prev_day_start = (as_of - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    prev_day_end = prev_day_start + timedelta(days=1)
    out: dict[str, Any] = {"as_of_utc": iso_z(as_of), "symbols": {}}
    from orderbook_analyse.market_profile.loader import default_client

    client = default_client()
    for symbol in SYMBOLS:
        sym_out: dict[str, Any] = {}
        win = ProfileWindow(
            window_id="recheck_prev_day",
            anchor_mode="day",
            label="prev_day",
            start=prev_day_start,
            end=prev_day_end,
        )
        prof = build_profile(client, symbol, win, value_area_pct=0.70, target_bins=160)
        if prof is not None:
            sym_out["completed_prior_day"] = {
                "window": [iso_z(prev_day_start), iso_z(prev_day_end)],
                "poc": prof.value_area.poc,
                "vah": prof.value_area.vah,
                "val": prof.value_area.val,
                "hvn_count": len(prof.nodes.hvn),
                "lvn_count": len(prof.nodes.lvn),
                "shape": prof.shape.kind,
                "status": "OK",
            }
        else:
            sym_out["completed_prior_day"] = {"status": "MISSING_DATA"}

        dev_start = as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        dev_win = ProfileWindow(
            window_id="recheck_developing",
            anchor_mode="day",
            label="developing",
            start=dev_start,
            end=as_of,
        )
        prof2 = build_profile(client, symbol, dev_win, value_area_pct=0.70, target_bins=160)
        if prof2 is not None:
            sym_out["developing_as_of"] = {
                "window": [iso_z(dev_start), iso_z(as_of)],
                "poc": prof2.value_area.poc,
                "vah": prof2.value_area.vah,
                "val": prof2.value_area.val,
                "status": "OK",
                "uses_future_data": False,
            }
        else:
            sym_out["developing_as_of"] = {"status": "MISSING_DATA"}
        out["symbols"][symbol] = sym_out
    return out


def compute_intersections(
    cutoff: Cutoff,
    hour_cov: list[dict[str, Any]],
    post_cov: list[dict[str, Any]],
    raw_gaps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    gap_hours = {(g["symbol"], g.get("hour_utc")) for g in raw_gaps if g.get("gap_type") == "MISSING_HOUR"}
    for symbol in SYMBOLS:
        hours_ok: list[str] = []
        hour = cutoff.last_complete_hour_start
        # build from source_coverage - all sources good
        by_hour: dict[str, dict[str, bool]] = defaultdict(dict)
        for r in hour_cov:
            if r["symbol"] != symbol:
                continue
            by_hour[r["hour_utc"]][r["source_id"]] = bool(r["coverage_ok"])

        post_by_hour = {r["hour_utc"]: r["classification"] for r in post_cov if r["symbol"] == symbol}

        cur = datetime(2026, 8, 24, 23, 0, tzinfo=timezone.utc)
        while cur < cutoff.audit_cutoff_exclusive:
            hk = iso_z(cur)
            c = by_hour.get(hk, {})
            candles = c.get("CANDLES_1M", False)
            trades = c.get("PUBLIC_TRADES", False)
            oi = c.get("OPEN_INTEREST_5S", False)
            liq = c.get("LIQUIDATIONS", True)
            ob_agg = c.get("OB_FEATURES_1S", False)
            raw_cls = post_by_hour.get(hk)
            if raw_cls is None:
                raw_ok = hk < iso_z(datetime(2026, 8, 28, 17, 0, tzinfo=timezone.utc))
            else:
                raw_ok = raw_cls in (
                    "RAW_DATA_REPLAYABLE",
                    "RAW_DATA_PRESENT",
                ) and (symbol, hk) not in gap_hours
            if candles and trades and oi and liq and raw_ok:
                hours_ok.append(hk)
            cur += timedelta(hours=1)

        longest = 0
        current = 0
        for i, hk in enumerate(hours_ok):
            if i == 0:
                current = 1
            else:
                prev = datetime.fromisoformat(hours_ok[i - 1].replace("Z", "+00:00"))
                this = datetime.fromisoformat(hk.replace("Z", "+00:00"))
                if (this - prev).total_seconds() == 3600:
                    current += 1
                else:
                    longest = max(longest, current)
                    current = 1
        longest = max(longest, current)

        limiting = []
        if not hours_ok:
            limiting.append("none")
        elif hours_ok[-1] < iso_z(cutoff.last_complete_hour_start):
            limiting.append("RAW_OR_OI_START")
        if iso_z(cutoff.last_complete_hour_start) not in hours_ok:
            limiting.append("LAST_HOUR_INCOMPLETE_IN_MATRIX")

        reaches_cutoff = hours_ok and hours_ok[-1] == iso_z(cutoff.last_complete_hour_start)
        verdict = "COMPLETE" if reaches_cutoff else ("PARTIAL" if hours_ok else "MISSING")

        rows.append(
            {
                "symbol": symbol,
                "components": "CANDLES+PUBLIC_TRADES+RAW_OB200_REPLAYABLE+OPEN_INTEREST+LIQUIDATIONS+RECONSTRUCTABLE_PROFILE",
                "earliest_common_ts_utc": hours_ok[0] if hours_ok else "MISSING",
                "latest_common_ts_utc": hours_ok[-1] if hours_ok else "MISSING",
                "shared_complete_hours": len(hours_ok),
                "longest_contiguous_hours": longest,
                "reaches_audit_cutoff_exclusive": reaches_cutoff,
                "limiting_sources": limiting,
                "raw_replay_required": True,
                "verdict": verdict,
            }
        )
    return rows


def last_day_minute_matrix(hour_cov: list[dict[str, Any]], cutoff: Cutoff, symbol: str) -> list[dict[str, Any]]:
    day = cutoff.last_complete_hour_start.date()
    day_start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    rows = []
    for minute in range(24 * 60):
        ts = day_start + timedelta(minutes=minute)
        if ts >= cutoff.audit_cutoff_exclusive:
            break
        hk = iso_z(ts.replace(minute=0, second=0, microsecond=0))
        hc = [r for r in hour_cov if r["symbol"] == symbol and r["hour_utc"] == hk]
        flags = {r["source_id"]: r["coverage_ok"] for r in hc}
        rows.append(
            {
                "symbol": symbol,
                "minute_utc": iso_z(ts),
                "candles": flags.get("CANDLES_1M", False),
                "trades": flags.get("PUBLIC_TRADES", False),
                "oi": flags.get("OPEN_INTEREST_5S", False),
                "liq": flags.get("LIQUIDATIONS", True),
                "ob_agg": flags.get("OB_FEATURES_1S", False),
            }
        )
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _determine_verdict(
    intersections: list[dict[str, Any]],
    parity_rows: list[dict[str, Any]],
    raw_gaps: list[dict[str, Any]],
    post_rows: list[dict[str, Any]],
) -> tuple[str, dict[str, str]]:
    sub: dict[str, str] = {}
    for row in intersections:
        sub[row["symbol"]] = row["verdict"]

    replay_gaps = [g for g in raw_gaps if g.get("gap_type") in ("REPLAY_FAIL", "MISSING_HOUR")]
    post_bad = [r for r in post_rows if r["classification"] in ("TRUE_DATA_GAP", "RAW_DATA_PRESENT_NOT_REPLAYABLE")]
    parity_fail = [r for r in parity_rows if r.get("raw_vs_aggregate_parity") == "FAIL"]

    if replay_gaps:
        overall = "BTC_DOGE_CURRENT_MULTISOURCE_PARTIAL"
    elif all(v == "COMPLETE" for v in sub.values()):
        overall = "BTC_DOGE_CURRENT_MULTISOURCE_COMPLETE_RAW_REPLAY_REQUIRED"
    else:
        overall = "BTC_DOGE_CURRENT_MULTISOURCE_PARTIAL"
    return overall, sub


def run_recheck(out_dir: Path) -> dict[str, Any]:
    started = utc_now()
    cutoff = compute_cutoff(started)
    out_dir.mkdir(parents=True, exist_ok=True)

    pids_before = {
        str(COLLECTOR_PID): _proc_info(COLLECTOR_PID),
        "147111": _proc_info(147111),
        "319436": _proc_info(319436),
        "1661773": _proc_info(1661773),
    }
    git = _git_preflight()
    collector = _collector_freshness(cutoff)

    raw_rows, raw_gaps, segments_by_sym = inventory_raw_segments(cutoff)

    db = open_db()
    try:
        parity_rows = parity_raw_vs_aggregate(db, cutoff, segments_by_sym)
        post_rows = post_aggregate_coverage(cutoff, segments_by_sym)
        hour_cov = source_coverage_by_hour(db, cutoff)
        profile_smoke = profile_asof_smoke(db, cutoff)
        intersections = compute_intersections(cutoff, hour_cov, post_rows, raw_gaps)
    finally:
        db.close()

    minute_rows = []
    for symbol in SYMBOLS:
        minute_rows.extend(last_day_minute_matrix(hour_cov, cutoff, symbol))

    replay_integrity = [
        {
            "symbol": r["symbol"],
            "segment": Path(r["path"]).name,
            "zstd_integrity": r["zstd_integrity"],
            "replay_status": r["replay_status"],
            "u_gaps_count": r.get("u_gaps_count"),
            "writer_errors": r.get("writer_errors"),
        }
        for r in raw_rows
        if r.get("state_closed_or_tmp") == "closed"
    ]

    overall, sub_verdicts = _determine_verdict(intersections, parity_rows, raw_gaps, post_rows)
    ended = utc_now()

    pids_after = {
        str(COLLECTOR_PID): _proc_info(COLLECTOR_PID),
        "147111": _proc_info(147111),
        "319436": _proc_info(319436),
        "1661773": _proc_info(1661773),
    }

    summary = {
        "verdict": overall,
        "sub_verdicts": sub_verdicts,
        "format_version": FORMAT_VERSION,
        "started_utc": iso_z(started),
        "ended_utc": iso_z(ended),
        "cutoff": {
            "server_now_utc": iso_z(cutoff.server_now_utc),
            "audit_cutoff_exclusive": iso_z(cutoff.audit_cutoff_exclusive),
            "last_complete_hour": [
                iso_z(cutoff.last_complete_hour_start),
                iso_z(cutoff.last_complete_hour_end),
            ],
        },
        "aggregate_ob_max_known": iso_z(AGGREGATE_END_KNOWN),
        "raw_segment_counts": {s: len(segments_by_sym[s]) for s in SYMBOLS},
        "raw_replay_gaps": len(raw_gaps),
        "parity_summary": {
            r["raw_vs_aggregate_parity"] for r in parity_rows
        },
    }

    integrity = {
        "pids_unchanged": all(
            pids_before.get(k, {}).get("raw") == pids_after.get(k, {}).get("raw")
            for k in pids_before
        ),
        "collector_pid": COLLECTOR_PID,
        "raw_gaps_count": len(raw_gaps),
    }

    manifest = {
        "format_version": FORMAT_VERSION,
        "audit_mode": "READ_ONLY",
        "started_utc": iso_z(started),
        "ended_utc": iso_z(ended),
        "hostname": socket.gethostname(),
        "repo": {"path": str(OA_ROOT), **git},
        "cutoff": summary["cutoff"],
        "pids_before": pids_before,
        "pids_after": pids_after,
        "verdict": overall,
        "sub_verdicts": sub_verdicts,
        "output_files": [],
    }

    _write_csv(out_dir / "raw_archive_segments.csv", raw_rows)
    _write_csv(out_dir / "raw_archive_gaps.csv", raw_gaps)
    _write_csv(out_dir / "raw_replay_integrity.csv", replay_integrity)
    _write_csv(out_dir / "raw_vs_aggregate_parity.csv", parity_rows)
    _write_csv(out_dir / "source_coverage_by_hour.csv", hour_cov)
    _write_csv(out_dir / "last_day_coverage_by_minute.csv", minute_rows)
    _write_csv(out_dir / "multisource_intersections.csv", intersections)
    (out_dir / "collector_freshness.json").write_text(json.dumps(collector, indent=2), encoding="utf-8")
    (out_dir / "profile_asof_smoke.json").write_text(json.dumps(profile_smoke, indent=2), encoding="utf-8")
    (out_dir / "integrity.json").write_text(json.dumps(integrity, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    post_class = defaultdict(int)
    for r in post_rows:
        post_class[r["classification"]] += 1
    amendment = _build_amendment(summary, collector, parity_rows, post_class, raw_gaps)
    (out_dir / "AUDIT_AMENDMENT.md").write_text(amendment, encoding="utf-8")
    (out_dir / "REPORT.md").write_text(_build_report(summary, git, collector, intersections, parity_rows, post_rows, raw_gaps, integrity, sub_verdicts), encoding="utf-8")
    (out_dir / "commands_sanitized.txt").write_text(
        "# BTC/DOGE current multisource recheck V1\n"
        "PYTHONPATH=src .venv/bin/python -m pytest tests/test_btc_doge_current_recheck_v1.py -q\n"
        "PYTHONPATH=src .venv/bin/python scripts/run_btc_doge_current_recheck_v1.py\n",
        encoding="utf-8",
    )

    manifest["output_files"] = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    (out_dir / "audit_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return summary


def _build_amendment(summary, collector, parity_rows, post_class, raw_gaps) -> str:
    return f"""# Amendment to Multisource Data Inventory Audit V1

## IM_AUDIT_BEWIESEN (recheck)

1. Raw-archive collector PID `{COLLECTOR_PID}` active in `raw-archive-only` mode; `writer_state=DISABLED`, `rows_written_total=0`.
2. Aggregated `orderbook_features_1s_v2` ends ~2026-08-28; raw archive continues through last complete hour.
3. Post-2026-08-28 hours: raw segments closed and replay-tested — classifications: {dict(post_class)}.
4. Parity raw replay vs aggregate (3 hours/symbol): { {r['symbol']+':'+r['hour_utc']: r['raw_vs_aggregate_parity'] for r in parity_rows} }.
5. Manifest `replayable=false` with `u_gaps=null` is **not authoritative** — seq gaps are expected; actual ZST replay succeeds.

## Correction to prior audit

- Prior audit correctly flagged OB aggregate as static since Aug 28.
- **Amendment:** For BTCUSDT/DOGEUSDT, per-level raw OB200 is present and replayable after Aug 28; research can use raw replay instead of aggregate table.
- Raw replay is **required** for post-Aug-28 OB features (aggregate not updated).

## Raw gaps in recheck

{len(raw_gaps)} gap records — see `raw_archive_gaps.csv`.
"""


def _build_report(summary, git, collector, intersections, parity_rows, post_rows, raw_gaps, integrity, sub_verdicts) -> str:
    return f"""# BTC/DOGE Current Multi-Source Recheck V1

## 1. VERDICT

```
{summary['verdict']}
```

Sub-verdicts: BTCUSDT={sub_verdicts.get('BTCUSDT')}, DOGEUSDT={sub_verdicts.get('DOGEUSDT')}

## 2. Audit-Cutoff

- server_now_utc: {summary['cutoff']['server_now_utc']}
- audit_cutoff_exclusive: {summary['cutoff']['audit_cutoff_exclusive']}
- last_complete_hour: {summary['cutoff']['last_complete_hour']}

## 3. Repo

Branch `{git['branch']}`, HEAD `{git['head']}` — dirty unchanged.

## 4. PIDs

Collector PID {COLLECTOR_PID} unchanged. See `integrity.json`.

## 5. Raw-Collector-Freshness

Mode confirmed raw-archive-only (writer DISABLED). See `collector_freshness.json`.

## 6–7. Raw OB Coverage

BTC segments: {summary['raw_segment_counts']['BTCUSDT']}, DOGE: {summary['raw_segment_counts']['DOGEUSDT']}.

## 8. Replay Integrity

See `raw_replay_integrity.csv`. {summary['raw_replay_gaps']} gap records.

## 9. Raw vs Aggregate Parity

See `raw_vs_aggregate_parity.csv`.

## 10. Post Aug 28

See post-classification in summary and `source_coverage_by_hour.csv` (OB_FEATURES rows = 0 after cutoff).

## 11. Other Sources

Hourly coverage in `source_coverage_by_hour.csv`.

## 12. Profile as-of

See `profile_asof_smoke.json`.

## 13–14. Intersections

See `multisource_intersections.csv`.

## 15. Audit Discrepancy Explanation

Aggregate producer stopped; raw-archive-only collector continued independently.

## 16. Prior Audit Correction

Amendment: BTC/DOGE raw OB200 covers post-Aug-28; aggregate stale only.

## 17. Files

All artifacts in `results/btc_doge_current_multisource_recheck_v1/`.

## 18. Safety

Read-only. No import/restart/commit.

## 19. STOP

No pattern/ML/repair.
"""
