"""ANALYSIS_ELIGIBLE_ANCHORED_WARMUP_V1 — AVR coverage anchored at analysis_eligible_start.

Orchestration / bounds only. Does not change Dashboard AVR formulas or baselines.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..avr.provenance import ensure_avr_import_path
from ..avr.warmup import check_avr_warmup_coverage
from ..avr_multiscale import LOOKBACK_S as MS_LOOKBACK_S
from ..avr_multiscale.coverage import check_multiscale_coverage
from orderbook_analyse.research.general_market_behavior_v1.coverage import load_clickhouse_env

from ..interval_coverage import _q
from ..timeparse import format_utc_z

WARMUP_ANCHOR_POLICY = "ANALYSIS_ELIGIBLE_ANCHORED_WARMUP_V1"

STATUS_SOURCE_MISSING = "PUBLIC_TRADES_SOURCE_MISSING"
STATUS_WARMUP_SOURCE_INCOMPLETE = "AVR_WARMUP_SOURCE_INCOMPLETE"
STATUS_BASELINE_INSUFFICIENT = "AVR_BASELINE_INSUFFICIENT_BUT_SOURCE_COMPLETE"
STATUS_STATE_INSUFFICIENT = "AVR_STATE_INSUFFICIENT_DATA"
STATUS_READY = "AVR_READY"

SPAN_NOT_ANALYZABLE = "SPAN_NOT_ANALYZABLE_AVR_WARMUP"
MIN_ANALYZABLE_REMAINING_S = 60
READY_SCAN_STEP_S = 60


def _p(s: str | datetime | None) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.astimezone(timezone.utc) if s.tzinfo else s.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def avr_contract_constants() -> dict[str, int]:
    """Existing Dashboard AVR contract — do not redefine."""
    ensure_avr_import_path()
    from footprint_candles.response_contracts import (  # noqa: WPS433
        BASELINE_LOOKBACK_S,
        MIN_BASELINE_VALID_SECONDS,
        PRIMARY_WINDOW_S,
    )

    return {
        "baseline_lookback_s": int(BASELINE_LOOKBACK_S),
        "min_baseline_valid_seconds": int(MIN_BASELINE_VALID_SECONDS),
        "primary_window_s": int(PRIMARY_WINDOW_S),
        "multiscale_lookback_s": int(MS_LOOKBACK_S),
    }


def history_bounds(eligible_start: datetime) -> dict[str, Any]:
    """AVR/MS history required so AVR is ready *at* eligible_start.

    preroll = [eligible − 1800s, eligible)           — existing BASELINE_LOOKBACK_S
    load    = [preroll_start − 15s, feature_end)     — existing PRIMARY_WINDOW_S edge
    MS      = [eligible − 300s, eligible)            — existing LOOKBACK_S
    """
    eligible_start = eligible_start.astimezone(timezone.utc)
    c = avr_contract_constants()
    preroll_start = eligible_start - timedelta(seconds=c["baseline_lookback_s"])
    load_start = preroll_start - timedelta(seconds=c["primary_window_s"])
    ms_start = eligible_start - timedelta(seconds=c["multiscale_lookback_s"])
    return {
        "avr_required_history_end": format_utc_z(eligible_start),
        "avr_required_history_start": format_utc_z(preroll_start),
        "avr_load_start": format_utc_z(load_start),
        "core_edge_seconds": c["primary_window_s"],
        "baseline_lookback_s": c["baseline_lookback_s"],
        "min_baseline_valid_seconds": c["min_baseline_valid_seconds"],
        "ms_required_history_start": format_utc_z(ms_start),
        "ms_required_history_end": format_utc_z(eligible_start),
        "ms_lookback_s": c["multiscale_lookback_s"],
        "primary_window_s": c["primary_window_s"],
    }


def classify_source_vs_baseline(
    warmup: dict[str, Any],
    *,
    preroll_inside_span: bool,
) -> str:
    """Keep existing AVR quality reasons; add source-vs-baseline labels around them."""
    if warmup.get("ok"):
        return STATUS_READY
    n_tr = int(warmup.get("n_trades_preroll") or 0)
    n_sec = int(warmup.get("n_preroll_distinct_seconds") or 0)
    min_s = int(warmup.get("min_baseline_valid_seconds") or 900)
    reason = warmup.get("reason")
    if not preroll_inside_span:
        return STATUS_WARMUP_SOURCE_INCOMPLETE
    if n_tr <= 0 or reason == "AVR_WARMUP_EMPTY":
        return STATUS_SOURCE_MISSING
    if n_sec < min_s:
        return STATUS_BASELINE_INSUFFICIENT
    if reason == "AVR_TIP_BEFORE_FEATURE_END":
        return STATUS_WARMUP_SOURCE_INCOMPLETE
    return STATUS_STATE_INSUFFICIENT


def warmup_blocks_analysis(warmup: dict[str, Any] | None, *, gate: str) -> bool:
    """STRICT: existing warmup.ok. SOURCE_COMPLETE: only empty/missing source or tip."""
    if not warmup:
        return gate == "STRICT"
    if warmup.get("ok"):
        return False
    if gate != "SOURCE_COMPLETE":
        return True
    n = int(warmup.get("n_trades_preroll") or 0)
    reason = warmup.get("reason")
    if reason == "AVR_TIP_BEFORE_FEATURE_END":
        return True
    if n <= 0 or reason == "AVR_WARMUP_EMPTY":
        return True
    return False


def query_distinct_trade_seconds(symbol: str, start: datetime, end: datetime) -> list[int]:
    load_clickhouse_env()
    symbol = symbol.upper()
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    hs = start.strftime("%Y-%m-%d %H:%M:%S")
    he = end.strftime("%Y-%m-%d %H:%M:%S")
    raw = _q(
        "SELECT toUnixTimestamp(toStartOfSecond(trade_ts)) FROM orderbook_analysis.public_trades_canonical "
        f"WHERE symbol='{symbol}' AND trade_ts>='{hs}' AND trade_ts<'{he}' "
        "GROUP BY toStartOfSecond(trade_ts) ORDER BY toStartOfSecond(trade_ts) FORMAT TSV"
    )
    out: list[int] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if line:
            out.append(int(float(line)))
    return out


def first_ready_from_seconds(
    *,
    seconds: list[int],
    base_eligible: datetime,
    span_start: datetime,
    span_end: datetime,
    lookback_s: int,
    min_valid_s: int,
    step_s: int = READY_SCAN_STEP_S,
) -> datetime | None:
    """First t >= base_eligible where [t−lookback, t) has enough distinct seconds inside the span."""
    if not seconds:
        return None
    sec_set = set(seconds)
    elig_u = int(base_eligible.timestamp())
    end_u = int(span_end.timestamp())
    span_u = int(span_start.timestamp())
    t = elig_u
    while t < end_u:
        lo = t - int(lookback_s)
        if lo < span_u:
            t += int(step_s)
            continue
        n = sum(1 for s in sec_set if lo <= s < t)
        if n >= int(min_valid_s):
            return datetime.fromtimestamp(t, tz=timezone.utc)
        t += int(step_s)
    return None


def resolve_one_span(
    *,
    symbol: str,
    span: dict[str, Any],
    requested_start: datetime,
    with_avr: bool,
    with_avr_multiscale: bool,
    check_avr_fn: Callable[..., dict[str, Any]] = check_avr_warmup_coverage,
    check_ms_fn: Callable[..., dict[str, Any]] = check_multiscale_coverage,
    seconds_fn: Callable[[str, datetime, datetime], list[int]] | None = None,
) -> dict[str, Any]:
    span_start = _p(span["start"])
    span_end = _p(span["end"])
    assert span_start and span_end
    base = _p(span.get("analysis_eligible_start") or span["start"])
    assert base
    if base < span_start:
        base = span_start
    if base >= span_end:
        return {
            **span,
            "base_analysis_eligible_start": format_utc_z(base),
            "avr_required_history_start": None,
            "avr_ready_from": None,
            "final_analysis_eligible_start": format_utc_z(base),
            "avr_warmup_status": "INCOMPLETE",
            "avr_multiscale_status": "INCOMPLETE",
            "span_analyzable": False,
            "span_exclude_reason": SPAN_NOT_ANALYZABLE,
            "source_vs_baseline": STATUS_WARMUP_SOURCE_INCOMPLETE,
            "warmup_anchor": WARMUP_ANCHOR_POLICY,
            "warmup_anchor_note": "eligible_at_or_after_span_end",
        }

    bounds = history_bounds(base)
    preroll_start = _p(bounds["avr_required_history_start"])
    assert preroll_start
    preroll_inside = preroll_start >= span_start - timedelta(seconds=1)

    avr_w = None
    ms_c = None
    ready = base
    avr_status = "COMPLETE"
    ms_status = "COMPLETE" if with_avr_multiscale else "NOT_REQUESTED"
    source_label = STATUS_READY
    exclude = None

    if with_avr:
        avr_w = check_avr_fn(symbol=symbol, feature_start=base, feature_end=span_end)
        source_label = classify_source_vs_baseline(avr_w, preroll_inside_span=preroll_inside)
        if not avr_w.get("ok"):
            source_label = classify_source_vs_baseline(avr_w, preroll_inside_span=preroll_inside)
            need_scan = warmup_blocks_analysis(avr_w, gate="SOURCE_COMPLETE")
            found = None
            if need_scan or seconds_fn is not None:
                c = avr_contract_constants()
                try:
                    secs = (seconds_fn or query_distinct_trade_seconds)(symbol, span_start, span_end)
                except Exception:  # noqa: BLE001
                    secs = []
                found = first_ready_from_seconds(
                    seconds=secs,
                    base_eligible=base,
                    span_start=span_start,
                    span_end=span_end,
                    lookback_s=c["baseline_lookback_s"],
                    min_valid_s=c["min_baseline_valid_seconds"],
                )
            if found is not None:
                ready = found
                avr_status = "DELAYED" if found > base else "COMPLETE"
                avr_w = check_avr_fn(symbol=symbol, feature_start=found, feature_end=span_end)
                source_label = classify_source_vs_baseline(avr_w, preroll_inside_span=True)
                bounds = history_bounds(found)
            elif warmup_blocks_analysis(avr_w, gate="SOURCE_COMPLETE"):
                avr_status = "INCOMPLETE"
                exclude = SPAN_NOT_ANALYZABLE
            else:
                # Source present, baseline quiet — existing AVR quality, not a control fail.
                ready = base
                avr_status = "COMPLETE"
                source_label = classify_source_vs_baseline(avr_w, preroll_inside_span=preroll_inside)

    if with_avr_multiscale and exclude is None:
        ms_c = check_ms_fn(symbol=symbol, feature_start=ready, feature_end=span_end)
        if not ms_c.get("ok"):
            reason = str(ms_c.get("reason") or "")
            inherited_only = "AVR_WARMUP" in reason and not warmup_blocks_analysis(avr_w, gate="SOURCE_COMPLETE")
            lookback_empty = "PUBLIC_TRADES_LOOKBACK_EMPTY" in reason or "CLOSED_5M_BEFORE_START_EMPTY" in reason
            if inherited_only and not lookback_empty:
                ms_status = "DELAYED" if ready > base else "COMPLETE"
                ms_c = {
                    **ms_c,
                    "ok": True,
                    "reason": None,
                    "note": "AVR baseline insufficient-but-source-complete; MS lookback present",
                }
            elif lookback_empty:
                ms_status = "INCOMPLETE"
                exclude = SPAN_NOT_ANALYZABLE
                source_label = STATUS_SOURCE_MISSING
            else:
                ms_status = "DELAYED" if ready > base else "COMPLETE"
        else:
            ms_status = "DELAYED" if ready > base else "COMPLETE"

    remaining = (span_end - ready).total_seconds()
    if exclude is None and remaining < MIN_ANALYZABLE_REMAINING_S:
        exclude = "TOO_SHORT_AFTER_AVR_READY"
        avr_status = "INCOMPLETE"

    out = {
        **span,
        "base_analysis_eligible_start": format_utc_z(base),
        "avr_required_history_start": bounds.get("avr_required_history_start"),
        "avr_required_history_end": bounds.get("avr_required_history_end"),
        "avr_load_start": bounds.get("avr_load_start"),
        "ms_required_history_start": bounds.get("ms_required_history_start"),
        "avr_ready_from": None if exclude else format_utc_z(ready),
        "final_analysis_eligible_start": format_utc_z(ready if exclude is None else base),
        "analysis_eligible_start": format_utc_z(ready if exclude is None else base),
        "avr_warmup_status": avr_status,
        "avr_multiscale_status": ms_status,
        "span_analyzable": exclude is None,
        "span_exclude_reason": exclude,
        "source_vs_baseline": source_label,
        "warmup_anchor": WARMUP_ANCHOR_POLICY,
        "requested_start_not_used_as_avr_anchor": format_utc_z(requested_start),
        "avr_warmup": avr_w,
        "avr_multiscale_coverage": ms_c,
        "core_edge_seconds": bounds.get("core_edge_seconds"),
        "usable_span_start": span["start"],
        "usable_span_end": span["end"],
    }
    return out


def usable_spans_hash(spans: list[dict[str, Any]]) -> str:
    payload = [
        {
            "span_id": s.get("span_id"),
            "start": s.get("start"),
            "end": s.get("end"),
            "base": s.get("base_analysis_eligible_start") or s.get("analysis_eligible_start"),
            "final": s.get("final_analysis_eligible_start"),
        }
        for s in spans
    ]
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def resolve_localized_avr_warmup(
    *,
    symbol: str,
    requested_start: datetime,
    requested_end: datetime,
    usable_spans: list[dict[str, Any]],
    with_avr: bool,
    with_avr_multiscale: bool = False,
    check_avr_fn: Callable[..., dict[str, Any]] = check_avr_warmup_coverage,
    check_ms_fn: Callable[..., dict[str, Any]] = check_multiscale_coverage,
    seconds_fn: Callable[[str, datetime, datetime], list[int]] | None = None,
) -> dict[str, Any]:
    resolved: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    analyzable: list[dict[str, Any]] = []
    for sp in usable_spans:
        row = resolve_one_span(
            symbol=symbol,
            span=sp,
            requested_start=requested_start,
            with_avr=with_avr,
            with_avr_multiscale=with_avr_multiscale,
            check_avr_fn=check_avr_fn,
            check_ms_fn=check_ms_fn,
            seconds_fn=seconds_fn,
        )
        resolved.append(row)
        if row["span_analyzable"]:
            analyzable.append(row)
        else:
            excluded.append(
                {
                    "span_id": f"ex_avr_{row.get('span_id')}",
                    "start": row["start"],
                    "end": row["end"],
                    "duration_seconds": row.get("duration_seconds"),
                    "reason": row.get("span_exclude_reason") or SPAN_NOT_ANALYZABLE,
                    "hard_local_gap": False,
                    "local_only": True,
                }
            )

    first = analyzable[0] if analyzable else (resolved[0] if resolved else None)
    avr_feature_start = None
    if analyzable:
        avr_feature_start = min(_p(s["final_analysis_eligible_start"]) for s in analyzable)

    identity = {
        "warmup_anchor_policy": WARMUP_ANCHOR_POLICY,
        "usable_spans_hash": usable_spans_hash(resolved),
        "base_analysis_eligible_start": None if not resolved else resolved[0].get("base_analysis_eligible_start"),
        "final_analysis_eligible_start": None
        if avr_feature_start is None
        else format_utc_z(avr_feature_start),
    }
    return {
        "policy": WARMUP_ANCHOR_POLICY,
        "requested_start": format_utc_z(requested_start.astimezone(timezone.utc)),
        "requested_end": format_utc_z(requested_end.astimezone(timezone.utc)),
        "spans": resolved,
        "analyzable_spans": analyzable,
        "excluded_spans": excluded,
        "any_analyzable": bool(analyzable),
        "avr_feature_start": None if avr_feature_start is None else format_utc_z(avr_feature_start),
        "avr_warmup": None if not first else first.get("avr_warmup"),
        "avr_multiscale_coverage": None if not first else first.get("avr_multiscale_coverage"),
        "run_key_identity": identity,
        "contract": avr_contract_constants() if with_avr else {},
    }


def render_eligible_warmup_terminal(
    *,
    requested_start: str,
    requested_end: str,
    resolved: dict[str, Any],
) -> str:
    lines = [
        "",
        "LOCALIZED COVERAGE",
        "",
        "Requested window:",
        f"  {requested_start}–{requested_end}",
        "",
    ]
    for s in resolved.get("spans") or []:
        lines.extend(
            [
                "Usable span:",
                f"  {s.get('usable_span_start')}–{s.get('usable_span_end')}",
                "",
                "Build/Warmup only:",
                f"  {s.get('usable_span_start')}–{s.get('base_analysis_eligible_start')}",
                "",
                "Base analysis eligible:",
                f"  {s.get('base_analysis_eligible_start')}",
                "",
                "AVR required history:",
                f"  {s.get('avr_required_history_start')}–{s.get('avr_required_history_end')}",
                "",
                "AVR ready from:",
                f"  {s.get('avr_ready_from')}",
                "",
                "Final analysis eligible:",
                f"  {s.get('final_analysis_eligible_start')}",
                "",
                f"AVR warmup status:      {s.get('avr_warmup_status')}",
                f"AVR multiscale status:  {s.get('avr_multiscale_status')}",
                f"Source vs baseline:     {s.get('source_vs_baseline')}",
                f"Span analyzable:        {s.get('span_analyzable')}",
                "",
            ]
        )
    if not resolved.get("any_analyzable"):
        lines.append("No analyzable span after eligible-anchored AVR warmup.")
    return "\n".join(lines) + "\n"
