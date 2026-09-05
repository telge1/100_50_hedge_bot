"""Multi-day coverage inventory for sample expansion — availability only, no outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.timeutil import iso_z, parse_utc
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance.edge_catalog import DEFAULT_RAW_ROOT
from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments

# Full forward horizon for eligibility (60m).
FORWARD_NEED_S = 3600
MIN_SEGMENT_S = 3500


def build_multi_day_coverage(
    *,
    raw_root: Path = DEFAULT_RAW_ROOT,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    range_start: str = "2026-08-24T00:00:00Z",
    range_end: str = "2026-08-30T00:00:00Z",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (hour_rows, summary). Status: ELIGIBLE | PARTIAL | BLOCKED."""
    start = parse_utc(range_start)
    end = parse_utc(range_end)
    segs = list_closed_segments(raw_root, symbols=symbols, start=start, end=end)

    by_sym_hour: dict[tuple[str, str], dict[str, Any]] = {}
    for s in segs:
        if s.is_boundary_stub or s.duration_sec < MIN_SEGMENT_S:
            continue
        hour_key = s.start_utc.strftime("%Y-%m-%dT%H:00:00Z")
        by_sym_hour[(s.symbol, hour_key)] = {
            "path": str(s.path),
            "start": iso_z(s.start_utc),
            "end": iso_z(s.end_utc),
            "duration_s": s.duration_sec,
        }

    hours = sorted({h for (_, h) in by_sym_hour})
    rows: list[dict[str, Any]] = []
    for h in hours:
        btc = by_sym_hour.get(("BTCUSDT", h))
        doge = by_sym_hour.get(("DOGEUSDT", h))
        ht = parse_utc(h)
        nxt = (ht + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")
        # Need trades through hour_end + 60m ⇒ next hour must exist for both symbols
        # (closed OB hour is proxy that research archive covers that wall-clock hour;
        #  trades validated at process time).
        btc_next = by_sym_hour.get(("BTCUSDT", nxt))
        doge_next = by_sym_hour.get(("DOGEUSDT", nxt))
        both = bool(btc and doge)
        forward_ok = bool(btc_next and doge_next)
        # Warmup prior hour helpful but not hard-required if first hour of archive
        prev = (ht - timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z")
        warmup_ok = bool(
            by_sym_hour.get(("BTCUSDT", prev)) and by_sym_hour.get(("DOGEUSDT", prev))
        ) or h == hours[0]

        if both and forward_ok and (btc["duration_s"] >= MIN_SEGMENT_S) and (
            doge["duration_s"] >= MIN_SEGMENT_S
        ):
            status = "ELIGIBLE"
            reason = "paired_closed_ob200_plus_next_hour_for_60m_forward"
        elif both and not forward_ok:
            status = "PARTIAL"
            reason = "paired_ob200_but_missing_next_hour_for_60m_forward"
        elif btc or doge:
            status = "PARTIAL"
            reason = "symbol_pair_incomplete"
        else:
            status = "BLOCKED"
            reason = "no_ob200"

        rows.append(
            {
                "hour_start": h,
                "hour_end": iso_z(ht + timedelta(hours=1)),
                "btc_ob200": bool(btc),
                "doge_ob200": bool(doge),
                "btc_duration_s": (btc or {}).get("duration_s"),
                "doge_duration_s": (doge or {}).get("duration_s"),
                "btc_path": (btc or {}).get("path"),
                "doge_path": (doge or {}).get("path"),
                "next_hour_paired": forward_ok,
                "warmup_prior_hour": warmup_ok,
                "forward_need_s": FORWARD_NEED_S,
                "status": status,
                "reason": reason,
                "tmp_excluded": True,
                "selection_basis": "data_availability_only",
            }
        )

    eligible = [r for r in rows if r["status"] == "ELIGIBLE"]
    summary = {
        "range_start": range_start,
        "range_end": range_end,
        "n_hours_seen": len(rows),
        "n_eligible": len(eligible),
        "n_partial": sum(1 for r in rows if r["status"] == "PARTIAL"),
        "n_blocked": sum(1 for r in rows if r["status"] == "BLOCKED"),
        "first_eligible": eligible[0]["hour_start"] if eligible else None,
        "last_eligible": eligible[-1]["hour_start"] if eligible else None,
        "forward_need_s": FORWARD_NEED_S,
        "outcome_used_for_window_selection": False,
    }
    return rows, summary


def chronological_eligible_hours(rows: list[dict[str, Any]]) -> list[str]:
    return [r["hour_start"] for r in rows if r["status"] == "ELIGIBLE"]
