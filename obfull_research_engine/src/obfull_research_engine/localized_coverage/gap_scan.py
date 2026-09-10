"""Scan Full-OB segments for sequence gaps and recovery checkpoints/snapshots."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from orderbook_analyse.orderbook_v2_live.full_ob_continuous_raw_archive.replay import iter_records
from orderbook_analyse.research.general_market_behavior_v1.coverage import (
    evaluate_segment,
    list_hour_segments,
)

from ..timeparse import format_utc_z
from . import RECOVERY_CHECKPOINT_REASONS


def _ns_to_dt(ns: Any) -> datetime | None:
    if ns is None:
        return None
    try:
        return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _parse_z(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_book_levels(bids: list, asks: list) -> dict[str, Any]:
    """Return ok + reason; reject crossed / negative / empty sides when both present."""
    try:
        for side_name, side in (("bids", bids or []), ("asks", asks or [])):
            for row in side:
                if not row or len(row) < 2:
                    return {"ok": False, "reason": f"malformed_{side_name}_level"}
                price, size = Decimal(str(row[0])), Decimal(str(row[1]))
                if size < 0:
                    return {"ok": False, "reason": f"negative_{side_name}_size"}
                if price <= 0:
                    return {"ok": False, "reason": f"non_positive_{side_name}_price"}
        if bids and asks:
            bb = Decimal(str(bids[0][0]))
            ba = Decimal(str(asks[0][0]))
            # bids should be sorted desc, asks asc — use max bid / min ask defensively
            max_bid = max(Decimal(str(r[0])) for r in bids)
            min_ask = min(Decimal(str(r[0])) for r in asks)
            if max_bid >= min_ask:
                return {"ok": False, "reason": "crossed_book", "best_bid": str(max_bid), "best_ask": str(min_ask)}
        if not bids or not asks:
            return {"ok": False, "reason": "empty_side"}
        return {"ok": True, "reason": "validated"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"validation_error:{exc}"}


def _hour_floor(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)


def iter_hours(start: datetime, end: datetime) -> list[datetime]:
    from datetime import timedelta

    h = _hour_floor(start)
    out: list[datetime] = []
    while h < end:
        out.append(h)
        h += timedelta(hours=1)
    return out


def pick_segments(
    symbol: str,
    start: datetime,
    end: datetime,
    archive_root: Path,
) -> list[dict[str, Any]]:
    """Best segment per overlapping hour (by message_count)."""
    by_hour: dict[datetime, list[tuple[Path, dict]]] = {}
    for seg in list_hour_segments(archive_root, symbol.upper()):
        man_path = Path(str(seg) + ".manifest.json")
        if not man_path.exists():
            continue
        man = json.loads(man_path.read_text(encoding="utf-8"))
        hh = _parse_z(str(man["utc_hour"]))
        if hh is None:
            continue
        by_hour.setdefault(hh, []).append((seg, man))

    picked: list[dict[str, Any]] = []
    for hh in iter_hours(start, end):
        cands = by_hour.get(hh) or []
        if not cands:
            picked.append({"hour": hh, "segment_path": None, "manifest": None, "missing": True})
            continue
        seg, man = max(cands, key=lambda cm: int(cm[1].get("message_count") or 0))
        picked.append(
            {
                "hour": hh,
                "segment_path": str(seg),
                "manifest": man,
                "missing": False,
            }
        )
    return picked


def scan_segment_gaps(segment_path: str | Path, *, symbol: str) -> list[dict[str, Any]]:
    """Return gap events with recovery info inside one segment file."""
    seg = Path(segment_path)
    gaps: list[dict[str, Any]] = []
    prev_u: int | None = None
    pending: dict[str, Any] | None = None

    def close_pending(rec_evt: datetime | None, reason: str, u: int | None, validation: dict[str, Any]):
        nonlocal pending
        if pending is None:
            return
        if not validation.get("ok"):
            # invalid recovery does not end taint
            pending.setdefault("rejected_recoveries", []).append(
                {
                    "ts": format_utc_z(rec_evt) if rec_evt else None,
                    "reason": reason,
                    "validation": validation,
                }
            )
            return
        pending["recovery_checkpoint_ts"] = format_utc_z(rec_evt) if rec_evt else None
        pending["recovery_reason"] = reason
        pending["recovery_u"] = u
        pending["book_validation"] = validation
        pending["status"] = "RECOVERED"
        if rec_evt and pending.get("_gap_dt"):
            pending["tainted_duration_ms"] = int((rec_evt - pending["_gap_dt"]).total_seconds() * 1000)
        else:
            pending["tainted_duration_ms"] = None
        out = {k: v for k, v in pending.items() if not str(k).startswith("_")}
        gaps.append(out)
        pending = None

    for rec in iter_records(seg):
        kind = str(rec.get("message_type") or "")
        payload = rec.get("original_payload") if isinstance(rec.get("original_payload"), dict) else {}
        evt = _ns_to_dt(rec.get("event_time_ns") or rec.get("receive_time_ns"))

        if kind == "checkpoint":
            u = payload.get("u")
            reason = str(payload.get("checkpoint_reason") or "")
            bids = payload.get("bids") or []
            asks = payload.get("asks") or []
            # Prefer checkpoint_time if present
            cts = _parse_z(payload.get("checkpoint_time")) or evt
            if reason in RECOVERY_CHECKPOINT_REASONS and pending is not None:
                close_pending(cts, reason, int(u) if u is not None else None, validate_book_levels(bids, asks))
            # periodic_5m resets u cursor for subsequent continuity counting (writer semantics)
            # but does NOT heal an open pending taint
            prev_u = int(u) if u is not None else prev_u
            continue

        if kind == "snapshot":
            # Exchange snapshot: treat as recovery candidate if book validates
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            bids = data.get("b") or data.get("bids") or payload.get("bids") or []
            asks = data.get("a") or data.get("asks") or payload.get("asks") or []
            u = data.get("u") or payload.get("u")
            if pending is not None:
                close_pending(
                    evt,
                    "exchange_snapshot" if "u" in (data or {}) or bids else "snapshot",
                    int(u) if u is not None else None,
                    validate_book_levels(bids, asks),
                )
            if u is not None:
                prev_u = int(u)
            continue

        if kind != "delta":
            continue

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        u = data.get("u")
        if prev_u is not None and u is not None and int(u) not in {prev_u, prev_u + 1}:
            missing = int(u) - int(prev_u) - 1 if int(u) > int(prev_u) else None
            if pending is None:
                pending = {
                    "symbol": symbol.upper(),
                    "gap_ts": format_utc_z(evt) if evt else None,
                    "_gap_dt": evt,
                    "prev_u": int(prev_u),
                    "next_u": int(u),
                    "missing_u_ids": missing,
                    "n_gap_events_merged": 1,
                    "segment_path": str(seg),
                    "status": "OPEN_TAINTED",
                    "recovery_checkpoint_ts": None,
                    "recovery_reason": None,
                    "tainted_duration_ms": None,
                    "book_validation": None,
                }
            else:
                pending["next_u"] = int(u)
                pending["n_gap_events_merged"] = int(pending.get("n_gap_events_merged") or 1) + 1
                if missing is not None:
                    pending["missing_u_ids"] = (pending.get("missing_u_ids") or 0) + int(missing)
        if u is not None:
            prev_u = int(u)

    if pending is not None:
        pending["status"] = "UNRECOVERED_IN_SEGMENT"
        out = {k: v for k, v in pending.items() if not str(k).startswith("_")}
        gaps.append(out)
    return gaps


def merge_tainted_intervals(gaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge overlapping/adjacent tainted [gap_ts, recovery) intervals."""
    intervals: list[dict[str, Any]] = []
    for g in gaps:
        start = _parse_z(g.get("gap_ts"))
        end = _parse_z(g.get("recovery_checkpoint_ts"))
        if start is None:
            continue
        if end is None:
            # open-ended: keep as open until known
            end = None
        intervals.append(
            {
                "start": start,
                "end": end,
                "source_gaps": [g],
                "reason": "TAINTED_SEQUENCE_GAP",
            }
        )
    intervals.sort(key=lambda x: x["start"])
    if not intervals:
        return []

    merged: list[dict[str, Any]] = [intervals[0]]
    for cur in intervals[1:]:
        prev = merged[-1]
        prev_end = prev["end"]
        # If previous unrecovered, swallow everything after
        if prev_end is None:
            prev["source_gaps"].extend(cur["source_gaps"])
            continue
        if cur["end"] is None:
            if cur["start"] <= prev_end:
                prev["end"] = None
                prev["source_gaps"].extend(cur["source_gaps"])
            else:
                merged.append(cur)
            continue
        if cur["start"] <= prev_end:
            if cur["end"] > prev_end:
                prev["end"] = cur["end"]
            prev["source_gaps"].extend(cur["source_gaps"])
        else:
            merged.append(cur)

    out: list[dict[str, Any]] = []
    for m in merged:
        start, end = m["start"], m["end"]
        dur_ms = None if end is None else int((end - start).total_seconds() * 1000)
        out.append(
            {
                "start": format_utc_z(start),
                "end": format_utc_z(end) if end else None,
                "tainted_duration_ms": dur_ms,
                "tainted_duration_seconds": None if dur_ms is None else dur_ms / 1000.0,
                "n_source_gaps": len(m["source_gaps"]),
                "reason": m["reason"],
                "source_gaps": m["source_gaps"],
            }
        )
    return out


def scan_window_gaps(
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    archive_root: Path,
) -> dict[str, Any]:
    picked = pick_segments(symbol, start, end, archive_root)
    all_gaps: list[dict[str, Any]] = []
    hour_meta: list[dict[str, Any]] = []
    segment_paths: list[str] = []
    for item in picked:
        hh = item["hour"]
        if item.get("missing") or not item.get("segment_path"):
            hour_meta.append(
                {
                    "hour": format_utc_z(hh),
                    "missing_segment": True,
                    "u_gaps": None,
                }
            )
            continue
        ev = evaluate_segment(
            Path(item["segment_path"]),
            symbol=symbol,
            run_replay_if_needed=False,
            query_modalities=False,
        )
        hour_meta.append(
            {
                "hour": format_utc_z(hh),
                "segment_path": item["segment_path"],
                "u_gaps": int(ev.in_segment_u_gap_count or 0),
                "completion_status": ev.completion_status,
                "wall_clock": bool(ev.full_wall_clock_coverage),
            }
        )
        segment_paths.append(item["segment_path"])
        all_gaps.extend(scan_segment_gaps(item["segment_path"], symbol=symbol))

    # Heal unrecovered gaps using later segments' recovery anchors
    all_gaps = _heal_unrecovered_across_segments(all_gaps, segment_paths, symbol=symbol)

    clipped = []
    for g in all_gaps:
        gs = _parse_z(g.get("gap_ts"))
        if gs is None:
            continue
        ge = _parse_z(g.get("recovery_checkpoint_ts")) or end
        if ge <= start or gs >= end:
            continue
        clipped.append(g)

    tainted = merge_tainted_intervals(clipped)
    clipped_tainted = []
    for t in tainted:
        ts = _parse_z(t["start"])
        te = _parse_z(t["end"]) if t.get("end") else end
        assert ts is not None
        a = max(ts, start)
        b = min(te, end) if te else end
        if a >= b:
            continue
        dur_ms = int((b - a).total_seconds() * 1000)
        clipped_tainted.append(
            {
                **t,
                "start": format_utc_z(a),
                "end": format_utc_z(b),
                "tainted_duration_ms": dur_ms,
                "tainted_duration_seconds": dur_ms / 1000.0,
                "clipped_to_window": True,
            }
        )

    return {
        "symbol": symbol.upper(),
        "start": format_utc_z(start),
        "end": format_utc_z(end),
        "hour_meta": hour_meta,
        "sequence_gaps": clipped,
        "n_sequence_gaps": len(clipped),
        "tainted_intervals": clipped_tainted,
        "n_tainted_intervals": len(clipped_tainted),
    }


def _heal_unrecovered_across_segments(
    gaps: list[dict[str, Any]],
    segment_paths: list[str],
    *,
    symbol: str,
) -> list[dict[str, Any]]:
    """If a gap has no recovery in its segment, search later segments for snapshot/checkpoint."""
    recoveries = _list_recovery_anchors(segment_paths)
    out: list[dict[str, Any]] = []
    for g in gaps:
        if g.get("recovery_checkpoint_ts") or g.get("status") == "RECOVERED":
            out.append(g)
            continue
        gap_dt = _parse_z(g.get("gap_ts"))
        if gap_dt is None:
            out.append(g)
            continue
        healed = None
        for rec in recoveries:
            if rec["dt"] > gap_dt and rec["validation"].get("ok"):
                healed = rec
                break
        if healed is None:
            out.append(g)
            continue
        g2 = dict(g)
        g2["recovery_checkpoint_ts"] = format_utc_z(healed["dt"])
        g2["recovery_reason"] = healed["reason"]
        g2["recovery_u"] = healed.get("u")
        g2["book_validation"] = healed["validation"]
        g2["status"] = "RECOVERED_CROSS_SEGMENT"
        g2["tainted_duration_ms"] = int((healed["dt"] - gap_dt).total_seconds() * 1000)
        out.append(g2)
    return out


def _list_recovery_anchors(segment_paths: list[str]) -> list[dict[str, Any]]:
    """Collect validated recovery points across segments (time-ordered)."""
    anchors: list[dict[str, Any]] = []
    for sp in segment_paths:
        for rec in iter_records(Path(sp)):
            kind = str(rec.get("message_type") or "")
            payload = rec.get("original_payload") if isinstance(rec.get("original_payload"), dict) else {}
            evt = _ns_to_dt(rec.get("event_time_ns") or rec.get("receive_time_ns"))
            if kind == "checkpoint":
                reason = str(payload.get("checkpoint_reason") or "")
                if reason not in RECOVERY_CHECKPOINT_REASONS:
                    continue
                bids = payload.get("bids") or []
                asks = payload.get("asks") or []
                cts = _parse_z(payload.get("checkpoint_time")) or evt
                if cts is None:
                    continue
                anchors.append(
                    {
                        "dt": cts,
                        "reason": reason,
                        "u": payload.get("u"),
                        "validation": validate_book_levels(bids, asks),
                    }
                )
            elif kind == "snapshot":
                data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
                bids = data.get("b") or data.get("bids") or payload.get("bids") or []
                asks = data.get("a") or data.get("asks") or payload.get("asks") or []
                if evt is None:
                    continue
                anchors.append(
                    {
                        "dt": evt,
                        "reason": "exchange_snapshot",
                        "u": data.get("u") or payload.get("u"),
                        "validation": validate_book_levels(bids, asks),
                    }
                )
    anchors.sort(key=lambda a: a["dt"])
    return anchors
