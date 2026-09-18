"""Read-only validation of frozen MP batch artifacts."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED_TRADE_SIDE = {
    "UPPER": {"ABSORB": "SHORT", "FAILED_BREAK": "SHORT", "TRUE_BREAK": "LONG"},
    "LOWER": {"ABSORB": "LONG", "FAILED_BREAK": "LONG", "TRUE_BREAK": "SHORT"},
}


def _truthy(v: Any) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y"}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def audit_batch_input(batch_dir: Path) -> dict[str, Any]:
    batch_dir = Path(batch_dir)
    report_path = batch_dir / "BATCH_REPORT.md"
    events_path = batch_dir / "events_all.csv"
    outcomes_path = batch_dir / "outcomes_all.csv"
    episodes_path = batch_dir / "episodes.csv"
    windows_path = batch_dir / "batch_windows.csv"
    sel_path = batch_dir / "selection_policies.csv"
    manifest_path = batch_dir / "run_manifest.json"
    params_path = batch_dir / "parameters.json"

    missing = [
        p.name
        for p in (
            report_path,
            events_path,
            outcomes_path,
            episodes_path,
            windows_path,
            sel_path,
            manifest_path,
            params_path,
        )
        if not p.exists()
    ]
    if missing:
        return {
            "ok": False,
            "verdict": "OB_ENRICHMENT_BLOCKED_INPUT",
            "reason": f"missing_artifacts:{missing}",
        }

    report = report_path.read_text(encoding="utf-8")
    if "MP_BATCH_SUCCESS" not in report:
        return {
            "ok": False,
            "verdict": "OB_ENRICHMENT_BLOCKED_INPUT",
            "reason": "BATCH_REPORT missing MP_BATCH_SUCCESS",
        }

    events = _read_csv(events_path)
    outcomes = _read_csv(outcomes_path)
    episodes = _read_csv(episodes_path)
    windows = _read_csv(windows_path)
    selection = _read_csv(sel_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    params = json.loads(params_path.read_text(encoding="utf-8"))

    event_ids = [e["event_id"] for e in events]
    unique_ids = set(event_ids)
    n_events = len(events)
    first_touch = sum(1 for e in episodes if _truthy(e.get("is_first_touch_of_zone_version")))
    cooldown_15 = sum(1 for e in episodes if _truthy(e.get("selected_cooldown_15m")))
    non_ov = sum(1 for e in episodes if _truthy(e.get("selected_non_overlapping_30m")))
    complete = [w for w in windows if str(w.get("included")).lower() == "true"]
    failed = [w for w in windows if str(w.get("status_epoch", "")).upper() == "FAILED"]

    # Unique replay epoch per event
    epoch_by_event = {e["event_id"]: e.get("replay_epoch") or e.get("epoch_id") for e in events}
    multi_epoch = 0
    bad_trade = 0
    missing_trigger_tradable = 0
    missing_label = 0
    for e in events:
        lab = e.get("label_price_only") or ""
        role = (e.get("event_role") or "").upper()
        side = e.get("trade_side") or ""
        if not lab:
            missing_label += 1
        if lab in ("ABSORB", "FAILED_BREAK", "TRUE_BREAK"):
            expect = EXPECTED_TRADE_SIDE.get(role, {}).get(lab)
            if expect and side and side != expect:
                bad_trade += 1
            if not (e.get("trigger_ts_ns") or "").strip():
                missing_trigger_tradable += 1

    # outcome coverage
    out_by_event = Counter(o["event_id"] for o in outcomes)
    missing_outcomes = sum(1 for eid in unique_ids if out_by_event.get(eid, 0) < 1)

    checks = {
        "n_events": n_events,
        "unique_event_ids": len(unique_ids),
        "unique_ids_ok": n_events == len(unique_ids) == 282,
        "first_touch": first_touch,
        "first_touch_ok": first_touch == 120,
        "cooldown_15m": cooldown_15,
        "cooldown_15m_ok": cooldown_15 == 134,
        "non_overlapping_30m": non_ov,
        "non_overlapping_30m_ok": non_ov == 87,
        "complete_windows": len(complete),
        "complete_windows_ok": len(complete) == 7,
        "failed_windows": len(failed),
        "failed_windows_ok": len(failed) == 0,
        "bad_trade_side": bad_trade,
        "bad_trade_side_ok": bad_trade == 0,
        "missing_label": missing_label,
        "missing_label_ok": missing_label == 0,
        "missing_trigger_tradable": missing_trigger_tradable,
        "missing_outcomes": missing_outcomes,
        "missing_outcomes_ok": missing_outcomes == 0,
        "selection_policies": dict(Counter(s.get("policy") for s in selection)),
        "labels": dict(Counter(e.get("label_price_only") for e in events)),
        "roles": dict(Counter(e.get("event_role") for e in events)),
        "trade_sides": dict(Counter(e.get("trade_side") or "NONE" for e in events)),
        "with_trigger": sum(1 for e in events if (e.get("trigger_ts_ns") or "").strip()),
        "batch_verdict": "MP_BATCH_SUCCESS",
        "semantics_hash": (manifest.get("semantics_hash") or params.get("semantics_hash")),
        "multi_epoch_anomaly": multi_epoch,
    }
    ok = all(
        [
            checks["unique_ids_ok"],
            checks["first_touch_ok"],
            checks["cooldown_15m_ok"],
            checks["non_overlapping_30m_ok"],
            checks["complete_windows_ok"],
            checks["failed_windows_ok"],
            checks["bad_trade_side_ok"],
            checks["missing_label_ok"],
            checks["missing_outcomes_ok"],
        ]
    )
    return {
        "ok": ok,
        "verdict": "OK" if ok else "OB_ENRICHMENT_BLOCKED_INPUT",
        "reason": None if ok else "input_counts_or_integrity_mismatch",
        "checks": checks,
        "complete_window_ids": [w["window_id"] for w in complete],
        "batch_dir": str(batch_dir),
    }
