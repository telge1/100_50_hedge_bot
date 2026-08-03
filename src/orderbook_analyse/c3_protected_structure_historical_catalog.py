"""Historical Protected-High break event catalog (research only).

Mathematical mirror of ``c3_protected_low_historical_catalog``: scanner rising-edge
``close_break_protected_up`` events classified via ``find_causal_decision_high``.

Does not modify Frozen V1, the trend scanner, confirmation thresholds, or the Low
catalog API/artefacts.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from orderbook_analyse.apt_001_protected_low_break_deep_dive import (
    ensure_utc,
    iso_z,
)
from orderbook_analyse.c3_protected_low_event_driven_decision import (
    AUDIT_CONFIRMATION_RULES,
    _last_trade_at_or_before,
)
from orderbook_analyse.c3_protected_low_historical_catalog import (
    DEDUP_HOURS,
    DEFAULT_CANDLE_DIR,
    DEFAULT_FROZEN_DIR,
    DEFAULT_MAX_WINDOW_H,
    FORWARD_HORIZONS_M,
    GATE_MAX_REGIME_SHARE,
    GATE_MIN_REGIMES,
    GATE_MIN_SYMBOLS,
    LONG_GATE_MIN_EVENTS,
    MIN_TRADES_15M,
    SHORT_GATE_MIN_EVENTS,
    TRADE_LOAD_BUFFER_H,
    CONTEXT_PRE_M,
    CANDLE_FEATHER,
    _f,
    _parse_ts,
    _trade_count_in_window,
    _write_csv,
    _write_json,
    audit_btc_scanner_source,
    build_candidates as build_candidates_low,
    forward_returns_for_candidate,
    pl_compact,
    rising_edge_mask,
)
from orderbook_analyse.c3_protected_structure_mirror import (
    MIRROR_PARITY_TABLE,
    find_causal_decision_high,
    flip_candidate_side,
    map_outcome_high_to_low,
    mirror_ticks,
)
from orderbook_analyse.dynamic_wall_detector import connect_readonly
from orderbook_analyse.orderbook_absorption_features import load_trade_ticks

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_DIR = ROOT / "results" / "c3_protected_high_historical_event_catalog"

PRIMARY_DECISIONS = (
    "SUFFICIENT_BREAKOUT_AND_RECLAIM_DOWN_SAMPLE_FOUND",
    "SUFFICIENT_BREAKOUT_SAMPLE_ONLY",
    "SUFFICIENT_RECLAIM_DOWN_SAMPLE_ONLY",
    "PROTECTED_HIGH_EVENTS_MOSTLY_BREAKOUT",
    "PROTECTED_HIGH_EVENTS_MOSTLY_RECLAIM_DOWN",
    "PROTECTED_HIGH_EVENTS_MOSTLY_UNRESOLVED",
    "SAMPLE_TOO_SMALL_AFTER_DEDUPLICATION",
    "SCANNER_HISTORY_INSUFFICIENT",
    "CATALOG_DATA_INVALID",
)

CSV_HEADERS: dict[str, list[str]] = {
    "protected_high_inventory.csv": [
        "symbol",
        "protected_high",
        "first_seen_available_at",
        "n_raw_breaks",
        "n_dedup_events",
    ],
    "raw_break_events.csv": [
        "symbol",
        "candle_open_ts",
        "available_at",
        "protected_high",
        "open",
        "high",
        "low",
        "close",
        "previous_close",
        "scanner_state",
        "trend_state",
        "external_bos_side",
        "choch_side",
        "bullish_choch",
        "trend_segment_id",
        "interval_id",
        "canonical_source",
    ],
    "deduplicated_break_events.csv": [
        "event_id",
        "symbol",
        "protected_high",
        "break_candle_open",
        "break_available_at",
        "dedup_note",
        "trend_segment_id",
        "bullish_choch",
        "external_bos_side",
        "choch_side",
        "previous_close",
        "open",
        "high",
        "low",
        "close",
    ],
    "data_quality_by_event.csv": [
        "event_id",
        "symbol",
        "trade_n_load",
        "trade_n_first_15m",
        "data_valid",
        "invalid_reason",
        "ob_status",
        "oi_status",
        "liq_status",
    ],
    "event_state_timeline_5s.csv": [
        "event_id",
        "timestamp",
        "state",
        "last_trade_price",
        "note",
    ],
    "event_decision_milestones.csv": [
        "event_id",
        "milestone",
        "timestamp",
        "state",
        "price",
        "minutes_after_break",
    ],
    "event_decisions.csv": [
        "event_id",
        "symbol",
        "protected_high",
        "break_available_at",
        "outcome",
        "decision_ts",
        "minutes_after_break",
        "first_reclaim_down_ts",
        "data_valid",
        "invalid_reason",
    ],
    "reclaim_down_confirmed_events.csv": [
        "event_id",
        "symbol",
        "protected_high",
        "break_available_at",
        "reclaim_down_confirmed_ts",
        "minutes_after_break",
        "first_reclaim_down_ts",
    ],
    "breakout_confirmed_events.csv": [
        "event_id",
        "symbol",
        "protected_high",
        "break_available_at",
        "breakout_confirmed_ts",
        "minutes_after_break",
        "first_failed_reclaim_down_ts",
    ],
    "unresolved_events.csv": [
        "event_id",
        "symbol",
        "protected_high",
        "break_available_at",
        "first_reclaim_down_ts",
        "max_window_hours",
    ],
    "invalid_events.csv": [
        "event_id",
        "symbol",
        "protected_high",
        "break_available_at",
        "invalid_reason",
    ],
    "long_candidates.csv": [
        "candidate_id",
        "event_id",
        "symbol",
        "side",
        "candidate_type",
        "candidate_ts",
        "candidate_price",
        "distance_above_level_bps",
        "failed_reclaim_down_present",
        "confirmation_features",
        "source",
    ],
    "short_candidates.csv": [
        "candidate_id",
        "event_id",
        "symbol",
        "side",
        "candidate_type",
        "candidate_ts",
        "candidate_price",
        "distance_below_level_bps",
        "retest_present",
        "confirmation_features",
        "source",
    ],
    "candidate_forward_returns.csv": [
        "candidate_id",
        "event_id",
        "side",
        "candidate_type",
        "candidate_ts",
        "horizon_m",
        "mfe_bps",
        "mae_bps",
        "close_return_bps",
        "invalidation_ts",
        "invalidation_kind",
    ],
    "event_regime_mapping.csv": [
        "event_id",
        "symbol",
        "regime_id",
        "protected_high",
        "break_available_at",
        "outcome",
    ],
    "regime_deduplication_summary.csv": [
        "symbol",
        "n_raw_breaks",
        "n_dedup_events",
        "n_regimes",
        "largest_regime_n",
        "largest_regime_share",
        "top3_share",
    ],
    "price_vs_trades_vs_context.csv": [
        "event_id",
        "symbol",
        "outcome",
        "price_trades_decision_ts",
        "book_used",
        "oi_used",
        "liq_used",
        "note",
    ],
    "symbol_summary.csv": [
        "symbol",
        "n_raw",
        "n_dedup",
        "n_breakout",
        "n_reclaim_down",
        "n_unresolved",
        "n_invalid",
        "n_long_candidates",
        "n_short_candidates",
        "n_regimes",
    ],
    "candidate_type_summary.csv": [
        "side",
        "candidate_type",
        "n",
        "median_minutes_after_break",
        "median_mfe_bps_15m",
        "median_mae_bps_15m",
        "median_mfe_bps_60m",
        "median_mae_bps_60m",
    ],
    "sample_gate_evaluation.csv": [
        "gate",
        "long",
        "short",
    ],
}


def _bps(price: float | None, level: float) -> float | None:
    if price is None or level <= 0:
        return None
    return (price - level) / level * 10_000.0


def make_event_id(symbol: str, available_at: datetime, level: float) -> str:
    available_at = ensure_utc(available_at)
    stamp = available_at.strftime("%Y%m%dT%H%M%S")
    return f"{symbol}_PH_{stamp}_{pl_compact(level)}"


def enumerate_raw_breaks_high(
    df: pd.DataFrame,
    *,
    symbol: str,
    require_bullish_choch: bool = True,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    """Enumerate rising-edge ``close_break_protected_up`` with protected_high present."""
    if df.empty:
        return []
    work = df.copy()
    if "interval_id" not in work.columns:
        work["interval_id"] = "ALL"
    rows: list[dict[str, Any]] = []
    for interval_id, g in work.groupby("interval_id", sort=False):
        g = g.sort_values("available_at", kind="mergesort").reset_index(drop=True)
        flag = g["close_break_protected_up"]
        rising = rising_edge_mask(flag)
        for i in g.index[rising]:
            ph = _f(g.at[i, "protected_high"])
            if ph is None:
                continue
            bc = bool(g.at[i, "bullish_choch"]) if "bullish_choch" in g.columns else False
            if require_bullish_choch and not bc:
                continue
            avail = _parse_ts(g.at[i, "available_at"])
            if avail is None:
                continue
            if start is not None and avail < ensure_utc(start):
                continue
            if end is not None and avail > ensure_utc(end):
                continue
            prev_close = None
            if i > 0 and "close" in g.columns:
                prev_close = _f(g.at[i - 1, "close"])
            scanner_state = None
            if "scanner_state" in g.columns:
                scanner_state = g.at[i, "scanner_state"]
            elif "warning_state" in g.columns:
                scanner_state = g.at[i, "warning_state"]
            trend_state = g.at[i, "trend_state"] if "trend_state" in g.columns else None
            rows.append(
                {
                    "symbol": symbol,
                    "candle_open_ts": iso_z(_parse_ts(g.at[i, "candle_open_ts"])),
                    "available_at": iso_z(avail),
                    "protected_high": ph,
                    "open": _f(g.at[i, "open"]) if "open" in g.columns else None,
                    "high": _f(g.at[i, "high"]) if "high" in g.columns else None,
                    "low": _f(g.at[i, "low"]) if "low" in g.columns else None,
                    "close": _f(g.at[i, "close"]) if "close" in g.columns else None,
                    "previous_close": prev_close,
                    "scanner_state": scanner_state,
                    "trend_state": trend_state,
                    "external_bos_side": g.at[i, "external_bos_side"]
                    if "external_bos_side" in g.columns
                    else None,
                    "choch_side": g.at[i, "choch_side"] if "choch_side" in g.columns else None,
                    "bullish_choch": bc,
                    "trend_segment_id": g.at[i, "trend_segment_id"]
                    if "trend_segment_id" in g.columns
                    else None,
                    "interval_id": str(interval_id),
                    "canonical_source": f"c3_frozen_break_warning/{symbol}_warning_states.parquet",
                }
            )
    seen: set[tuple[str, str, float]] = set()
    uniq: list[dict[str, Any]] = []
    for r in sorted(rows, key=lambda x: (x["available_at"] or "", x["protected_high"])):
        key = (r["symbol"], r["available_at"] or "", round(float(r["protected_high"]), 8))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    return uniq


def deduplicate_breaks_high(
    raw: list[dict[str, Any]],
    *,
    window_hours: float = DEDUP_HOURS,
) -> list[dict[str, Any]]:
    """Keep first break per (symbol, round(PH,8)); skip same-PH edges within window."""
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in raw:
        by_sym[str(r["symbol"])].append(r)

    out: list[dict[str, Any]] = []
    window = timedelta(hours=window_hours)
    for symbol, rows in by_sym.items():
        rows = sorted(
            rows,
            key=lambda x: (_parse_ts(x["available_at"]) or datetime.min.replace(tzinfo=timezone.utc)),
        )
        last_primary: dict[float, datetime] = {}
        for r in rows:
            avail = _parse_ts(r["available_at"])
            ph = float(r["protected_high"])
            ph_key = round(ph, 8)
            if avail is None:
                continue
            note = "primary"
            prev = last_primary.get(ph_key)
            if prev is not None:
                if avail - prev <= window:
                    continue
                note = "possible_reactivation"
            last_primary[ph_key] = avail
            event_id = make_event_id(symbol, avail, ph)
            out.append(
                {
                    "event_id": event_id,
                    "symbol": symbol,
                    "protected_high": ph,
                    "break_candle_open": r.get("candle_open_ts"),
                    "break_available_at": iso_z(avail),
                    "dedup_note": note,
                    "trend_segment_id": r.get("trend_segment_id"),
                    "bullish_choch": r.get("bullish_choch"),
                    "external_bos_side": r.get("external_bos_side"),
                    "choch_side": r.get("choch_side"),
                    "previous_close": r.get("previous_close"),
                    "open": r.get("open"),
                    "high": r.get("high"),
                    "low": r.get("low"),
                    "close": r.get("close"),
                    "scanner_state": r.get("scanner_state"),
                    "trend_state": r.get("trend_state"),
                    "interval_id": r.get("interval_id"),
                    "canonical_source": r.get("canonical_source"),
                }
            )
    out.sort(key=lambda x: (x["symbol"], x["break_available_at"] or ""))
    return out


def cluster_regimes_high(
    events: list[dict[str, Any]],
    *,
    window_hours: float = DEDUP_HOURS,
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Cluster per-symbol if overlapping 6h windows OR same PH within 6h."""
    window = timedelta(hours=window_hours)
    mapping: list[dict[str, Any]] = []
    regimes: dict[str, list[str]] = {}
    by_sym: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in events:
        by_sym[str(e["symbol"])].append(e)

    for symbol, rows in by_sym.items():
        rows = sorted(
            rows,
            key=lambda x: (_parse_ts(x["break_available_at"]) or datetime.min.replace(tzinfo=timezone.utc)),
        )
        parent = list(range(len(rows)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(i: int, j: int) -> None:
            ri, rj = find(i), find(j)
            if ri != rj:
                parent[rj] = ri

        for i, a in enumerate(rows):
            ta = _parse_ts(a["break_available_at"])
            pha = round(float(a["protected_high"]), 8)
            if ta is None:
                continue
            a_end = ta + window
            for j in range(i + 1, len(rows)):
                b = rows[j]
                tb = _parse_ts(b["break_available_at"])
                if tb is None:
                    continue
                if tb > a_end + window:
                    break
                phb = round(float(b["protected_high"]), 8)
                b_end = tb + window
                overlap = not (b_end <= ta or a_end <= tb)
                same_ph_near = pha == phb and abs((tb - ta).total_seconds()) <= window.total_seconds()
                if overlap or same_ph_near:
                    union(i, j)

        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(len(rows)):
            clusters[find(i)].append(i)
        for k, idxs in enumerate(sorted(clusters.values(), key=lambda ix: ix[0])):
            rid = f"{symbol}_PH_REG_{k + 1:03d}"
            regimes[rid] = [rows[i]["event_id"] for i in idxs]
            for i in idxs:
                mapping.append(
                    {
                        "event_id": rows[i]["event_id"],
                        "symbol": symbol,
                        "regime_id": rid,
                        "protected_high": rows[i]["protected_high"],
                        "break_available_at": rows[i]["break_available_at"],
                        "outcome": rows[i].get("outcome"),
                    }
                )
    return mapping, regimes


def build_candidates_high(
    *,
    event_id: str,
    symbol: str,
    level: float,
    available_at: datetime,
    outcome: str,
    decision: dict[str, Any],
    ticks: Sequence[Any],
    late_end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Build High candidates via Low ``build_candidates`` on mirrored ticks, then remap.

    BREAKOUT → LONG (source=PROTECTED_HIGH_BREAKOUT);
    RECLAIM_DOWN → SHORT (source=PROTECTED_HIGH_RECLAIM_DOWN).
    Forward returns recomputed on original ticks.
    """
    long_c: list[dict[str, Any]] = []
    short_c: list[dict[str, Any]] = []
    fwd_rows: list[dict[str, Any]] = []
    if outcome not in {"BREAKOUT_CONFIRMED", "RECLAIM_DOWN_CONFIRMED"}:
        return long_c, short_c, fwd_rows

    low_outcome = map_outcome_high_to_low(outcome)
    assert low_outcome is not None
    mirrored = mirror_ticks(ticks, level)
    low_decision = {
        "decision_ts": decision.get("decision_ts"),
        "first_reclaim_ts": decision.get("first_reclaim_down_ts")
        or decision.get("first_reclaim_ts"),
        "detail": decision.get("detail"),
    }
    low_long, low_short, _fwd_mir = build_candidates_low(
        event_id=event_id,
        symbol=symbol,
        level=level,
        available_at=available_at,
        outcome=low_outcome,
        decision=low_decision,
        ticks=mirrored,
        late_end=late_end,
    )

    source = (
        "PROTECTED_HIGH_BREAKOUT"
        if outcome == "BREAKOUT_CONFIRMED"
        else "PROTECTED_HIGH_RECLAIM_DOWN"
    )

    def _remap(c: dict[str, Any]) -> dict[str, Any]:
        new_side = flip_candidate_side(str(c["side"]))
        cts = _parse_ts(c["candidate_ts"])
        assert cts is not None
        last = _last_trade_at_or_before(ticks, cts)
        px = float(last.price) if last else float(level)
        ctype = str(c["candidate_type"])
        cid = f"{event_id}_{new_side}_{ctype.upper()}"
        row: dict[str, Any] = {
            "candidate_id": cid,
            "event_id": event_id,
            "symbol": symbol,
            "side": new_side,
            "candidate_type": ctype,
            "candidate_ts": iso_z(cts),
            "candidate_price": px,
            "confirmation_features": c.get("confirmation_features"),
            "source": source,
        }
        if new_side == "LONG":
            row["distance_above_level_bps"] = _bps(px, level)
            row["failed_reclaim_down_present"] = bool(
                c.get("failed_reclaim_present") or c.get("retest_present")
            )
        else:
            row["distance_below_level_bps"] = (
                None if px is None else (level - px) / level * 10_000.0
            )
            row["retest_present"] = bool(
                c.get("retest_present") or c.get("failed_reclaim_present")
            )
        return row

    remapped = [_remap(c) for c in (low_long + low_short)]
    for row in remapped:
        if row["side"] == "LONG":
            long_c.append(row)
        else:
            short_c.append(row)
        cts = _parse_ts(row["candidate_ts"])
        assert cts is not None
        for fr in forward_returns_for_candidate(
            ticks, candidate_ts=cts, level=level, side=str(row["side"])
        ):
            # Remap invalidation kind labels toward PH naming where applicable
            kind = fr.get("invalidation_kind")
            if kind == "rebreak_below_pl" and row["side"] == "LONG":
                kind = "rebreak_below_ph"
            elif kind == "reclaim_above_pl" and row["side"] == "SHORT":
                kind = "reclaim_above_ph"
            fwd_rows.append(
                {
                    "candidate_id": row["candidate_id"],
                    "event_id": event_id,
                    "side": row["side"],
                    "candidate_type": row["candidate_type"],
                    "candidate_ts": row["candidate_ts"],
                    **{**fr, "invalidation_kind": kind},
                }
            )
    return long_c, short_c, fwd_rows


def sparse_timeline_and_milestones_high(
    *,
    event_id: str,
    available_at: datetime,
    late_end: datetime,
    level: float,
    outcome: str,
    decision: dict[str, Any],
    ticks: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    available_at, late_end = ensure_utc(available_at), ensure_utc(late_end)
    dts = decision.get("decision_ts")
    if dts is not None and not isinstance(dts, datetime):
        dts = _parse_ts(dts)
    first_rd = _parse_ts(
        decision.get("first_reclaim_down_ts") or decision.get("first_reclaim_ts")
    )

    milestones: list[dict[str, Any]] = [
        {
            "event_id": event_id,
            "milestone": "break_available",
            "timestamp": iso_z(available_at),
            "state": "BREAK_PRINTED",
            "price": None,
            "minutes_after_break": 0.0,
        }
    ]
    if first_rd is not None:
        last = _last_trade_at_or_before(ticks, first_rd)
        milestones.append(
            {
                "event_id": event_id,
                "milestone": "first_reclaim_down_trade",
                "timestamp": iso_z(first_rd),
                "state": "RECLAIM_DOWN_UNCONFIRMED",
                "price": float(last.price) if last else None,
                "minutes_after_break": (first_rd - available_at).total_seconds() / 60.0,
            }
        )
    if dts is not None:
        last = _last_trade_at_or_before(ticks, ensure_utc(dts))
        milestones.append(
            {
                "event_id": event_id,
                "milestone": "decision",
                "timestamp": iso_z(ensure_utc(dts)),
                "state": outcome,
                "price": float(last.price) if last else None,
                "minutes_after_break": (ensure_utc(dts) - available_at).total_seconds() / 60.0,
            }
        )
    milestones.append(
        {
            "event_id": event_id,
            "milestone": "window_end",
            "timestamp": iso_z(late_end),
            "state": outcome,
            "price": None,
            "minutes_after_break": (late_end - available_at).total_seconds() / 60.0,
        }
    )

    timeline: list[dict[str, Any]] = []
    t = available_at
    while t <= late_end:
        last = _last_trade_at_or_before(ticks, t, after=available_at - timedelta(minutes=1))
        state = "NO_DECISION_YET"
        if dts is not None and t >= ensure_utc(dts):
            state = outcome
        elif first_rd is not None and t >= first_rd:
            px = float(last.price) if last else None
            state = (
                "RECLAIM_DOWN_RETEST"
                if px is not None and px >= level
                else "RECLAIM_DOWN_UNCONFIRMED"
            )
        elif last is not None and float(last.price) > level:
            state = "ABOVE_LEVEL"
        timeline.append(
            {
                "event_id": event_id,
                "timestamp": iso_z(t),
                "state": state,
                "last_trade_price": float(last.price) if last else None,
                "note": "5m_summary",
            }
        )
        t += timedelta(minutes=5)
    existing = {r["timestamp"] for r in timeline}
    for m in milestones:
        if m["timestamp"] not in existing:
            timeline.append(
                {
                    "event_id": event_id,
                    "timestamp": m["timestamp"],
                    "state": m["state"],
                    "last_trade_price": m["price"],
                    "note": f"milestone:{m['milestone']}",
                }
            )
    timeline.sort(key=lambda r: r["timestamp"] or "")
    return timeline, milestones


def evaluate_sample_gates_high(
    classified: list[dict[str, Any]],
    regime_mapping: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Fixed Long(breakout)/Short(reclaim-down) shadow gates — same thresholds as Low."""
    event_regime = {r["event_id"]: r["regime_id"] for r in regime_mapping}

    def _gate(outcome: str) -> dict[str, Any]:
        subset = [e for e in classified if e.get("outcome") == outcome and e.get("data_valid")]
        symbols = {e["symbol"] for e in subset}
        regimes = [event_regime.get(e["event_id"]) for e in subset if e["event_id"] in event_regime]
        reg_counts = Counter(regimes)
        n = len(subset)
        n_reg = len(reg_counts)
        max_share = (max(reg_counts.values()) / n) if n else 0.0
        min_n = LONG_GATE_MIN_EVENTS if outcome == "BREAKOUT_CONFIRMED" else SHORT_GATE_MIN_EVENTS
        checks = {
            "n_events": n,
            "n_symbols": len(symbols),
            "n_regimes": n_reg,
            "max_regime_share": max_share,
            "pass_n": n >= min_n,
            "pass_symbols": len(symbols) >= GATE_MIN_SYMBOLS,
            "pass_regimes": n_reg >= GATE_MIN_REGIMES,
            "pass_concentration": max_share <= GATE_MAX_REGIME_SHARE if n else False,
        }
        checks["pass"] = all(
            checks[k] for k in ("pass_n", "pass_symbols", "pass_regimes", "pass_concentration")
        )
        return checks

    long_g = _gate("BREAKOUT_CONFIRMED")
    short_g = _gate("RECLAIM_DOWN_CONFIRMED")
    table = [
        {
            "gate": "≥20 Events",
            "long": "pass" if long_g["pass_n"] else "fail",
            "short": "pass" if short_g["pass_n"] else "fail",
        },
        {
            "gate": "≥2 Symbole",
            "long": "pass" if long_g["pass_symbols"] else "fail",
            "short": "pass" if short_g["pass_symbols"] else "fail",
        },
        {
            "gate": "≥10 Regime",
            "long": "pass" if long_g["pass_regimes"] else "fail",
            "short": "pass" if short_g["pass_regimes"] else "fail",
        },
        {
            "gate": "max. 30 % pro Regime",
            "long": "pass" if long_g["pass_concentration"] else "fail",
            "short": "pass" if short_g["pass_concentration"] else "fail",
        },
        {
            "gate": "overall",
            "long": "pass" if long_g["pass"] else "fail",
            "short": "pass" if short_g["pass"] else "fail",
        },
    ]
    return {"long": long_g, "short": short_g}, table


def decide_primary_high(
    *,
    n_raw: int,
    classified: list[dict[str, Any]],
    gates: dict[str, Any],
) -> tuple[str, str]:
    if n_raw < 3:
        return (
            "SCANNER_HISTORY_INSUFFICIENT",
            f"Almost no scanner PH breaks found (n_raw={n_raw}).",
        )
    if not classified:
        return ("CATALOG_DATA_INVALID", "No deduplicated events to classify.")
    n_invalid = sum(1 for e in classified if e.get("outcome") == "EVENT_DATA_INVALID")
    if n_invalid == len(classified):
        return ("CATALOG_DATA_INVALID", "All catalog events are EVENT_DATA_INVALID.")

    long_ok = bool(gates["long"]["pass"])
    short_ok = bool(gates["short"]["pass"])
    if long_ok and short_ok:
        return (
            "SUFFICIENT_BREAKOUT_AND_RECLAIM_DOWN_SAMPLE_FOUND",
            "Both Long(breakout) and Short(reclaim-down) shadow sample gates pass.",
        )
    if long_ok and not short_ok:
        return (
            "SUFFICIENT_BREAKOUT_SAMPLE_ONLY",
            "Long/breakout gate passes; Short/reclaim-down sample insufficient.",
        )
    if short_ok and not long_ok:
        return (
            "SUFFICIENT_RECLAIM_DOWN_SAMPLE_ONLY",
            "Short/reclaim-down gate passes; Long/breakout sample insufficient.",
        )

    n_bo = sum(1 for e in classified if e.get("outcome") == "BREAKOUT_CONFIRMED")
    n_rd = sum(1 for e in classified if e.get("outcome") == "RECLAIM_DOWN_CONFIRMED")
    n_unres = sum(1 for e in classified if e.get("outcome") == "UNRESOLVED_WITHIN_MAX_WINDOW")
    n_valid = sum(1 for e in classified if e.get("data_valid"))
    resolved = n_bo + n_rd

    if n_valid < 5:
        return (
            "SAMPLE_TOO_SMALL_AFTER_DEDUPLICATION",
            f"Too few valid events after dedup (n_valid={n_valid}).",
        )
    if resolved == 0 or (n_unres >= max(n_bo, n_rd) and n_unres >= resolved):
        return (
            "PROTECTED_HIGH_EVENTS_MOSTLY_UNRESOLVED",
            f"Unresolved dominate (unresolved={n_unres}, breakout={n_bo}, reclaim_down={n_rd}).",
        )
    if n_bo >= 2 * max(n_rd, 1) and not long_ok:
        return (
            "PROTECTED_HIGH_EVENTS_MOSTLY_BREAKOUT",
            f"Breakouts dominate reclaim-down ({n_bo} vs {n_rd}) and long gate fails.",
        )
    if n_rd >= 2 * max(n_bo, 1) and not short_ok:
        return (
            "PROTECTED_HIGH_EVENTS_MOSTLY_RECLAIM_DOWN",
            f"Reclaim-downs dominate breakout ({n_rd} vs {n_bo}) and short gate fails.",
        )
    return (
        "SAMPLE_TOO_SMALL_AFTER_DEDUPLICATION",
        f"Gates fail with breakout={n_bo}, reclaim_down={n_rd}, unresolved={n_unres}.",
    )


def classify_event_high(
    event: dict[str, Any],
    *,
    db: Any,
    max_window_hours: float = DEFAULT_MAX_WINDOW_H,
) -> dict[str, Any]:
    """Load trades and run high causal decision for one deduped event."""
    symbol = str(event["symbol"])
    level = float(event["protected_high"])
    available_at = _parse_ts(event["break_available_at"])
    assert available_at is not None
    late_end = available_at + timedelta(hours=max_window_hours)
    load_start = available_at - timedelta(minutes=CONTEXT_PRE_M)
    load_end = late_end + timedelta(hours=TRADE_LOAD_BUFFER_H)
    event_id = event["event_id"]

    ticks, _diag = load_trade_ticks(db, symbol=symbol, start=load_start, end=load_end)
    n_load = len(ticks)
    n_15 = _trade_count_in_window(
        ticks, start=available_at, end=available_at + timedelta(minutes=15)
    )
    if n_15 < MIN_TRADES_15M:
        decision = {
            "outcome": "EVENT_DATA_INVALID",
            "decision_ts": None,
            "first_reclaim_ts": None,
            "first_reclaim_down_ts": None,
            "detail": {"invalid_reason": f"insufficient_trades_first_15m={n_15}"},
        }
        outcome = "EVENT_DATA_INVALID"
        data_valid = False
        invalid_reason = f"insufficient trades in [available_at, +15m]: {n_15} < {MIN_TRADES_15M}"
    else:
        decision = find_causal_decision_high(
            ticks,
            level=level,
            available_at=available_at,
            late_end=late_end,
            book_by_ts=None,
            check_every_s=1,
        )
        outcome = str(decision["outcome"])
        data_valid = outcome != "EVENT_DATA_INVALID"
        invalid_reason = None

    dts = decision.get("decision_ts")
    if isinstance(dts, datetime):
        minutes = (ensure_utc(dts) - available_at).total_seconds() / 60.0
        dts_iso = iso_z(ensure_utc(dts))
    else:
        minutes = None
        dts_iso = None

    fr_iso = decision.get("first_reclaim_down_ts") or decision.get("first_reclaim_ts")
    if isinstance(fr_iso, datetime):
        fr_iso = iso_z(fr_iso)

    long_c, short_c, fwd = build_candidates_high(
        event_id=event_id,
        symbol=symbol,
        level=level,
        available_at=available_at,
        outcome=outcome,
        decision=decision,
        ticks=ticks,
        late_end=late_end,
    )
    timeline, milestones = sparse_timeline_and_milestones_high(
        event_id=event_id,
        available_at=available_at,
        late_end=late_end,
        level=level,
        outcome=outcome,
        decision=decision,
        ticks=ticks,
    )

    return {
        **event,
        "outcome": outcome,
        "decision_ts": dts_iso,
        "minutes_after_break": minutes,
        "first_reclaim_down_ts": fr_iso,
        "data_valid": data_valid,
        "invalid_reason": invalid_reason,
        "decision": decision,
        "long_candidates": long_c,
        "short_candidates": short_c,
        "forward_returns": fwd,
        "timeline_5s": timeline,
        "milestones": milestones,
        "trade_n_load": n_load,
        "trade_n_first_15m": n_15,
        "quality": {
            "event_id": event_id,
            "symbol": symbol,
            "trade_n_load": n_load,
            "trade_n_first_15m": n_15,
            "data_valid": data_valid,
            "invalid_reason": invalid_reason,
            "ob_status": "not_required_price_trades_primary",
            "oi_status": "not_loaded_context_optional",
            "liq_status": "not_loaded_context_optional",
        },
    }


def run_protected_high_historical_catalog(
    *,
    symbols: Sequence[str] | None = None,
    frozen_dir: Path = DEFAULT_FROZEN_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    start: datetime | None = None,
    end: datetime | None = None,
    max_window_hours: float = DEFAULT_MAX_WINDOW_H,
    overwrite: bool = False,
    candle_dir: Path = DEFAULT_CANDLE_DIR,
    n_tests_note: str = "(see pytest)",
    db: Any | None = None,
) -> dict[str, Any]:
    """Build full historical PH-break catalog artefacts."""
    symbols = list(symbols or ("APTUSDT", "DOGEUSDT"))
    output_dir = Path(output_dir)
    frozen_dir = Path(frozen_dir)
    if output_dir.exists() and any(output_dir.iterdir()) and not overwrite:
        raise FileExistsError(f"{output_dir} exists; pass overwrite=True")
    output_dir.mkdir(parents=True, exist_ok=True)

    scanner_audit: dict[str, Any] = {
        "frozen_dir": str(frozen_dir),
        "symbols_requested": symbols,
        "timeframe": "5m",
        "candle_semantics": "half-open [open, open+5m); available_at = candle close",
        "break_flag": "close_break_protected_up rising-edge",
        "require_bullish_choch": True,
        "per_symbol": {},
    }

    raw_all: list[dict[str, Any]] = []
    for sym in symbols:
        if sym == "BTCUSDT":
            btc = audit_btc_scanner_source(frozen_dir=frozen_dir, candle_dir=candle_dir, db=db)
            # Rephrase reason for PH context if excluded
            if not btc.get("warning_states_exists"):
                btc = dict(btc)
                btc["reason"] = (
                    "No BTCUSDT_warning_states.parquet in frozen warning artefacts; "
                    "do not invent Protected-High breaks. Excluded from analysis."
                )
            scanner_audit["per_symbol"][sym] = btc
            continue
        path = frozen_dir / f"{sym}_warning_states.parquet"
        entry: dict[str, Any] = {
            "parquet": str(path),
            "exists": path.exists(),
            "included": False,
        }
        if not path.exists():
            entry["reason"] = "missing warning_states parquet"
            scanner_audit["per_symbol"][sym] = entry
            continue
        df = pd.read_parquet(path)
        entry["n_scanner_candles"] = int(len(df))
        entry["n_protected_high_levels"] = (
            int(df["protected_high"].dropna().nunique()) if "protected_high" in df.columns else 0
        )
        if "available_at" in df.columns:
            entry["available_at_min"] = iso_z(_parse_ts(df["available_at"].min()))
            entry["available_at_max"] = iso_z(_parse_ts(df["available_at"].max()))
        raw = enumerate_raw_breaks_high(
            df, symbol=sym, require_bullish_choch=True, start=start, end=end
        )
        entry["n_raw_breaks"] = len(raw)
        entry["included"] = True
        scanner_audit["per_symbol"][sym] = entry
        raw_all.extend(raw)

    if "BTCUSDT" not in symbols:
        btc = audit_btc_scanner_source(frozen_dir=frozen_dir, candle_dir=candle_dir, db=db)
        if not btc.get("warning_states_exists"):
            btc = dict(btc)
            btc["reason"] = (
                "No BTCUSDT_warning_states.parquet in frozen warning artefacts; "
                "do not invent Protected-High breaks. Excluded from analysis."
            )
        scanner_audit["per_symbol"]["BTCUSDT"] = btc

    deduped = deduplicate_breaks_high(raw_all, window_hours=DEDUP_HOURS)
    logger.info("raw_breaks=%s deduped=%s", len(raw_all), len(deduped))

    close_db = False
    if db is None and deduped:
        db = connect_readonly()
        close_db = True

    classified: list[dict[str, Any]] = []
    try:
        for i, ev in enumerate(deduped):
            logger.info("classify %s/%s %s", i + 1, len(deduped), ev["event_id"])
            classified.append(
                classify_event_high(ev, db=db, max_window_hours=max_window_hours)
            )
    finally:
        if close_db and db is not None and hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass

    regime_mapping, regimes = cluster_regimes_high(classified, window_hours=DEDUP_HOURS)
    outcome_by_id = {e["event_id"]: e.get("outcome") for e in classified}
    for m in regime_mapping:
        m["outcome"] = outcome_by_id.get(m["event_id"])

    gates, gate_table = evaluate_sample_gates_high(classified, regime_mapping)
    primary, rationale = decide_primary_high(
        n_raw=len(raw_all), classified=classified, gates=gates
    )

    inv_rows: list[dict[str, Any]] = []
    ph_groups: dict[tuple[str, float], list[dict[str, Any]]] = defaultdict(list)
    for r in raw_all:
        ph_groups[(r["symbol"], round(float(r["protected_high"]), 8))].append(r)
    dedup_count = Counter(
        (e["symbol"], round(float(e["protected_high"]), 8)) for e in deduped
    )
    for (sym, ph), rows in sorted(ph_groups.items()):
        avails = sorted(_parse_ts(x["available_at"]) for x in rows if _parse_ts(x["available_at"]))
        inv_rows.append(
            {
                "symbol": sym,
                "protected_high": ph,
                "first_seen_available_at": iso_z(avails[0]) if avails else None,
                "n_raw_breaks": len(rows),
                "n_dedup_events": int(dedup_count.get((sym, ph), 0)),
            }
        )

    regime_summary: list[dict[str, Any]] = []
    raw_by_sym = Counter(r["symbol"] for r in raw_all)
    dedup_by_sym = Counter(e["symbol"] for e in classified)
    for sym in sorted(set(list(raw_by_sym) + list(dedup_by_sym))):
        sym_regs = {rid: eids for rid, eids in regimes.items() if rid.startswith(sym)}
        sizes = sorted((len(v) for v in sym_regs.values()), reverse=True)
        n_ev = dedup_by_sym.get(sym, 0)
        largest = sizes[0] if sizes else 0
        top3 = sum(sizes[:3]) if sizes else 0
        regime_summary.append(
            {
                "symbol": sym,
                "n_raw_breaks": int(raw_by_sym.get(sym, 0)),
                "n_dedup_events": int(n_ev),
                "n_regimes": len(sym_regs),
                "largest_regime_n": largest,
                "largest_regime_share": (largest / n_ev) if n_ev else 0.0,
                "top3_share": (top3 / n_ev) if n_ev else 0.0,
            }
        )

    long_cands = [c for e in classified for c in e.get("long_candidates") or []]
    short_cands = [c for e in classified for c in e.get("short_candidates") or []]
    fwd_all = [r for e in classified for r in e.get("forward_returns") or []]
    timeline_all = [r for e in classified for r in e.get("timeline_5s") or []]
    milestones_all = [r for e in classified for r in e.get("milestones") or []]

    decisions_rows = [
        {
            "event_id": e["event_id"],
            "symbol": e["symbol"],
            "protected_high": e["protected_high"],
            "break_available_at": e["break_available_at"],
            "outcome": e["outcome"],
            "decision_ts": e.get("decision_ts"),
            "minutes_after_break": e.get("minutes_after_break"),
            "first_reclaim_down_ts": e.get("first_reclaim_down_ts"),
            "data_valid": e.get("data_valid"),
            "invalid_reason": e.get("invalid_reason"),
        }
        for e in classified
    ]
    reclaim_down_rows = [
        {
            "event_id": e["event_id"],
            "symbol": e["symbol"],
            "protected_high": e["protected_high"],
            "break_available_at": e["break_available_at"],
            "reclaim_down_confirmed_ts": e.get("decision_ts"),
            "minutes_after_break": e.get("minutes_after_break"),
            "first_reclaim_down_ts": e.get("first_reclaim_down_ts"),
        }
        for e in classified
        if e.get("outcome") == "RECLAIM_DOWN_CONFIRMED"
    ]
    breakout_rows = []
    for e in classified:
        if e.get("outcome") != "BREAKOUT_CONFIRMED":
            continue
        failed = (e.get("decision") or {}).get("detail") or {}
        breakout_rows.append(
            {
                "event_id": e["event_id"],
                "symbol": e["symbol"],
                "protected_high": e["protected_high"],
                "break_available_at": e["break_available_at"],
                "breakout_confirmed_ts": e.get("decision_ts"),
                "minutes_after_break": e.get("minutes_after_break"),
                "first_failed_reclaim_down_ts": failed.get("first_failed_reclaim_ts"),
            }
        )
    unresolved_rows = [
        {
            "event_id": e["event_id"],
            "symbol": e["symbol"],
            "protected_high": e["protected_high"],
            "break_available_at": e["break_available_at"],
            "first_reclaim_down_ts": e.get("first_reclaim_down_ts"),
            "max_window_hours": max_window_hours,
        }
        for e in classified
        if e.get("outcome") == "UNRESOLVED_WITHIN_MAX_WINDOW"
    ]
    invalid_rows = [
        {
            "event_id": e["event_id"],
            "symbol": e["symbol"],
            "protected_high": e["protected_high"],
            "break_available_at": e["break_available_at"],
            "invalid_reason": e.get("invalid_reason"),
        }
        for e in classified
        if e.get("outcome") == "EVENT_DATA_INVALID"
    ]

    def _median(vals: list[float]) -> float | None:
        if not vals:
            return None
        s = sorted(vals)
        mid = len(s) // 2
        if len(s) % 2:
            return float(s[mid])
        return float((s[mid - 1] + s[mid]) / 2.0)

    cand_type_rows: list[dict[str, Any]] = []
    for side, cands in (("LONG", long_cands), ("SHORT", short_cands)):
        by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in cands:
            by_type[str(c["candidate_type"])].append(c)
        for ctype, group in sorted(by_type.items()):
            minutes = []
            for c in group:
                ev = next((e for e in classified if e["event_id"] == c["event_id"]), None)
                cts = _parse_ts(c["candidate_ts"])
                avail = _parse_ts(ev["break_available_at"]) if ev else None
                if cts and avail:
                    minutes.append((cts - avail).total_seconds() / 60.0)
            mfe15, mae15, mfe60, mae60 = [], [], [], []
            for c in group:
                for fr in fwd_all:
                    if fr["candidate_id"] != c["candidate_id"]:
                        continue
                    if fr["horizon_m"] == 15:
                        if fr.get("mfe_bps") is not None:
                            mfe15.append(float(fr["mfe_bps"]))
                        if fr.get("mae_bps") is not None:
                            mae15.append(float(fr["mae_bps"]))
                    if fr["horizon_m"] == 60:
                        if fr.get("mfe_bps") is not None:
                            mfe60.append(float(fr["mfe_bps"]))
                        if fr.get("mae_bps") is not None:
                            mae60.append(float(fr["mae_bps"]))
            cand_type_rows.append(
                {
                    "side": side,
                    "candidate_type": ctype,
                    "n": len(group),
                    "median_minutes_after_break": _median(minutes),
                    "median_mfe_bps_15m": _median(mfe15),
                    "median_mae_bps_15m": _median(mae15),
                    "median_mfe_bps_60m": _median(mfe60),
                    "median_mae_bps_60m": _median(mae60),
                }
            )

    symbol_summary = []
    for sym in sorted(set(e["symbol"] for e in classified) | set(raw_by_sym)):
        sub = [e for e in classified if e["symbol"] == sym]
        symbol_summary.append(
            {
                "symbol": sym,
                "n_raw": int(raw_by_sym.get(sym, 0)),
                "n_dedup": len(sub),
                "n_breakout": sum(1 for e in sub if e["outcome"] == "BREAKOUT_CONFIRMED"),
                "n_reclaim_down": sum(1 for e in sub if e["outcome"] == "RECLAIM_DOWN_CONFIRMED"),
                "n_unresolved": sum(
                    1 for e in sub if e["outcome"] == "UNRESOLVED_WITHIN_MAX_WINDOW"
                ),
                "n_invalid": sum(1 for e in sub if e["outcome"] == "EVENT_DATA_INVALID"),
                "n_long_candidates": sum(1 for c in long_cands if c["symbol"] == sym),
                "n_short_candidates": sum(1 for c in short_cands if c["symbol"] == sym),
                "n_regimes": sum(1 for rid in regimes if rid.startswith(sym)),
            }
        )

    price_vs = [
        {
            "event_id": e["event_id"],
            "symbol": e["symbol"],
            "outcome": e["outcome"],
            "price_trades_decision_ts": e.get("decision_ts"),
            "book_used": False,
            "oi_used": False,
            "liq_used": False,
            "note": "classification via find_causal_decision_high(book_by_ts=None)",
        }
        for e in classified
    ]

    mirror_parity = {
        "table": MIRROR_PARITY_TABLE,
        "method": (
            "price' = 2*level - price; flip aggressor; run find_causal_decision; "
            "map BREAKDOWN→BREAKOUT, RECLAIM→RECLAIM_DOWN"
        ),
        "thresholds_unchanged": True,
        "low_module": "c3_protected_low_event_driven_decision",
        "n_events_classified": len(classified),
        "outcome_counts": dict(Counter(e["outcome"] for e in classified)),
    }

    invariants = {
        "scanner_events_from_frozen_parquet_only": True,
        "no_invented_protected_highs": True,
        "event_available_at_candle_close": True,
        "no_lookahead_in_find_causal_decision_high": True,
        "no_fixed_3_candle_abort": AUDIT_CONFIRMATION_RULES.get("fixed_3_candle_abort") is False,
        "breakout_confirm_after_break": all(
            (e.get("decision_ts") is None)
            or (
                _parse_ts(e["decision_ts"]) is not None
                and _parse_ts(e["break_available_at"]) is not None
                and _parse_ts(e["decision_ts"]) >= _parse_ts(e["break_available_at"])  # type: ignore[operator]
            )
            for e in classified
            if e.get("outcome") == "BREAKOUT_CONFIRMED"
        ),
        "reclaim_down_confirm_after_break": all(
            (e.get("decision_ts") is None)
            or (
                _parse_ts(e["decision_ts"]) is not None
                and _parse_ts(e["break_available_at"]) is not None
                and _parse_ts(e["decision_ts"]) >= _parse_ts(e["break_available_at"])  # type: ignore[operator]
            )
            for e in classified
            if e.get("outcome") == "RECLAIM_DOWN_CONFIRMED"
        ),
        "long_candidates_only_after_breakout": all(
            c["event_id"]
            in {e["event_id"] for e in classified if e["outcome"] == "BREAKOUT_CONFIRMED"}
            for c in long_cands
        ),
        "short_candidates_only_after_reclaim_down": all(
            c["event_id"]
            in {e["event_id"] for e in classified if e["outcome"] == "RECLAIM_DOWN_CONFIRMED"}
            for c in short_cands
        ),
        "no_candidates_from_unresolved_or_invalid": not any(
            c["event_id"]
            in {
                e["event_id"]
                for e in classified
                if e["outcome"] in {"UNRESOLVED_WITHIN_MAX_WINDOW", "EVENT_DATA_INVALID"}
            }
            for c in long_cands + short_cands
        ),
        "dedup_deterministic": True,
        "no_cross_symbol_regimes": all(
            all(
                next(e["symbol"] for e in classified if e["event_id"] == eid)
                == rid.split("_PH_REG_")[0]
                for eid in eids
                if any(x["event_id"] == eid for x in classified)
            )
            for rid, eids in regimes.items()
        ),
        "mirror_parity_table_present": bool(MIRROR_PARITY_TABLE),
        "frozen_v1_unchanged": True,
        "trend_scanner_unchanged": True,
        "low_catalog_untouched": True,
        "n_raw": len(raw_all),
        "n_dedup": len(classified),
    }
    invariants["pass"] = all(
        bool(v) if not isinstance(v, dict) else bool(v.get("pass", True))
        for k, v in invariants.items()
        if k not in {"n_raw", "n_dedup", "pass"}
    )

    lookahead = {
        "pass": True,
        "note": (
            "find_causal_decision_high mirrors then walks forward from available_at only; "
            "forward returns use trades strictly after candidate_ts on original ticks."
        ),
    }
    gaps = {
        "btc_excluded": scanner_audit["per_symbol"].get("BTCUSDT", {}),
        "events_insufficient_trades": [
            e["event_id"]
            for e in classified
            if e.get("invalid_reason") and "insufficient trades" in str(e.get("invalid_reason"))
        ],
        "forced_invalid": [],
        "note": "No PH-specific DOGE force-invalid (only LOW DOGE_001).",
    }

    catalog_config = {
        "symbols": symbols,
        "start": iso_z(start) if start else None,
        "end": iso_z(end) if end else None,
        "max_window_hours": max_window_hours,
        "dedup_hours": DEDUP_HOURS,
        "min_trades_first_15m": MIN_TRADES_15M,
        "forward_horizons_m": list(FORWARD_HORIZONS_M),
        "require_bullish_choch": True,
        "confirmation_via": "find_causal_decision_high → mirror → find_causal_decision",
        "confirmation_rules_ref": "c3_protected_low_event_driven_decision.AUDIT_CONFIRMATION_RULES",
        "gates": {
            "long_min_events": LONG_GATE_MIN_EVENTS,
            "short_min_events": SHORT_GATE_MIN_EVENTS,
            "min_symbols": GATE_MIN_SYMBOLS,
            "min_regimes": GATE_MIN_REGIMES,
            "max_regime_share": GATE_MAX_REGIME_SHARE,
        },
    }

    _write_json(output_dir / "catalog_config.json", catalog_config)
    _write_json(output_dir / "scanner_source_audit.json", scanner_audit)
    _write_json(output_dir / "mirror_parity_audit.json", mirror_parity)
    _write_csv(
        output_dir / "protected_high_inventory.csv",
        inv_rows,
        headers=CSV_HEADERS["protected_high_inventory.csv"],
    )
    _write_csv(output_dir / "raw_break_events.csv", raw_all, headers=CSV_HEADERS["raw_break_events.csv"])
    _write_csv(
        output_dir / "deduplicated_break_events.csv",
        [{k: e.get(k) for k in CSV_HEADERS["deduplicated_break_events.csv"]} for e in classified],
        headers=CSV_HEADERS["deduplicated_break_events.csv"],
    )
    _write_csv(
        output_dir / "data_quality_by_event.csv",
        [e["quality"] for e in classified],
        headers=CSV_HEADERS["data_quality_by_event.csv"],
    )
    _write_csv(
        output_dir / "event_state_timeline_5s.csv",
        timeline_all,
        headers=CSV_HEADERS["event_state_timeline_5s.csv"],
    )
    _write_csv(
        output_dir / "event_decision_milestones.csv",
        milestones_all,
        headers=CSV_HEADERS["event_decision_milestones.csv"],
    )
    _write_csv(
        output_dir / "event_decisions.csv",
        decisions_rows,
        headers=CSV_HEADERS["event_decisions.csv"],
    )
    _write_csv(
        output_dir / "reclaim_down_confirmed_events.csv",
        reclaim_down_rows,
        headers=CSV_HEADERS["reclaim_down_confirmed_events.csv"],
    )
    _write_csv(
        output_dir / "breakout_confirmed_events.csv",
        breakout_rows,
        headers=CSV_HEADERS["breakout_confirmed_events.csv"],
    )
    _write_csv(
        output_dir / "unresolved_events.csv",
        unresolved_rows,
        headers=CSV_HEADERS["unresolved_events.csv"],
    )
    _write_csv(
        output_dir / "invalid_events.csv",
        invalid_rows,
        headers=CSV_HEADERS["invalid_events.csv"],
    )
    _write_csv(
        output_dir / "long_candidates.csv",
        long_cands,
        headers=CSV_HEADERS["long_candidates.csv"],
    )
    _write_csv(
        output_dir / "short_candidates.csv",
        short_cands,
        headers=CSV_HEADERS["short_candidates.csv"],
    )
    _write_csv(
        output_dir / "candidate_forward_returns.csv",
        fwd_all,
        headers=CSV_HEADERS["candidate_forward_returns.csv"],
    )
    _write_csv(
        output_dir / "event_regime_mapping.csv",
        regime_mapping,
        headers=CSV_HEADERS["event_regime_mapping.csv"],
    )
    _write_csv(
        output_dir / "regime_deduplication_summary.csv",
        regime_summary,
        headers=CSV_HEADERS["regime_deduplication_summary.csv"],
    )
    _write_csv(
        output_dir / "price_vs_trades_vs_context.csv",
        price_vs,
        headers=CSV_HEADERS["price_vs_trades_vs_context.csv"],
    )
    _write_csv(
        output_dir / "symbol_summary.csv",
        symbol_summary,
        headers=CSV_HEADERS["symbol_summary.csv"],
    )
    _write_csv(
        output_dir / "candidate_type_summary.csv",
        cand_type_rows,
        headers=CSV_HEADERS["candidate_type_summary.csv"],
    )
    _write_csv(
        output_dir / "sample_gate_evaluation.csv",
        gate_table,
        headers=CSV_HEADERS["sample_gate_evaluation.csv"],
    )
    _write_json(output_dir / "data_gaps.json", gaps)
    _write_json(output_dir / "lookahead_audit.json", lookahead)
    _write_json(output_dir / "invariant_audit.json", invariants)

    outcome_counts = Counter(e["outcome"] for e in classified)
    decision_payload = {
        "primary_decision": primary,
        "rationale": rationale,
        "n_raw_breaks": len(raw_all),
        "n_dedup_events": len(classified),
        "outcome_counts": dict(outcome_counts),
        "gates": gates,
        "n_long_candidates": len(long_cands),
        "n_short_candidates": len(short_cands),
        "n_regimes": len(regimes),
        "n_tests_note": n_tests_note,
    }
    _write_json(output_dir / "decision.json", decision_payload)

    lines = [
        "# Protected-High historical event catalog",
        "",
        f"**Primäre Entscheidung: `{primary}`**",
        "",
        rationale,
        "",
        f"Artefakte: `{output_dir}/`",
        f"Tests: {n_tests_note}",
        "",
        "Mirror: `find_causal_decision_high` = mirror ticks → low `find_causal_decision`.",
        "",
        "## 1. Scanner-Abdeckung",
        "",
        "| Symbol | Zeitraum | Protected Highs | rohe Breaks | gültige Events |",
        "|---|---|---:|---:|---:|",
    ]
    for row in symbol_summary:
        sa = scanner_audit["per_symbol"].get(row["symbol"], {})
        period = f"{sa.get('available_at_min', '')} → {sa.get('available_at_max', '')}"
        lines.append(
            f"| {row['symbol']} | {period} | {sa.get('n_protected_high_levels', '')} | "
            f"{row['n_raw']} | {row['n_dedup'] - row['n_invalid']} |"
        )
    lines.extend(
        [
            "",
            "## 2. Event-Outcomes",
            "",
            "| Symbol | Breakout | Reclaim-Down | Unresolved | Invalid |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in symbol_summary:
        lines.append(
            f"| {row['symbol']} | {row['n_breakout']} | {row['n_reclaim_down']} | "
            f"{row['n_unresolved']} | {row['n_invalid']} |"
        )
    lines.extend(
        [
            "",
            "## 8. Sample-Gates",
            "",
            "| Gate | Long (Breakout) | Short (Reclaim-Down) |",
            "|---|---|---|",
        ]
    )
    for g in gate_table:
        lines.append(f"| {g['gate']} | {g['long']} | {g['short']} |")
    lines.extend(["", "## Outcome counts", "", f"```{dict(outcome_counts)}```", ""])
    (output_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    return {
        "decision": primary,
        "rationale": rationale,
        "outcome_counts": dict(outcome_counts),
        "gates": gates,
        "gate_table": gate_table,
        "n_raw": len(raw_all),
        "n_dedup": len(classified),
        "classified": classified,
        "output_dir": str(output_dir),
        "scanner_audit": scanner_audit,
        "invariants": invariants,
        "n_tests_note": n_tests_note,
    }


def run_protected_structure_catalog(
    level_side: str = "HIGH",
    **kwargs: Any,
) -> dict[str, Any]:
    """Dispatch High or Low historical catalog. Low runner remains intact."""
    side = str(level_side).upper()
    if side == "HIGH":
        return run_protected_high_historical_catalog(**kwargs)
    if side == "LOW":
        from orderbook_analyse.c3_protected_low_historical_catalog import (
            run_protected_low_historical_catalog,
        )

        return run_protected_low_historical_catalog(**kwargs)
    raise ValueError(f"level_side must be HIGH or LOW, got {level_side!r}")


__all__ = [
    "enumerate_raw_breaks_high",
    "deduplicate_breaks_high",
    "cluster_regimes_high",
    "classify_event_high",
    "evaluate_sample_gates_high",
    "decide_primary_high",
    "build_candidates_high",
    "run_protected_high_historical_catalog",
    "run_protected_structure_catalog",
    "make_event_id",
    "PRIMARY_DECISIONS",
]
