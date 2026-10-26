"""BT-D46/D47 chronological short backtester → research chart (box-only, read-only)."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

STRATEGY_ID = "chronological_short_backtester_bt_d46"
DISPLAY_NAME = "Chronological Short Backtester · BT-D46"
BACKTESTER_SOURCE = "bt_d46_chronological_chart_v1"
BOX_PREFIX = "btd46-box-"
# Legacy prefixes — cleared on load so old marker/link layers disappear.
MARKER_PREFIX = "btd46-marker-"
LABEL_PREFIX = "btd46-label-"
LINK_PREFIX = "btd46-link-"
MIN_BOX_WIDTH_SEC = 300
CONTRACT_VERSION = "bt_d47_integrated_trade_forensics_overlay_v1"
PILOT_RUN_ID = "phase_bt_d47_trade_forensics_v1"
EXECUTION_MODEL_LABEL = "BT-D45 Model 1 (CAUSAL_V2_LIMIT_TOUCH_INTRABAR / MARKETABLE_AT_OPEN)"
FILL_DISCLAIMER = "SIMULATED_TOUCH_FILL_NOT_EXCHANGE_CONFIRMED"

EXPECTED_COUNTS = {
    "SIMULATED_ENTRY": 19,
    "SIMULATED_EXIT_SL": 14,
    "SIMULATED_EXIT_TP": 4,
    "SIMULATED_POSITION_OPEN": 1,
}


def _pools_ob_root() -> Path:
    return resolve_pools_ob_repo()


def _overlay_path() -> Path:
    return (
        _pools_ob_root()
        / "results"
        / "phase_bt_d47_trade_forensics_v1"
        / "chart_overlay_events.json"
    )


def _audit_path() -> Path:
    return (
        _pools_ob_root()
        / "results"
        / "phase_bt_d47_trade_forensics_v1"
        / "positions_audit.csv"
    )


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _to_unix(s: str) -> int:
    return int(_parse_utc(s).timestamp())


def _price_float(v: Any) -> float:
    return float(v)


def load_overlay_document() -> tuple[dict[str, Any] | None, list[str]]:
    path = _overlay_path()
    if not path.is_file():
        return None, ["overlay_not_found"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None, ["overlay_read_error"]
    return doc, []


def load_positions_audit() -> tuple[dict[str, dict[str, Any]], list[str]]:
    path = _audit_path()
    if not path.is_file():
        return {}, ["audit_not_found"]
    rows: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            pid = str(row.get("position_id") or "").strip()
            if pid:
                rows[pid] = dict(row)
    if len(rows) != EXPECTED_COUNTS["SIMULATED_ENTRY"]:
        return rows, ["audit_position_count_mismatch"]
    return rows, []


def validate_overlay_document(doc: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if str(doc.get("contract_version") or "") != CONTRACT_VERSION:
        errs.append("contract_version_mismatch")
    if doc.get("scanner_boxes_included") is not False:
        errs.append("scanner_boxes_must_be_false")
    if str(doc.get("source") or "") != "bt_d46_integrated_short_backtester_v1":
        errs.append("source_provenance_mismatch")
    records = list(doc.get("records") or [])
    if len(records) != sum(EXPECTED_COUNTS.values()):
        errs.append("record_count_mismatch")
    by_type: dict[str, int] = {}
    position_ids: set[str] = set()
    for rec in records:
        et = str(rec.get("event_type") or "")
        by_type[et] = by_type.get(et, 0) + 1
        pid = str(rec.get("position_id") or "")
        if pid:
            position_ids.add(pid)
        if not rec.get("time_utc"):
            errs.append("missing_time_utc")
            break
    for et, n in EXPECTED_COUNTS.items():
        if by_type.get(et) != n:
            errs.append(f"event_type_count:{et}")
    if len(position_ids) != EXPECTED_COUNTS["SIMULATED_ENTRY"]:
        errs.append("unique_position_id_count")
    entry_pids = {
        str(r.get("position_id"))
        for r in records
        if r.get("event_type") == "SIMULATED_ENTRY" and r.get("position_id")
    }
    if entry_pids != position_ids:
        errs.append("entry_position_id_set_mismatch")
    return errs


def build_summary(doc: dict[str, Any]) -> dict[str, Any]:
    records = doc.get("records") or []
    times = [r["time_utc"] for r in records if r.get("time_utc")]
    return {
        "run_id": PILOT_RUN_ID,
        "strategy_id": STRATEGY_ID,
        "symbol": str(doc.get("symbol") or "DOGEUSDT").upper(),
        "marker_count_total": len(records),
        "trade_count": EXPECTED_COUNTS["SIMULATED_ENTRY"],
        "event_counts": {
            et: sum(1 for r in records if r.get("event_type") == et) for et in EXPECTED_COUNTS
        },
        "fill_disclaimer": doc.get("fill_disclaimer") or FILL_DISCLAIMER,
        "execution_model": EXECUTION_MODEL_LABEL,
        "scanner_boxes_included": False,
        "display_mode": "box_only_short_position_v1",
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "overlay_path": str(_overlay_path().relative_to(_pools_ob_root())),
        "payload_kind": "bt_d46_chronological_chart_integration_v1",
    }


def filter_records_causal(records: list[dict[str, Any]], as_of_unix: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        ts = _to_unix(str(rec["time_utc"]))
        if ts <= int(as_of_unix):
            out.append(rec)
    return out


def _records_by_position(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_pid: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        pid = str(rec.get("position_id") or "")
        if pid:
            by_pid.setdefault(pid, []).append(rec)
    return by_pid


def _same_bar_flags_by_position(records: list[dict[str, Any]]) -> dict[str, bool]:
    by_pid = _records_by_position(records)
    flags: dict[str, bool] = {}
    for pid, evs in by_pid.items():
        times = {str(e.get("time_utc")) for e in evs}
        flags[pid] = len(times) == 1 and len(evs) > 1
    return flags


def _trade_end_time(
    pid: str,
    causal: list[dict[str, Any]],
    audit: dict[str, Any],
    *,
    as_of_unix: int,
) -> tuple[datetime, str]:
    evs = [e for e in causal if str(e.get("position_id")) == pid]
    entry = next((e for e in evs if e.get("event_type") == "SIMULATED_ENTRY"), None)
    if not entry:
        raise ValueError("no entry")
    entry_ts = _parse_utc(str(entry["time_utc"]))
    exit_sl = next((e for e in evs if e.get("event_type") == "SIMULATED_EXIT_SL"), None)
    exit_tp = next((e for e in evs if e.get("event_type") == "SIMULATED_EXIT_TP"), None)
    open_ev = next((e for e in evs if e.get("event_type") == "SIMULATED_POSITION_OPEN"), None)

    if exit_sl:
        return _parse_utc(str(exit_sl["time_utc"])), "EXIT_SL"
    if exit_tp:
        return _parse_utc(str(exit_tp["time_utc"])), "EXIT_TP"
    if open_ev:
        return datetime.fromtimestamp(int(as_of_unix), tz=timezone.utc), "OPEN"
    # Entry known at as_of but exit not yet revealed — extend to as_of.
    return datetime.fromtimestamp(int(as_of_unix), tz=timezone.utc), "OPEN_INFERRED"


def trade_to_position_spec(
    audit: dict[str, Any],
    *,
    as_of_unix: int,
    causal: list[dict[str, Any]],
    same_bar_unresolved: bool,
) -> dict[str, Any] | None:
    pid = str(audit.get("position_id") or "")
    if not pid:
        return None
    evs = [e for e in causal if str(e.get("position_id")) == pid]
    if not any(e.get("event_type") == "SIMULATED_ENTRY" for e in evs):
        return None

    entry_bar = audit.get("entry_bar_utc")
    if not entry_bar:
        return None
    entry_ts = _parse_utc(str(entry_bar))
    if _to_unix(str(audit.get("entry_bar_utc"))) > int(as_of_unix):
        return None

    try:
        end_ts, status = _trade_end_time(pid, causal, audit, as_of_unix=as_of_unix)
    except ValueError:
        return None

    if end_ts < entry_ts:
        end_ts = entry_ts
    if (end_ts - entry_ts).total_seconds() < MIN_BOX_WIDTH_SEC:
        end_ts = entry_ts + timedelta(seconds=MIN_BOX_WIDTH_SEC)

    entry = _price_float(audit.get("entry_price"))
    sl = _price_float(audit.get("frozen_sl"))
    tp = _price_float(audit.get("frozen_tp"))

    return {
        "drawing_id": f"{BOX_PREFIX}{pid}",
        "drawing_type": "short_position",
        "position_id": pid,
        "order_id": audit.get("order_id"),
        "start": entry_ts,
        "end": end_ts,
        "entry": entry,
        "stop": sl,
        "target": tp,
        "status": status,
        "same_bar_order_unresolved": same_bar_unresolved,
        "metadata": {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "position_id": pid,
            "order_id": audit.get("order_id"),
            "fill_disclaimer": FILL_DISCLAIMER,
            "simulated_only": True,
            "display_mode": "box_only_short_position_v1",
            "trade_status": status,
            "same_bar_order_unresolved": same_bar_unresolved,
            "execution_model": EXECUTION_MODEL_LABEL,
            "data_resolution": "5m_bar_close_utc",
            "entry_bar_utc": audit.get("entry_bar_utc"),
            "exit_bar_utc": audit.get("exit_bar_utc"),
            "exit_reason": audit.get("exit_reason"),
        },
    }


def filter_position_specs_viewport(
    specs: list[dict[str, Any]],
    *,
    view_start_unix: int,
    view_end_unix: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for spec in specs:
        t0 = int(spec["start"].timestamp())
        t1 = int(spec["end"].timestamp())
        if t1 >= int(view_start_unix) and t0 <= int(view_end_unix):
            out.append(spec)
    return out


def build_viewport_draw_specs(
    doc: dict[str, Any],
    audit_by_pid: dict[str, dict[str, Any]],
    *,
    view_start_unix: int,
    view_end_unix: int,
    as_of_unix: int,
) -> dict[str, Any]:
    all_records = list(doc.get("records") or [])
    causal = filter_records_causal(all_records, as_of_unix)
    same_bar_flags = _same_bar_flags_by_position(causal)

    all_specs: list[dict[str, Any]] = []
    for pid, audit in sorted(audit_by_pid.items(), key=lambda x: str(x[1].get("entry_bar_utc") or "")):
        spec = trade_to_position_spec(
            audit,
            as_of_unix=as_of_unix,
            causal=causal,
            same_bar_unresolved=same_bar_flags.get(pid, False),
        )
        if spec:
            all_specs.append(spec)

    visible = filter_position_specs_viewport(
        all_specs, view_start_unix=view_start_unix, view_end_unix=view_end_unix
    )

    return {
        "position_specs": visible,
        "marker_specs": [],
        "link_specs": [],
        "counts": {
            "causal_total": len(causal),
            "trades_causal": len(all_specs),
            "viewport_boxes": len(visible),
            "viewport_markers": 0,
            "viewport_links": 0,
            "catalog_total": len(all_records),
        },
        "as_of_utc": _iso(datetime.fromtimestamp(int(as_of_unix), tz=timezone.utc)),
        "same_bar_unresolved_positions": sorted(
            [pid for pid, v in same_bar_flags.items() if v]
        ),
        "display_mode": "box_only_short_position_v1",
    }


def position_detail_panel(
    position_id: str,
    audit_by_pid: dict[str, dict[str, Any]],
    doc: dict[str, Any],
) -> dict[str, Any] | None:
    pid = str(position_id or "").strip()
    if not pid:
        return None
    audit = audit_by_pid.get(pid)
    events = [r for r in doc.get("records") or [] if str(r.get("position_id")) == pid]
    if not audit and not events:
        return None
    same_bar = _same_bar_flags_by_position(events).get(pid, False)
    return {
        "position_id": pid,
        "order_id": (audit or {}).get("order_id") or (events[0].get("order_id") if events else None),
        "pool_lower": (audit or {}).get("pool_lower"),
        "pool_upper": (audit or {}).get("pool_upper"),
        "origin_set_key": (audit or {}).get("origin_set_key"),
        "decision_close_utc": (audit or {}).get("decision_close_utc"),
        "pending_activation_utc": (audit or {}).get("pending_activation_utc"),
        "entry_bar_utc": (audit or {}).get("entry_bar_utc"),
        "entry_price": (audit or {}).get("entry_price"),
        "frozen_sl": (audit or {}).get("frozen_sl"),
        "frozen_tp": (audit or {}).get("frozen_tp"),
        "exit_reason": (audit or {}).get("exit_reason"),
        "exit_bar_utc": (audit or {}).get("exit_bar_utc"),
        "execution_assumption": EXECUTION_MODEL_LABEL,
        "fill_disclaimer": FILL_DISCLAIMER,
        "data_resolution_limitation": "5m bars only — bar-close UTC timestamps; no invented sub-bar order",
        "same_bar_order_unresolved": same_bar,
        "events": events,
        "scanner_boxes_included": False,
        "provenance": {
            "overlay": str(_overlay_path().relative_to(_pools_ob_root())),
            "backtester": "bt_d46_integrated_short_backtester_v1",
            "forensics": PILOT_RUN_ID,
        },
    }
