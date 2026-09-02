"""Liquidation flow facts contract v1 — frozen causal volume-based description."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import iso_z, utc
from .facts import json_safe, window_trade_facts
from .liquidation_flow_contract import (
    ATTRIBUTION_METHOD,
    BYBIT_ALL_LIQUIDATION_DOCS_URL,
    EVENT_KEY_FORMAT,
    EVENT_KEY_VERSION,
    INPUT_SOURCES,
    LIQUIDATION_FLOW_CONTRACT,
    LIQUIDATION_FLOW_CONTRACT_FROZEN,
    PHASE_ROLE_FROM_ANALYSIS,
    PHASE_ROLE_CAUSAL,
    PHASE_ROLE_HINDSIGHT,
    SENSITIVITY_WINDOWS_MS,
    SUPERSEDED_EXPLANATORY_AUDIT,
    UNITS,
    frozen_contract_schema,
    map_bybit_position_side,
    phase_live_usable,
)

BURST_GAP_VARIANTS_SECONDS = (1, 5, 30)
ROOT = Path(__file__).resolve().parents[2]


def build_liquidation_flow_facts(
    *,
    trades: list[dict[str, Any]],
    liq_events: list[dict[str, Any]],
    liq_load_meta: dict[str, Any],
    oi_rows: list[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    anchor: datetime,
    outer_edge_price: float | None,
    reclaim_events: list[dict[str, Any]] | None = None,
    git_head: str | None = None,
) -> dict[str, Any]:
    window_start = utc(window_start)
    window_end = utc(window_end)
    anchor = utc(anchor)

    window_trades = [t for t in trades if window_start <= t["ts"] < window_end]
    window_liqs = [e for e in liq_events if window_start <= e["event_time"] < window_end]

    short_liqs = [e for e in window_liqs if e["liquidated_side"] == "LIQUIDATED_SHORT"]
    long_liqs = [e for e in window_liqs if e["liquidated_side"] == "LIQUIDATED_LONG"]

    buy_trades = [t for t in window_trades if t["side"] == "Buy"]
    sell_trades = [t for t in window_trades if t["side"] == "Sell"]

    total_taker_buy_base = sum(t["size"] for t in buy_trades)
    total_taker_sell_base = sum(t["size"] for t in sell_trades)
    total_taker_buy_quote = sum(t["notional"] for t in buy_trades)
    total_taker_sell_quote = sum(t["notional"] for t in sell_trades)

    short_base = sum(e["executed_base_size"] for e in short_liqs)
    long_base = sum(e["executed_base_size"] for e in long_liqs)
    short_bkr_ref = sum(e["bankruptcy_reference_quote"] for e in short_liqs)
    long_bkr_ref = sum(e["bankruptcy_reference_quote"] for e in long_liqs)

    phase_bounds = _derive_phase_boundaries(
        window_trades,
        anchor=anchor,
        window_end=window_end,
        outer_edge_price=outer_edge_price,
        reclaim_events=reclaim_events or [],
    )
    phase_rows = _build_phase_rows(
        window_trades,
        window_liqs,
        oi_rows,
        phase_bounds,
        sensitivity_ms=500,
    )

    sensitivity_rows: list[dict[str, Any]] = []
    allocation_rows: list[dict[str, Any]] = []
    for win_ms in SENSITIVITY_WINDOWS_MS:
        allocs, sens = _allocate_volume(
            window_liqs,
            window_trades,
            window_ms=win_ms,
            total_taker_buy_base=total_taker_buy_base,
            total_taker_sell_base=total_taker_sell_base,
        )
        sensitivity_rows.append(sens)
        for row in allocs:
            row["sensitivity_window_ms"] = win_ms
            allocation_rows.append(row)

    event_rows = [_event_row(e) for e in window_liqs]
    bursts = _detect_bursts(window_liqs)

    summary = {
        "contract_version": LIQUIDATION_FLOW_CONTRACT,
        "contract_frozen": LIQUIDATION_FLOW_CONTRACT_FROZEN,
        "interpretation_status": "NOT_EVALUATED",
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "direction": None,
        "raw_liquidation_event_count": liq_load_meta.get("raw_row_count", len(window_liqs)),
        "unique_liquidation_event_count": len(window_liqs),
        "duplicate_liquidation_event_count": liq_load_meta.get("duplicate_event_count", 0),
        "short_liquidation_event_count": len(short_liqs),
        "long_liquidation_event_count": len(long_liqs),
        "short_liquidation_executed_base_size": short_base,
        "long_liquidation_executed_base_size": long_base,
        "short_liquidation_bankruptcy_reference_quote": short_bkr_ref,
        "long_liquidation_bankruptcy_reference_quote": long_bkr_ref,
        "execution_price": None,
        "execution_notional": None,
        "total_taker_buy_base": total_taker_buy_base,
        "total_taker_sell_base": total_taker_sell_base,
        "total_taker_buy_quote": total_taker_buy_quote,
        "total_taker_sell_quote": total_taker_sell_quote,
        "taker_delta_quote": total_taker_buy_quote - total_taker_sell_quote,
        "liquidation_to_trade_direct_id_available": False,
        "attribution_method": ATTRIBUTION_METHOD,
        "attribution_decision_eligible": False,
        "bankruptcy_reference_quote_definition": "sum(executed_base_size × bankruptcy_price); NOT execution quote",
        "matching_sensitivity": sensitivity_rows,
        "phase_boundaries": phase_bounds,
        "liquidation_bursts": bursts,
        "superseded_explanatory_audit": SUPERSEDED_EXPLANATORY_AUDIT,
    }

    manifest = _build_manifest(
        window_start=window_start,
        window_end=window_end,
        anchor=anchor,
        liq_load_meta=liq_load_meta,
        window_trades=window_trades,
        window_liqs=window_liqs,
        total_taker_buy_base=total_taker_buy_base,
        git_head=git_head or _resolve_git_head(),
    )

    return json_safe(
        {
            "summary": summary,
            "manifest": manifest,
            "events": event_rows,
            "allocations": allocation_rows,
            "sensitivity": sensitivity_rows,
            "phases": phase_rows,
        }
    )


def _build_manifest(
    *,
    window_start: datetime,
    window_end: datetime,
    anchor: datetime,
    liq_load_meta: dict[str, Any],
    window_trades: list[dict[str, Any]],
    window_liqs: list[dict[str, Any]],
    total_taker_buy_base: float,
    git_head: str,
) -> dict[str, Any]:
    input_fp = _fingerprint(
        {
            "window_start_utc": iso_z(window_start),
            "window_end_utc": iso_z(window_end),
            "anchor_utc": iso_z(anchor),
            "unique_liquidation_events": len(window_liqs),
            "deduped_trades": len(window_trades),
            "total_taker_buy_base": total_taker_buy_base,
        }
    )
    output_fp = _fingerprint(
        {
            "contract_version": LIQUIDATION_FLOW_CONTRACT,
            "unique_liquidation_events": len(window_liqs),
            "short_events": sum(1 for e in window_liqs if e["liquidated_side"] == "LIQUIDATED_SHORT"),
            "long_events": sum(1 for e in window_liqs if e["liquidated_side"] == "LIQUIDATED_LONG"),
        }
    )
    return {
        **frozen_contract_schema(),
        "git_head": git_head,
        "utc_window": {
            "start": iso_z(window_start),
            "end": iso_z(window_end),
            "anchor": iso_z(anchor),
        },
        "input_sources": INPUT_SOURCES,
        "attribution_algorithm": {
            "method": ATTRIBUTION_METHOD,
            "windows_ms": list(SENSITIVITY_WINDOWS_MS),
            "short_liquidation_matches": "Taker Buy only",
            "long_liquidation_matches": "Taker Sell only",
            "global_trade_capacity": "single-use per trade_id; partial allocation allowed",
            "tie_break": "smallest |Δt| then trade_ts then trade_id",
            "price_match_required": False,
            "exchange_seq_used": False,
        },
        "units": UNITS,
        "event_key_version": EVENT_KEY_VERSION,
        "event_key_format": EVENT_KEY_FORMAT,
        "bybit_documentation_url": BYBIT_ALL_LIQUIDATION_DOCS_URL,
        "dedup_and_coverage": {
            "liquidation_dedup_key": liq_load_meta.get("dedup_key", "event_key"),
            "raw_liquidation_row_count": liq_load_meta.get("raw_row_count"),
            "unique_liquidation_event_count": liq_load_meta.get("unique_event_count"),
            "duplicate_liquidation_event_count": liq_load_meta.get("duplicate_event_count"),
            "public_trade_dedup": "trade_id",
        },
        "input_fingerprint_sha256": input_fp,
        "output_fingerprint_sha256": output_fp,
        "volume_identity": "total_taker_buy_base = allocated_liquidation_base + remaining_unattributed_taker_buy_base",
        "forbidden": [
            "subtract_liquidation_quote_from_taker_delta",
            "label_bankruptcy_reference_quote_as_execution_notional",
            "equate_event_count_with_position_count",
            "direct_id_association_without_shared_field",
            "double_allocate_same_trade_volume",
            "load_superseded_explanatory_audit_outputs",
        ],
    }


def _fingerprint(obj: dict[str, Any]) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _resolve_git_head() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "UNKNOWN"


def _event_row(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_time": iso_z(e["event_time"]),
        "event_key": e["event_key"],
        "liquidated_side": e["liquidated_side"],
        "position_side_raw": e.get("position_side_raw"),
        "forced_trade_direction": e["forced_trade_direction"],
        "executed_base_size": e["executed_base_size"],
        "bankruptcy_price": e["bankruptcy_price"],
        "bankruptcy_reference_quote": e["bankruptcy_reference_quote"],
        "execution_price": None,
        "execution_notional": None,
        "data_quality": "DEDUPED_BY_EVENT_KEY",
    }


def _derive_phase_boundaries(
    trades: list[dict[str, Any]],
    *,
    anchor: datetime,
    window_end: datetime,
    outer_edge_price: float | None,
    reclaim_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    post_anchor = [t for t in trades if t["ts"] >= anchor]
    outer_cross_ts = None
    if outer_edge_price is not None:
        for t in post_anchor:
            if t["price"] >= outer_edge_price:
                outer_cross_ts = t["ts"]
                break

    peak_ts = None
    peak_price = None
    if outer_cross_ts:
        segment = [t for t in trades if outer_cross_ts <= t["ts"] < window_end]
        if segment:
            peak_t = max(segment, key=lambda t: t["price"])
            peak_ts = peak_t["ts"]
            peak_price = peak_t["price"]

    reclaim_ts = None
    reclaim_price = None
    for rc in sorted(reclaim_events, key=lambda r: r.get("cross_ts") or ""):
        if rc.get("event_status") == "CANONICAL_RECLAIM_OBSERVED" and rc.get("cross_ts"):
            reclaim_ts = _parse_ts(rc["cross_ts"])
            reclaim_price = rc.get("cross_price")
            break

    specs: list[tuple[str, datetime | None, datetime | None, str, bool]] = [
        ("ANCHOR_TO_OUTER_CROSS", anchor, outer_cross_ts or anchor, "CAUSAL_OBSERVABLE", False),
        (
            "OUTER_CROSS_TO_PEAK",
            outer_cross_ts or anchor,
            peak_ts or window_end,
            "EXPLANATORY_HINDSIGHT_SEGMENT",
            False,
        ),
        (
            "PEAK_TO_RECLAIM",
            peak_ts or outer_cross_ts or anchor,
            reclaim_ts or window_end,
            "EXPLANATORY_HINDSIGHT_SEGMENT",
            False,
        ),
        (
            "RECLAIM_TO_WINDOW_END",
            reclaim_ts or peak_ts or anchor,
            window_end,
            "CAUSAL_OBSERVABLE" if reclaim_ts else "PARTIAL_BOUNDARY",
            reclaim_ts is None,
        ),
    ]
    rows = []
    for name, start, end, analysis_role, partial in specs:
        if start is None or end is None or start >= end:
            continue
        phase_role = PHASE_ROLE_FROM_ANALYSIS.get(analysis_role, PHASE_ROLE_CAUSAL)
        rows.append(
            {
                "phase": name,
                "start_ts": iso_z(start),
                "end_ts": iso_z(end),
                "analysis_role": analysis_role,
                "phase_role": phase_role,
                "usable_for_live_signal": phase_live_usable(phase_role, partial_boundary=partial),
                "outer_edge_price": outer_edge_price,
                "peak_price": peak_price,
                "reclaim_price": reclaim_price,
            }
        )
    return rows


def _build_phase_rows(
    trades: list[dict[str, Any]],
    liqs: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    phase_bounds: list[dict[str, Any]],
    *,
    sensitivity_ms: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pb in phase_bounds:
        start = _parse_ts(pb["start_ts"])
        end = _parse_ts(pb["end_ts"])
        wf = window_trade_facts(trades, start, end, label=pb["phase"])
        phase_liqs = [e for e in liqs if start <= e["event_time"] < end]
        short_l = [e for e in phase_liqs if e["liquidated_side"] == "LIQUIDATED_SHORT"]
        long_l = [e for e in phase_liqs if e["liquidated_side"] == "LIQUIDATED_LONG"]
        oi_chunk = [r for r in oi_rows if start <= r["ts"] < end]
        oi_start = oi_chunk[0]["oi"] if oi_chunk else None
        oi_end = oi_chunk[-1]["oi"] if oi_chunk else None
        oi_delta = (oi_end - oi_start) if oi_start is not None and oi_end is not None else None

        buy_base = sum(t["size"] for t in trades if start <= t["ts"] < end and t["side"] == "Buy")
        sell_base = sum(t["size"] for t in trades if start <= t["ts"] < end and t["side"] == "Sell")
        _, sens = _allocate_volume(
            phase_liqs,
            [t for t in trades if start <= t["ts"] < end],
            window_ms=sensitivity_ms,
            total_taker_buy_base=buy_base,
            total_taker_sell_base=sell_base,
        )
        rows.append(
            {
                "phase": pb["phase"],
                "phase_role": pb["phase_role"],
                "usable_for_live_signal": pb["usable_for_live_signal"],
                "analysis_role": pb.get("analysis_role"),
                "start_ts": pb["start_ts"],
                "end_ts": pb["end_ts"],
                "short_liquidation_count": len(short_l),
                "long_liquidation_count": len(long_l),
                "short_liquidation_executed_base_size": sum(e["executed_base_size"] for e in short_l),
                "long_liquidation_executed_base_size": sum(e["executed_base_size"] for e in long_l),
                "short_bankruptcy_reference_quote": sum(e["bankruptcy_reference_quote"] for e in short_l),
                "long_bankruptcy_reference_quote": sum(e["bankruptcy_reference_quote"] for e in long_l),
                "total_taker_buy_base": buy_base,
                "total_taker_sell_base": sell_base,
                "total_taker_buy_quote": wf.get("buy_notional"),
                "total_taker_sell_quote": wf.get("sell_notional"),
                "taker_delta_quote": wf.get("delta_notional"),
                "price_change_bps": wf.get("price_change_bps"),
                "oi_start": oi_start,
                "oi_end": oi_end,
                "oi_delta": oi_delta,
                "allocated_liquidation_base": sens.get("allocated_liquidation_base"),
                "remaining_unattributed_taker_buy_base": sens.get("remaining_unattributed_taker_buy_base"),
                "matching_sensitivity_ms": sensitivity_ms,
            }
        )
    return rows


def _allocate_volume(
    liq_events: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    window_ms: int,
    total_taker_buy_base: float,
    total_taker_sell_base: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    trade_capacity: dict[str, float] = {t["trade_id"]: t["size"] for t in trades}
    trade_meta = {t["trade_id"]: t for t in trades}
    allocations: list[dict[str, Any]] = []

    sorted_liqs = sorted(liq_events, key=lambda e: (e["event_time"], e["event_key"]))
    events_with_capacity = 0
    allocated_liq_base = 0.0

    union_trade_ids: set[str] = set()

    for liq in sorted_liqs:
        required_side = "Buy" if liq["liquidated_side"] == "LIQUIDATED_SHORT" else "Sell"
        liq_remaining = liq["executed_base_size"]
        et = liq["event_time"]
        matched_any = False

        candidates: list[tuple[float, datetime, str, dict[str, Any]]] = []
        for tid, cap in trade_capacity.items():
            if cap <= 1e-12:
                continue
            t = trade_meta[tid]
            if t["side"] != required_side:
                continue
            dt_ms = abs((t["ts"] - et).total_seconds() * 1000.0)
            if dt_ms <= window_ms:
                candidates.append((dt_ms, t["ts"], tid, t))

        candidates.sort(key=lambda x: (x[0], x[1], x[2]))

        for dt_ms, _, tid, t in candidates:
            if liq_remaining <= 1e-12:
                break
            avail = trade_capacity[tid]
            if avail <= 1e-12:
                continue
            alloc_base = min(liq_remaining, avail)
            trade_capacity[tid] -= alloc_base
            liq_remaining -= alloc_base
            matched_any = True
            union_trade_ids.add(tid)
            allocated_liq_base += alloc_base
            allocations.append(
                {
                    "liquidation_event_key": liq["event_key"],
                    "liquidation_event_time": iso_z(et),
                    "liquidated_side": liq["liquidated_side"],
                    "forced_trade_direction": liq["forced_trade_direction"],
                    "trade_id": tid,
                    "trade_ts": iso_z(t["ts"]),
                    "trade_side": t["side"],
                    "allocated_liquidation_base": alloc_base,
                    "time_distance_ms": dt_ms,
                    "trade_price_heuristic": t["price"],
                    "bankruptcy_price": liq["bankruptcy_price"],
                    "execution_price": None,
                    "execution_notional": None,
                    "identification_status": "NOT_DIRECTLY_IDENTIFIED",
                    "association_type": ATTRIBUTION_METHOD,
                }
            )

        if matched_any:
            events_with_capacity += 1

    total_liq_base = sum(e["executed_base_size"] for e in liq_events) or 0.0
    buy_used = sum(a["allocated_liquidation_base"] for a in allocations if a["trade_side"] == "Buy")
    sell_used = sum(a["allocated_liquidation_base"] for a in allocations if a["trade_side"] == "Sell")

    union_buy_base = sum(
        trade_meta[tid]["size"]
        for tid in union_trade_ids
        if trade_meta[tid]["side"] == "Buy"
    )

    unallocated_liq_base = max(total_liq_base - allocated_liq_base, 0.0)
    remaining_buy = max(total_taker_buy_base - buy_used, 0.0)
    remaining_sell = max(total_taker_sell_base - sell_used, 0.0)

    share_total = buy_used / total_taker_buy_base if total_taker_buy_base else None
    capacity_pct = (allocated_liq_base / total_liq_base * 100.0) if total_liq_base else None

    sens = {
        "sensitivity_window_ms": window_ms,
        "association_type": ATTRIBUTION_METHOD,
        "identification_status": "NOT_DIRECTLY_IDENTIFIED",
        "liquidation_event_count": len(liq_events),
        "events_with_candidate_trade_capacity": events_with_capacity,
        "total_liquidation_executed_base_size": total_liq_base,
        "allocated_liquidation_base": allocated_liq_base,
        "unallocated_liquidation_base": unallocated_liq_base,
        "total_taker_buy_base": total_taker_buy_base,
        "total_taker_sell_base": total_taker_sell_base,
        "remaining_unattributed_taker_buy_base": remaining_buy,
        "remaining_unattributed_taker_sell_base": remaining_sell,
        "allocated_liquidation_share_of_total_taker_buy_base": share_total,
        "union_window_taker_buy_base": union_buy_base,
        "liquidation_capacity_coverage_pct": capacity_pct,
        "double_counted_trade_volume_base": 0.0,
        "coverage_status": "COMPUTED" if liq_events else "NO_LIQUIDATIONS_IN_WINDOW",
        "volume_identity_check": abs(total_taker_buy_base - (buy_used + remaining_buy)) < 1e-9,
    }
    return allocations, sens


def _detect_bursts(liqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not liqs:
        return []
    bursts: list[dict[str, Any]] = []
    sorted_liqs = sorted(liqs, key=lambda e: (e["event_time"], e["event_key"]))
    for gap_s in BURST_GAP_VARIANTS_SECONDS:
        gap = timedelta(seconds=gap_s)
        cluster: list[dict[str, Any]] = [sorted_liqs[0]]
        for ev in sorted_liqs[1:]:
            if ev["event_time"] - cluster[-1]["event_time"] <= gap:
                cluster.append(ev)
            else:
                if len(cluster) >= 2:
                    bursts.append(_burst_row(cluster, gap_s))
                cluster = [ev]
        if len(cluster) >= 2:
            bursts.append(_burst_row(cluster, gap_s))
    return bursts


def _burst_row(cluster: list[dict[str, Any]], gap_s: int) -> dict[str, Any]:
    return {
        "burst_gap_seconds": gap_s,
        "classification": "UNFROZEN_SENSITIVITY_ONLY",
        "event_count": len(cluster),
        "start_ts": iso_z(cluster[0]["event_time"]),
        "end_ts": iso_z(cluster[-1]["event_time"]),
        "short_executed_base_size": sum(
            e["executed_base_size"] for e in cluster if e["liquidated_side"] == "LIQUIDATED_SHORT"
        ),
        "long_executed_base_size": sum(
            e["executed_base_size"] for e in cluster if e["liquidated_side"] == "LIQUIDATED_LONG"
        ),
    }


def _parse_ts(raw: str | datetime) -> datetime:
    if isinstance(raw, datetime):
        return utc(raw)
    return utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))


# Re-export for callers/tests.
__all__ = [
    "LIQUIDATION_FLOW_CONTRACT",
    "ATTRIBUTION_METHOD",
    "build_liquidation_flow_facts",
    "map_bybit_position_side",
]
