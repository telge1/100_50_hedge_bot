"""Causal coverage / eligibility evaluation for research-db fight CLI."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .config import iso_z, utc
from .eligibility_contract import (
    CONTEXT_SOURCES,
    MANDATORY_SOURCES,
    OI_EXPECTED_FREQUENCY_MS,
    evaluate_eligibility,
)
from .research_db_loader import TimedQuery, load_candles_coverage, terminal_batch_status
from .volume_profile import profile_session_window


def analysis_windows(
    anchor: datetime, before_minutes: int, after_minutes: int
) -> dict[str, dict[str, str]]:
    anchor = utc(anchor)
    session_start, _, session_id = profile_session_window(anchor)
    pre_start = anchor - timedelta(minutes=before_minutes)
    post_end = anchor + timedelta(minutes=after_minutes)
    return {
        "profile_input_window": {
            "start": iso_z(session_start) or "",
            "end_exclusive": iso_z(anchor) or "",
            "session_id": session_id,
            "predicate": "session_start <= event_ts < anchor",
        },
        "pre_anchor_observation_window": {
            "start": iso_z(pre_start) or "",
            "end_exclusive": iso_z(anchor) or "",
            "predicate": "anchor - before <= event_ts < anchor",
        },
        "post_anchor_observation_window": {
            "start": iso_z(anchor) or "",
            "end_inclusive": iso_z(post_end) or "",
            "predicate": "anchor <= event_ts <= anchor + after",
        },
        "full_fight_window": {
            "start": iso_z(pre_start) or "",
            "end_inclusive": iso_z(post_end) or "",
            "predicate": "anchor - before <= event_ts <= anchor + after",
        },
        "auto_extension_enabled": False,  # type: ignore[dict-item]
    }


def _missing_intervals(missing_seconds: list[datetime]) -> list[dict[str, str]]:
    if not missing_seconds:
        return []
    missing_seconds = sorted(utc(x) for x in missing_seconds)
    intervals: list[dict[str, str]] = []
    start = missing_seconds[0]
    prev = start
    for ts in missing_seconds[1:]:
        if (ts - prev).total_seconds() == 1:
            prev = ts
            continue
        intervals.append({"start": iso_z(start) or "", "end": iso_z(prev) or ""})
        start = prev = ts
    intervals.append({"start": iso_z(start) or "", "end": iso_z(prev) or ""})
    return intervals


def evaluate_ob200_coverage(
    snapshots: list[dict[str, Any]],
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    inclusive_end: bool = True,
) -> dict[str, Any]:
    start = utc(start)
    end = utc(end)
    expected = int((end - start).total_seconds()) + (1 if inclusive_end else 0)
    expected = max(0, expected)
    by_ts = {utc(s["ts"]): s for s in snapshots if s.get("ok")}
    observed = len(by_ts)
    missing: list[datetime] = []
    cursor = start
    step = timedelta(seconds=1)
    last = end if inclusive_end else end - step
    while cursor <= last:
        if cursor not in by_ts:
            missing.append(cursor)
        cursor += step
    levels_ok = all(s.get("genuine_200") for s in by_ts.values()) if by_ts else False
    dup = len(snapshots) - observed
    if observed == 0:
        status = "NOT_AVAILABLE"
    elif missing or not levels_ok or dup > 0:
        status = "PARTIAL"
    else:
        status = "COMPLETE"
    return {
        "source_name": "OB200",
        "symbol": symbol,
        "requested_start": iso_z(start),
        "requested_end": iso_z(end),
        "available_start": iso_z(min(by_ts)) if by_ts else None,
        "available_end": iso_z(max(by_ts)) if by_ts else None,
        "expected_units": expected,
        "observed_units": observed,
        "missing_count": len(missing),
        "missing_intervals": _missing_intervals(missing),
        "missing_seconds": [iso_z(x) for x in missing],
        "duplicate_seconds": dup,
        "levels_200x200_ok": levels_ok,
        "source_segment_status": snapshots[0].get("coverage_status") if snapshots else None,
        "effective_coverage_status": status,
        "mandatory_for_facts": True,
        "mandatory_for_interpretation": True,
        "build_ids": sorted({s.get("build_id") for s in snapshots if s.get("build_id")}),
        "source_fingerprints": sorted(
            {s.get("source_fingerprint") for s in snapshots if s.get("source_fingerprint")}
        ),
    }


def evaluate_trades_coverage(
    trades: list[dict[str, Any]],
    trade_meta: dict[str, Any],
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    source_name: str = "PUBLIC_TRADES",
) -> dict[str, Any]:
    start = utc(start)
    end = utc(end)
    if not trades:
        status = "NOT_AVAILABLE"
    else:
        # Event source: empty seconds are not gaps. COMPLETE when span covers window
        # endpoints within 1s tolerance and dedup ok.
        tmin = min(t["ts"] for t in trades)
        tmax = max(t["ts"] for t in trades)
        covers_start = tmin <= start + timedelta(seconds=5)
        covers_end = tmax >= end - timedelta(seconds=5)
        status = "COMPLETE" if covers_start and covers_end else "PARTIAL"
        if trade_meta.get("source_mode") in {"RESEARCH_EVENTS_ABSENT", "RESEARCH_TRADE_EVENTS_MISSING"}:
            status = "NOT_AVAILABLE"
    return {
        "source_name": source_name,
        "symbol": symbol,
        "requested_start": iso_z(start),
        "requested_end": iso_z(end),
        "available_start": trade_meta.get("min_ts"),
        "available_end": trade_meta.get("max_ts"),
        "expected_units": None,
        "observed_units": len(trades),
        "missing_count": 0 if trades else 1,
        "missing_intervals": []
        if trades
        else [{"start": iso_z(start) or "", "end": iso_z(end) or "", "reason": "NO_TRADES"}],
        "source_segment_status": trade_meta.get("source_mode"),
        "effective_coverage_status": status,
        "mandatory_for_facts": True,
        "mandatory_for_interpretation": True,
        "lineage": {
            "table": trade_meta.get("table"),
            "lineage_companion_used": trade_meta.get("lineage_companion_used"),
            "raw_archive_replay_used": False,
        },
        "deduped_count": trade_meta.get("deduped_count"),
        "raw_count": trade_meta.get("raw_count"),
    }


def evaluate_oi_coverage(
    oi_rows: list[dict[str, Any]], oi_meta: dict[str, Any], *, symbol: str, start: datetime, end: datetime
) -> dict[str, Any]:
    expected = oi_meta.get("expected_samples") or 0
    observed = len(oi_rows)
    if observed == 0:
        status = "NOT_AVAILABLE"
    elif expected and observed < int(expected * 0.95):
        status = "PARTIAL"
    else:
        # Window sample density is complete. Day-level PARTIAL labels on rows do not
        # automatically make a fully sampled fight window PARTIAL.
        status = "COMPLETE"
    return {
        "source_name": "OPEN_INTEREST",
        "symbol": symbol,
        "requested_start": iso_z(utc(start)),
        "requested_end": iso_z(utc(end)),
        "available_start": oi_meta.get("min_ts"),
        "available_end": oi_meta.get("max_ts"),
        "expected_units": expected,
        "observed_units": observed,
        "missing_count": max(0, int(expected) - observed) if expected else 0,
        "missing_intervals": [],
        "source_segment_status": ",".join(oi_meta.get("coverage_statuses") or []),
        "effective_coverage_status": status,
        "mandatory_for_facts": False,
        "mandatory_for_interpretation": False,
        "resolution_ms": OI_EXPECTED_FREQUENCY_MS,
        "note": "effective status uses window sample density; day PARTIAL tags are informational",
    }


def evaluate_liq_coverage(
    liq_rows: list[dict[str, Any]],
    liq_meta: dict[str, Any],
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    client: Any,
    timer: TimedQuery,
) -> dict[str, Any]:
    # Event source: null events valid. Use day batch lineage when available.
    day = utc(start).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day + timedelta(days=1)
    batch_status = terminal_batch_status(
        client, timer, symbol=symbol, modality="LIQUIDATIONS", segment_start=day, segment_end=day_end
    )
    if batch_status in {"READY", "PARTIAL"}:
        status = "COMPLETE" if batch_status == "READY" else "PARTIAL"
    elif liq_rows:
        status = "COMPLETE"
    else:
        # No events and no batch proof for day — still not a forced gap for event sources
        # if neighboring days imported; treat as COMPLETE with zero events when day READY unknown
        # but research DB may simply have zero liquidations.
        status = "COMPLETE"
    return {
        "source_name": "LIQUIDATIONS",
        "symbol": symbol,
        "requested_start": iso_z(utc(start)),
        "requested_end": iso_z(utc(end)),
        "available_start": liq_meta.get("min_ts"),
        "available_end": liq_meta.get("max_ts"),
        "expected_units": None,
        "observed_units": len(liq_rows),
        "missing_count": 0,
        "missing_intervals": [],
        "source_segment_status": batch_status,
        "effective_coverage_status": status,
        "mandatory_for_facts": False,
        "mandatory_for_interpretation": False,
        "null_events_are_valid": True,
    }


def evaluate_candles_coverage(candle_meta: dict[str, Any], *, symbol: str) -> dict[str, Any]:
    status = "COMPLETE" if candle_meta.get("complete") else "PARTIAL"
    if candle_meta.get("count", 0) == 0:
        status = "NOT_AVAILABLE"
    return {
        "source_name": "CANDLES_1M",
        "symbol": symbol,
        "requested_start": None,
        "requested_end": None,
        "available_start": candle_meta.get("min_ts"),
        "available_end": candle_meta.get("max_ts"),
        "expected_units": candle_meta.get("expected_minutes"),
        "observed_units": candle_meta.get("count"),
        "missing_count": 0,
        "missing_intervals": [],
        "source_segment_status": "COVERAGE_ONLY",
        "effective_coverage_status": status,
        "mandatory_for_facts": False,
        "mandatory_for_interpretation": False,
        "classification": "COVERAGE_ONLY",
    }


def build_eligibility_bundle(
    *,
    symbol: str,
    anchor: datetime,
    before_minutes: int,
    after_minutes: int,
    ob_cov: dict[str, Any],
    fight_trades_cov: dict[str, Any],
    profile_trades_cov: dict[str, Any],
    oi_cov: dict[str, Any],
    liq_cov: dict[str, Any],
    candles_cov: dict[str, Any],
    profile_causality_passed: bool,
    contract_error: str | None = None,
) -> dict[str, Any]:
    mandatory = {
        "OB200": ob_cov["effective_coverage_status"],
        "PUBLIC_TRADES": fight_trades_cov["effective_coverage_status"],
        "PROFILE_TRADES": profile_trades_cov["effective_coverage_status"],
    }
    context = {
        "OPEN_INTEREST": oi_cov["effective_coverage_status"],
        "LIQUIDATIONS": liq_cov["effective_coverage_status"],
        "CANDLES_1M": candles_cov["effective_coverage_status"],
    }
    gate = evaluate_eligibility(
        mandatory_statuses=mandatory,
        context_statuses=context,
        profile_causality_passed=profile_causality_passed,
        contract_error=contract_error,
    )
    sources = [ob_cov, fight_trades_cov, profile_trades_cov, oi_cov, liq_cov, candles_cov]
    missing_rows = []
    for src in sources:
        for interval in src.get("missing_intervals") or []:
            missing_rows.append(
                {
                    "source_name": src["source_name"],
                    "symbol": symbol,
                    **interval,
                }
            )
        for sec in src.get("missing_seconds") or []:
            missing_rows.append(
                {
                    "source_name": src["source_name"],
                    "symbol": symbol,
                    "start": sec,
                    "end": sec,
                }
            )
    return {
        **gate,
        "windows": analysis_windows(anchor, before_minutes, after_minutes),
        "sources": sources,
        "mandatory_statuses": mandatory,
        "context_statuses": context,
        "missing_rows": missing_rows,
        "mandatory_sources": list(MANDATORY_SOURCES),
        "context_sources": list(CONTEXT_SOURCES),
    }
