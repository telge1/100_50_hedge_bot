"""Load the 54 CH-covered events and enrich from existing artifacts only."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from research.orderbook.ch_break_reclaim_microstructure_audit.features import (
    direction_context,
    ensure_utc,
    iso_z,
)
from research.orderbook.ch_break_reclaim_microstructure_audit.outcomes import map_outcome_label

DEFAULT_COVERAGE_CSV = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/event_ob_coverage_audit_20260808/event_coverage.csv"
)
OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"nan", "none", "nat"}:
        return None
    return ensure_utc(datetime.fromisoformat(s.replace("Z", "+00:00")))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _index_by(rows: list[dict[str, str]], key: str) -> dict[str, dict[str, str]]:
    return {r[key]: r for r in rows if r.get(key)}


def load_artifact_indices(oa_root: Path = OA_ROOT) -> dict[str, Any]:
    pl = oa_root / "results/c3_protected_low_historical_event_catalog"
    ph = oa_root / "results/c3_protected_high_historical_event_catalog"
    inv = oa_root / "results/apt_1h_4h_protected_level_event_inventory"
    return {
        "pl_decisions": _index_by(_read_csv(pl / "event_decisions.csv"), "event_id"),
        "pl_dedup": _index_by(_read_csv(pl / "deduplicated_break_events.csv"), "event_id"),
        "pl_reclaim": _index_by(_read_csv(pl / "reclaim_confirmed_events.csv"), "event_id"),
        "pl_breakdown": _index_by(_read_csv(pl / "breakdown_confirmed_events.csv"), "event_id"),
        "ph_decisions": _index_by(_read_csv(ph / "event_decisions.csv"), "event_id"),
        "ph_dedup": _index_by(_read_csv(ph / "deduplicated_break_events.csv"), "event_id"),
        "ph_reclaim": _index_by(_read_csv(ph / "reclaim_down_confirmed_events.csv"), "event_id"),
        "ph_breakout": _index_by(_read_csv(ph / "breakout_confirmed_events.csv"), "event_id"),
        "apt_events": _index_by(_read_csv(inv / "protected_level_events.csv"), "event_id"),
        "apt_queue": _index_by(_read_csv(inv / "audit_queue.csv"), "primary_event_id"),
    }


def load_ch_covered_events(
    coverage_csv: Path = DEFAULT_COVERAGE_CSV,
    *,
    oa_root: Path = OA_ROOT,
) -> list[dict[str, Any]]:
    """Exactly the coverage rows with CLICKHOUSE_OB_FULL (and trades_pm5 True)."""
    rows = _read_csv(coverage_csv)
    covered = [
        r
        for r in rows
        if r.get("coverage") == "CLICKHOUSE_OB_FULL"
        and str(r.get("trades_pm5", "")).lower() in {"true", "1"}
    ]
    arts = load_artifact_indices(oa_root)
    out: list[dict[str, Any]] = []
    for r in covered:
        out.append(enrich_event(r, arts))
    if len(out) != 54:
        # soft check — still return what we have, caller may warn
        pass
    return out


def enrich_event(coverage_row: dict[str, str], arts: dict[str, Any]) -> dict[str, Any]:
    eid = coverage_row["event_id"]
    source = coverage_row["source"]
    symbol = coverage_row["symbol"]
    level_type = coverage_row["level_type"]
    level = float(coverage_row["level"])
    event_ts = parse_ts(coverage_row["event_ts"])
    raw_outcome = coverage_row.get("outcome") or ""

    first_touch = None
    first_break = event_ts
    reclaim_ts = None
    candle_open = None
    scanner_type = source
    timeframe = coverage_row.get("timeframe") or ""
    minutes_after = None
    reclaim_minutes = None
    touch_src = "coverage_event_ts_as_break"
    break_src = "coverage_event_ts"
    reclaim_src = None

    if source == "apt_1h_4h_audit_queue":
        ev = arts["apt_events"].get(eid, {})
        q = arts["apt_queue"].get(eid, {})
        first_touch = parse_ts(ev.get("first_touch"))
        first_break = parse_ts(ev.get("first_break_ts") or q.get("first_break_ts")) or event_ts
        reclaim_ts = parse_ts(ev.get("first_trade_reclaim") or ev.get("first_5m_close_reclaim"))
        candle_open = parse_ts(ev.get("confirmed_at"))
        timeframe = ev.get("timeframe") or q.get("primary_timeframe") or timeframe
        raw_outcome = ev.get("final_event_class") or q.get("final_event_class") or raw_outcome
        touch_src = "apt_inventory_first_touch" if first_touch else "missing_will_derive"
        break_src = "apt_inventory_first_break_ts"
        reclaim_src = "apt_inventory_reclaim" if reclaim_ts else None
        if first_break and reclaim_ts:
            reclaim_minutes = (reclaim_ts - first_break).total_seconds() / 60.0
    elif source == "c3_pl":
        d = arts["pl_decisions"].get(eid, {})
        ded = arts["pl_dedup"].get(eid, {})
        rc = arts["pl_reclaim"].get(eid, {})
        bd = arts["pl_breakdown"].get(eid, {})
        candle_open = parse_ts(ded.get("break_candle_open"))
        first_break = parse_ts(d.get("break_available_at") or coverage_row["event_ts"])
        reclaim_ts = parse_ts(d.get("first_reclaim_ts") or rc.get("first_reclaim_ts"))
        raw_outcome = d.get("outcome") or raw_outcome
        try:
            minutes_after = float(d["minutes_after_break"]) if d.get("minutes_after_break") else None
        except (TypeError, ValueError):
            minutes_after = None
        if reclaim_ts and first_break:
            reclaim_minutes = (reclaim_ts - first_break).total_seconds() / 60.0
        elif minutes_after is not None and raw_outcome.startswith("RECLAIM"):
            reclaim_minutes = minutes_after
        touch_src = "missing_will_derive"
        break_src = "c3_break_available_at"
        reclaim_src = "c3_first_reclaim_ts" if reclaim_ts else None
        scanner_type = "c3_protected_low"
    elif source == "c3_ph":
        d = arts["ph_decisions"].get(eid, {})
        ded = arts["ph_dedup"].get(eid, {})
        rc = arts["ph_reclaim"].get(eid, {})
        candle_open = parse_ts(ded.get("break_candle_open") or ded.get("candle_open_ts"))
        first_break = parse_ts(d.get("break_available_at") or coverage_row["event_ts"])
        reclaim_ts = parse_ts(
            d.get("first_reclaim_down_ts") or rc.get("first_reclaim_down_ts") or rc.get("first_reclaim_ts")
        )
        raw_outcome = d.get("outcome") or raw_outcome
        try:
            minutes_after = float(d["minutes_after_break"]) if d.get("minutes_after_break") else None
        except (TypeError, ValueError):
            minutes_after = None
        if reclaim_ts and first_break:
            reclaim_minutes = (reclaim_ts - first_break).total_seconds() / 60.0
        elif minutes_after is not None and "RECLAIM" in raw_outcome:
            reclaim_minutes = minutes_after
        touch_src = "missing_will_derive"
        break_src = "c3_break_available_at"
        reclaim_src = "c3_first_reclaim_down_ts" if reclaim_ts else None
        scanner_type = "c3_protected_high"

    ctx = direction_context(level_type)
    mapped = map_outcome_label(
        raw_outcome,
        minutes_after_break=minutes_after,
        reclaim_minutes=reclaim_minutes,
    )

    return {
        "event_id": eid,
        "symbol": symbol,
        "source": source,
        "scanner_type": scanner_type,
        "level_type": level_type,
        "level": level,
        "break_direction": ctx.break_direction,
        "support_side": ctx.support_side,
        "break_aggressor": ctx.break_aggressor,
        "timeframe": timeframe,
        "event_ts": event_ts,
        "candle_open_ts": candle_open,
        "first_touch_ts": first_touch,
        "first_break_ts": first_break,
        "reclaim_ts": reclaim_ts,
        "first_touch_source": touch_src,
        "first_break_source": break_src,
        "reclaim_source": reclaim_src,
        "raw_outcome": raw_outcome,
        **mapped,
        "coverage": coverage_row.get("coverage"),
        "trades_pm5": coverage_row.get("trades_pm5"),
        # ISO helpers for CSV
        "event_ts_iso": iso_z(event_ts),
        "candle_open_ts_iso": iso_z(candle_open),
        "first_touch_ts_iso": iso_z(first_touch),
        "first_break_ts_iso": iso_z(first_break),
        "reclaim_ts_iso": iso_z(reclaim_ts),
    }


def outcomes_table(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = [
        "event_id",
        "symbol",
        "source",
        "scanner_type",
        "level_type",
        "level",
        "break_direction",
        "timeframe",
        "event_ts_iso",
        "candle_open_ts_iso",
        "first_touch_ts_iso",
        "first_break_ts_iso",
        "reclaim_ts_iso",
        "first_touch_source",
        "first_break_source",
        "reclaim_source",
        "raw_outcome",
        "outcome_label",
        "outcome_map_reason",
        "reclaim_minutes_used",
        "uses_future_info",
        "note",
    ]
    return [{k: e.get(k) for k in keys} for e in events]
