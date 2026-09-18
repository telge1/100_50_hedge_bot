"""Per-event analysis: QDH v2 features + confidence split + % outcomes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from obfull_research_engine.bounded_level_first_analyzer_pilot_v1.persist import atomic_write_json
from obfull_research_engine.mp_price_path_4h_v1.candles import load_candles_1m
from obfull_research_engine.mp_qdh_30event_case_control_v2.analyze_one import analyze_one_event_v2
from obfull_research_engine.timeparse import format_utc_z

from . import ALLOW_CLICKHOUSE_WRITES, SCHEMA_VERSION
from .confidence import attach_confidence_fields
from .contract import CONTRACT_HASH, validate_checkpoint_contract
from .outcomes import compute_path_outcomes


def _as_dt(x: Any) -> datetime:
    if isinstance(x, datetime):
        return x if x.tzinfo else x.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(x).replace("Z", "+00:00"))


def _ns_to_dt(ns: int | str) -> datetime:
    return datetime.fromtimestamp(int(float(ns)) / 1e9, tz=timezone.utc)


def analyze_first_touch_event(
    *,
    universe_row: dict[str, Any],
    event_row: dict[str, Any],
    window: dict[str, Any],
    client: Any,
    universe_hash: str,
    out_event_dir: Path | None = None,
) -> dict[str, Any]:
    if ALLOW_CLICKHOUSE_WRITES:
        raise RuntimeError("CH writes forbidden")

    case_meta = {
        "case_role": "FIRST_TOUCH",
        "outcome_class": universe_row.get("label_price_only"),
        "pair_id": None,
    }
    qdh = analyze_one_event_v2(
        event=event_row,
        window=window,
        case_meta=case_meta,
        client=client,
        dry_run=False,
    )
    feats = dict(qdh.get("features") or {})
    funnel = (qdh.get("attribution_summary") or {}).get("funnel") or event_row.get("funnel") or {}
    if isinstance(funnel, str):
        try:
            funnel = json.loads(funnel)
        except json.JSONDecodeError:
            funnel = {}
    stats = (qdh.get("attribution_summary") or {}).get("attribution_stats") or {}
    if isinstance(stats, str):
        try:
            stats = json.loads(stats)
        except json.JSONDecodeError:
            stats = {}

    conf = attach_confidence_fields(
        coverage=qdh.get("coverage") or {},
        linkage_status=qdh.get("linkage_status"),
        has_wall=bool(qdh.get("selected")),
        funnel=funnel,
        attribution_stats=stats,
        features=feats,
        flow_meta=qdh.get("flow_meta") or {},
        decision_at=qdh.get("decision_at"),
    )
    feats.update(conf)

    # Outcomes — decision-relative (primary) and touch-relative (research)
    touch_ns = int(float(universe_row["first_touch_ts_ns"]))
    decision_ns = int(float(universe_row["trigger_ts_ns"]))
    touch_px = float(universe_row["touch_price"])
    decision_px = float(universe_row["trigger_price"])
    side = str(universe_row["trade_side"])

    # Load candles spanning touch-1m through decision+4h
    start = _ns_to_dt(min(touch_ns, decision_ns)) - timedelta(minutes=2)
    end = _ns_to_dt(max(touch_ns, decision_ns)) + timedelta(hours=4, minutes=5)
    candles = load_candles_1m(client, start=start, end=end)

    decision_out = compute_path_outcomes(
        candles=candles,
        entry_ts_ns=decision_ns,
        entry_price=decision_px,
        trade_side=side,
        entry_price_source="TRIGGER_PRICE_BATCH",
        perspective="DECISION_RELATIVE",
    )
    touch_out = compute_path_outcomes(
        candles=candles,
        entry_ts_ns=touch_ns,
        entry_price=touch_px,
        trade_side=side,
        entry_price_source="TOUCH_PRICE_BATCH",
        perspective="TOUCH_RELATIVE",
    )

    # Drop heavy flow from checkpoint JSON (keep on disk if dir provided)
    flow_100 = qdh.pop("flow_100ms", None) or []
    flow_1s = qdh.pop("flow_1s", None) or []
    wall_ev = qdh.pop("wall_movement_events", None) or []
    typed_tl = qdh.pop("typed_wall_flow_timeline", None) or []
    footprint = qdh.pop("footprint_cluster", None)

    # Attach confidence onto footprint view (does not alter QDH)
    if isinstance(footprint, dict):
        footprint = {
            **footprint,
            "flow_attribution_confidence": conf.get("flow_attribution_confidence"),
            "availability_confidence": conf.get("availability_confidence"),
        }

    result = {
        **qdh,
        "features": feats,
        "confidence": conf,
        "footprint_cluster": footprint,
        "typed_timeline_n_events": len(typed_tl),
        "decision_outcomes": decision_out,
        "touch_outcomes": touch_out,
        "universe": {
            k: universe_row[k]
            for k in (
                "event_id",
                "label_price_only",
                "trade_side",
                "fade_side",
                "break_side",
                "confluence_class",
                "event_role",
                "first_touch_ts_ns",
                "trigger_ts_ns",
                "touch_price",
                "trigger_price",
            )
            if k in universe_row
        },
        "contract_hash": CONTRACT_HASH,
        "universe_hash": universe_hash,
        "schema_version": SCHEMA_VERSION,
        "db_mutation": False,
        "status": "OK" if qdh.get("ok") else "BLOCKED",
    }
    if not qdh.get("ok"):
        result["status"] = "BLOCKED"
        result["blocked_reason"] = qdh.get("blocked_reason") or "EVENT_NOT_OK"

    if out_event_dir is not None:
        out_event_dir.mkdir(parents=True, exist_ok=True)
        # write flows
        _write_flow_csv(out_event_dir / "flow_100ms.csv", flow_100)
        _write_flow_csv(out_event_dir / "flow_1s.csv", flow_1s)
        if wall_ev:
            _write_flow_csv(out_event_dir / "wall_movement_events.csv", wall_ev)
        if typed_tl:
            _write_flow_csv(out_event_dir / "typed_wall_flow_timeline.csv", typed_tl)
            atomic_write_json(out_event_dir / "typed_wall_flow_timeline.json", typed_tl)
        if footprint:
            atomic_write_json(out_event_dir / "footprint_cluster.json", footprint)
        atomic_write_json(out_event_dir / "decision_outcomes.json", decision_out)
        atomic_write_json(out_event_dir / "touch_outcomes.json", touch_out)

    # artifact hash without huge arrays
    slim = {
        "event_id": result.get("event_id"),
        "features": {k: feats.get(k) for k in sorted(feats) if k not in ("snapshot_5s",)},
        "confidence": conf,
        "decision_outcomes_summary": {
            "reached_0_41_pct": decision_out.get("reached_0_41_pct"),
            "mfe_240": next((h.get("mfe_pct") for h in decision_out.get("horizons") or [] if h.get("horizon_min") == 240), None),
            "mae_240": next((h.get("mae_pct") for h in decision_out.get("horizons") or [] if h.get("horizon_min") == 240), None),
        },
        "contract_hash": CONTRACT_HASH,
    }
    result["artifact_hash"] = hashlib.sha256(
        json.dumps(slim, sort_keys=True, default=str).encode()
    ).hexdigest()
    return result


def _write_flow_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    import csv

    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in keys})


def load_or_reject_checkpoint(
    path: Path,
    *,
    universe_hash: str,
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    obj = json.loads(path.read_text(encoding="utf-8"))
    # support wrapped {contract_hash, result} or flat
    payload = obj.get("result") if isinstance(obj.get("result"), dict) else obj
    check = {
        "contract_hash": obj.get("contract_hash") or payload.get("contract_hash"),
        "universe_hash": obj.get("universe_hash") or payload.get("universe_hash"),
    }
    val = validate_checkpoint_contract(
        check, expected_hash=CONTRACT_HASH, expected_universe_hash=universe_hash
    )
    if not val["ok"]:
        return {"_rejected": True, **val}
    return payload
