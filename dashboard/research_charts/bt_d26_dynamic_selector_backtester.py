"""BT-D26/BT-D28 dynamic upper-pool selector → research chart (planned short boxes only)."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

STRATEGY_ID = "pool_dynamic_selector_bt_d26"
DISPLAY_NAME = "Dynamic Upper Pool Selector · PLANNED (BT-D26/BT-D28)"
BACKTESTER_SOURCE = "bt_d26_dynamic_selector_chart_v1"
DRAWING_PREFIX = "btd26-planned-"
MARKER_PREFIX = "btd26-marker-"
DISPLAY_MODE_ALL_SETUPS = "all_setups"
DISPLAY_MODE_CURRENT_CANDIDATE = "current_candidate"
PLANNED_LABEL = "PLANNED SHORT — NOT EXECUTED"
STATE_CANDIDATE_INVALIDATED = "CANDIDATE_INVALIDATED"
STATE_NO_CANDIDATE = "NO_CANDIDATE"
PREVIEW_HOURS = 4
EXPECTED_CONTRACT = "bt_d26_dynamic_upper_pool_selector_v1"

RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,120}$", re.I)
CONTRACT_VERSION = "bt_d26_dynamic_selector_chart_integration_v1"


def _pools_ob_root() -> Path:
    return resolve_pools_ob_repo()


def _scanner_runs_root() -> Path:
    return _pools_ob_root() / "results" / "setup_scanner_runs"


def _ensure_import_paths() -> None:
    root = _pools_ob_root()
    for rel in (
        "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
        "tools/phase_bt_d10_existing_viewer_overlay_integration_v1",
    ):
        p = str(root / rel)
        if p not in sys.path:
            sys.path.insert(0, p)


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_config_provenance(run_dir: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    cfg_txt = run_dir / "config_path.txt"
    if cfg_txt.is_file():
        out["config_path"] = cfg_txt.read_text(encoding="utf-8").strip()
    cmp_path = run_dir / "comparison_vs_no_offset.json"
    if cmp_path.is_file():
        try:
            out["comparison_vs_no_offset"] = json.loads(cmp_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return out


def discover_dynamic_selector_runs() -> list[dict[str, Any]]:
    runs_root = _scanner_runs_root()
    repo = _pools_ob_root()
    runs: list[dict[str, Any]] = []
    if not runs_root.is_dir():
        return runs
    for path in sorted(runs_root.iterdir()):
        if not path.is_dir():
            continue
        setups_path = path / "setups.json"
        if not setups_path.is_file():
            continue
        try:
            doc = json.loads(setups_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if str(doc.get("contract_version") or "") != EXPECTED_CONTRACT:
            continue
        times = [s.get("decision_utc") for s in doc.get("setups") or [] if s.get("decision_utc")]
        span = {"start": min(times), "end": max(times)} if times else {}
        prov = _read_config_provenance(path)
        label = path.name
        if "entry_offset" in path.name or "offset" in path.name:
            label = path.name + " · entry −0.1%"
        elif "referenz" in path.name or "dynamic_referenz" in path.name:
            label = path.name + " · pool lower edge"
        runs.append(
            {
                "run_id": path.name,
                "display_label": label,
                "symbol": str(doc.get("symbol") or "DOGEUSDT").upper(),
                "setup_count": int(doc.get("setup_count") or len(doc.get("setups") or [])),
                "config_hash": doc.get("config_hash"),
                "scan_start_utc": doc.get("scan_start_utc"),
                "scan_end_utc": doc.get("scan_end_utc"),
                "selection_note": doc.get("selection_note"),
                "time_span": span,
                "setups_path": str(setups_path.relative_to(repo)),
                "payload_kind": CONTRACT_VERSION,
                **prov,
            }
        )
    return runs


def resolve_setups_path(run_id: str) -> Path | None:
    rid = str(run_id or "").strip()
    if not rid or not RUN_ID_RE.match(rid):
        return None
    runs_root = _scanner_runs_root()
    candidate = (runs_root / rid / "setups.json").resolve()
    try:
        candidate.relative_to(runs_root.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def load_setups_document(run_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    path = resolve_setups_path(run_id)
    if path is None:
        return None, ["run_not_found"]
    doc = json.loads(path.read_text(encoding="utf-8"))
    if str(doc.get("contract_version") or "") != EXPECTED_CONTRACT:
        return None, ["contract_version_mismatch"]
    setups = doc.get("setups") or []
    if not setups:
        return None, ["empty_setups"]
    return doc, []


def build_summary(doc: dict[str, Any], run_id: str) -> dict[str, Any]:
    setups = doc.get("setups") or []
    times = [s["decision_utc"] for s in setups if s.get("decision_utc")]
    origin_keys = {s.get("origin_set_key") for s in setups if s.get("origin_set_key")}
    setups_path = resolve_setups_path(run_id)
    prov = _read_config_provenance(setups_path.parent) if setups_path else {}
    return {
        "run_id": run_id,
        "strategy_id": STRATEGY_ID,
        "display_name": DISPLAY_NAME,
        "payload_kind": CONTRACT_VERSION,
        "config_hash": doc.get("config_hash"),
        "config_path": prov.get("config_path"),
        "symbol": str(doc.get("symbol") or "DOGEUSDT").upper(),
        "setup_count": len(setups),
        "unique_origin_sets": len(origin_keys),
        "time_span": {"start": min(times), "end": max(times)} if times else {},
        "scan_start_utc": doc.get("scan_start_utc"),
        "scan_end_utc": doc.get("scan_end_utc"),
        "selection_note": doc.get("selection_note"),
        "tp_selection_note": doc.get("tp_selection_note"),
        "entry_offset_pct": (setups[0].get("entry_offset_pct") if setups else None),
        "entry_price_mode": (setups[0].get("entry_price_mode") if setups else None),
        "filled_trade_count": 0,
        "signal_count": len(setups),
        "plan_only": True,
    }


def setup_to_position_spec(setup: dict[str, Any], *, run_id: str, config_hash: str | None) -> dict[str, Any]:
    decision = str(setup.get("decision_utc") or "")
    start = _parse_utc(decision)
    end = start + timedelta(hours=PREVIEW_HOURS)
    sid = str(setup.get("setup_id") or "")
    safe = sid.replace(":", "").replace("|", "_").replace("+", "")
    entry = float(setup["planned_entry"])
    sl = float(setup["planned_sl"])
    tp = float(setup["planned_tp"])
    dist_sl = setup.get("distance_entry_to_sl_pct")
    dist_tp = setup.get("distance_entry_to_tp_pct")
    rr = setup.get("planned_risk_reward")
    sel_reason = setup.get("selection_reason") or ""
    origin_key = setup.get("origin_set_key") or setup.get("upper_origin_ids")
    tooltip = (
        f"{PLANNED_LABEL}\n"
        f"UTC signal: {decision}\n"
        f"setup_id={sid}\n"
        f"entry={entry} sl={sl} tp={tp}\n"
        f"pool={setup.get('pool_lower')}|{setup.get('pool_upper')}\n"
        f"origins={origin_key}\n"
        f"selection_reason={sel_reason}"
    )
    if dist_sl is not None and dist_tp is not None and rr is not None:
        tooltip += f"\nΔentry→SL {dist_sl:.3f}% · Δentry→TP {dist_tp:.3f}% · R:R {rr:.3f} (planned)"
    return {
        "drawing_id": f"{DRAWING_PREFIX}{safe}",
        "drawing_type": "short_position",
        "symbol": str(setup.get("symbol") or "DOGEUSDT").upper(),
        "timeframe": "5m",
        "start": start,
        "end": end,
        "entry": entry,
        "stop": sl,
        "target": tp,
        "signal_id": sid,
        "setup_id": sid,
        "direction": "SHORT",
        "source": BACKTESTER_SOURCE,
        "lifecycle_state": "SIGNAL",
        "metadata": {
            "origin": BACKTESTER_SOURCE,
            "strategy_id": STRATEGY_ID,
            "run_id": run_id,
            "setup_id": sid,
            "plan_only": True,
            "not_executed": True,
            "preview_only": True,
            "status_label": PLANNED_LABEL,
            "origin_set_key": setup.get("origin_set_key"),
            "upper_origin_ids": setup.get("upper_origin_ids"),
            "selection_reason": sel_reason,
            "dynamic_state": setup.get("dynamic_state"),
            "pool_lower": setup.get("pool_lower"),
            "pool_upper": setup.get("pool_upper"),
            "decision_utc": decision,
            "distance_entry_to_sl_pct": dist_sl,
            "distance_entry_to_tp_pct": dist_tp,
            "planned_risk_reward": rr,
            "tooltip": tooltip,
            "strategy_config_hash": config_hash,
        },
    }


def filter_setups_viewport(
    setups: list[dict[str, Any]],
    *,
    view_start_unix: int,
    view_end_unix: int,
    preview_hours: int = PREVIEW_HOURS,
) -> list[dict[str, Any]]:
    pad = int(preview_hours * 3600)
    out: list[dict[str, Any]] = []
    for s in setups:
        decision = str(s.get("decision_utc") or "")
        if not decision:
            continue
        t0 = int(_parse_utc(decision).timestamp())
        t1 = t0 + pad
        if t1 >= view_start_unix and t0 <= view_end_unix:
            out.append(s)
    return out


def reference_setup_at_decision(doc: dict[str, Any], decision_utc: str) -> dict[str, Any] | None:
    for s in doc.get("setups") or []:
        if s.get("decision_utc") == decision_utc:
            return s
    return None


def setup_to_candidate_detail(setup: dict[str, Any] | None) -> dict[str, Any] | None:
    if not setup:
        return None
    return {
        "setup_id": setup.get("setup_id"),
        "decision_utc": setup.get("decision_utc"),
        "status": setup.get("status"),
        "dynamic_state": setup.get("dynamic_state"),
        "selection_reason": setup.get("selection_reason"),
        "invalidation_reason": setup.get("invalidation_reason"),
        "pool_lower": setup.get("pool_lower"),
        "pool_upper": setup.get("pool_upper"),
        "upper_gap_pct": setup.get("upper_gap_pct"),
        "origin_set_key": setup.get("origin_set_key"),
        "upper_origin_ids": setup.get("upper_origin_ids"),
        "entry_price_mode": setup.get("entry_price_mode"),
        "entry_offset_pct": setup.get("entry_offset_pct"),
        "planned_entry": setup.get("planned_entry"),
        "planned_sl": setup.get("planned_sl"),
        "planned_tp": setup.get("planned_tp"),
        "planned_risk_reward": setup.get("planned_risk_reward"),
        "plan_only": True,
        "not_executed": True,
        "status_label": PLANNED_LABEL,
    }


def resolve_active_candidate_at_asof(
    doc: dict[str, Any],
    as_of_unix: int,
) -> dict[str, Any]:
    """Causal active setup at as_of_unix from candidate_transitions (no guessing)."""
    transitions = list(doc.get("candidate_transitions") or [])
    setups = list(doc.get("setups") or [])
    by_decision = {str(s.get("decision_utc")): s for s in setups if s.get("decision_utc")}
    active: dict[str, Any] | None = None
    active_from: str | None = None
    last_state: str | None = None
    missing_emit_setup = False

    for row in transitions:
        decision = str(row.get("decision_utc") or "")
        if not decision:
            continue
        t_unix = int(_parse_utc(decision).timestamp())
        if t_unix > int(as_of_unix):
            break
        state = str(row.get("state") or "")
        last_state = state
        if state in (STATE_CANDIDATE_INVALIDATED, STATE_NO_CANDIDATE):
            active = None
            active_from = None
            continue
        if not row.get("current"):
            continue
        if row.get("emit_setup"):
            matched = by_decision.get(decision)
            if matched is None:
                missing_emit_setup = True
                active = None
                active_from = None
            else:
                active = matched
                active_from = decision

    as_of_dt = datetime.fromtimestamp(int(as_of_unix), tz=timezone.utc)
    base = {
        "as_of_unix": int(as_of_unix),
        "as_of_utc": _iso(as_of_dt),
        "last_transition_state": last_state,
    }
    if missing_emit_setup and active is None:
        return {**base, "status": "UNRESOLVED", "active": None, "active_from_utc": None}
    if active is None:
        return {**base, "status": "NONE", "active": None, "active_from_utc": None}
    return {
        **base,
        "status": "ACTIVE",
        "active": setup_to_candidate_detail(active),
        "active_from_utc": active_from,
    }


def prior_setup_markers_before_asof(
    doc: dict[str, Any],
    as_of_unix: int,
    *,
    active_setup_id: str | None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for setup in doc.get("setups") or []:
        decision = str(setup.get("decision_utc") or "")
        if not decision:
            continue
        if int(_parse_utc(decision).timestamp()) > int(as_of_unix):
            continue
        sid = str(setup.get("setup_id") or "")
        if active_setup_id and sid == active_setup_id:
            continue
        out.append(setup)
    return out


def setup_to_marker_spec(
    setup: dict[str, Any],
    *,
    run_id: str,
    config_hash: str | None,
) -> dict[str, Any]:
    decision = str(setup.get("decision_utc") or "")
    ts = _parse_utc(decision)
    sid = str(setup.get("setup_id") or "")
    safe = sid.replace(":", "").replace("|", "_").replace("+", "")
    detail = setup_to_candidate_detail(setup) or {}
    return {
        "drawing_id": f"{MARKER_PREFIX}{safe}",
        "drawing_type": "vline",
        "symbol": str(setup.get("symbol") or "DOGEUSDT").upper(),
        "timestamp": ts,
        "metadata": {
            "origin": BACKTESTER_SOURCE,
            "marker_kind": "bt_d26_prior_candidate",
            "strategy_id": STRATEGY_ID,
            "run_id": run_id,
            "setup_id": sid,
            "decision_utc": decision,
            "plan_only": True,
            "not_executed": True,
            "status_label": PLANNED_LABEL,
            "candidate_detail": detail,
            "tooltip": (
                f"{PLANNED_LABEL} · prior candidate\n"
                f"UTC {decision}\n"
                f"entry={setup.get('planned_entry')} sl={setup.get('planned_sl')} "
                f"tp={setup.get('planned_tp')}\n"
                f"pool={setup.get('pool_lower')}|{setup.get('pool_upper')}"
            ),
            "strategy_config_hash": config_hash,
        },
    }


def build_clean_view_draw_specs(
    doc: dict[str, Any],
    *,
    run_id: str,
    config_hash: str | None,
    view_start_unix: int,
    view_end_unix: int,
    as_of_unix: int,
) -> dict[str, Any]:
    """Current-candidate mode: ≤1 position box + prior markers (causal as-of)."""
    resolved = resolve_active_candidate_at_asof(doc, as_of_unix)
    position_specs: list[dict[str, Any]] = []
    marker_specs: list[dict[str, Any]] = []
    active_setup: dict[str, Any] | None = None
    if resolved.get("status") == "ACTIVE" and resolved.get("active"):
        for s in doc.get("setups") or []:
            if str(s.get("setup_id")) == str(resolved["active"].get("setup_id")):
                active_setup = s
                break
        if active_setup:
            spec = setup_to_position_spec(active_setup, run_id=run_id, config_hash=config_hash)
            t0 = int(_parse_utc(str(active_setup["decision_utc"])).timestamp())
            t1 = t0 + int(PREVIEW_HOURS * 3600)
            if t1 >= view_start_unix and t0 <= view_end_unix:
                position_specs.append(spec)

    active_sid = str(active_setup.get("setup_id")) if active_setup else None
    for setup in prior_setup_markers_before_asof(doc, as_of_unix, active_setup_id=active_sid):
        ms = setup_to_marker_spec(setup, run_id=run_id, config_hash=config_hash)
        t0 = int(_parse_utc(str(setup["decision_utc"])).timestamp())
        if view_start_unix <= t0 <= view_end_unix:
            marker_specs.append(ms)

    return {
        "resolved": resolved,
        "position_specs": position_specs,
        "marker_specs": marker_specs,
    }


def analyze_setup_episode_transitions(doc: dict[str, Any]) -> dict[str, Any]:
    """Read-only classification of emitted setup episodes (display audit)."""
    setups = sorted(
        list(doc.get("setups") or []),
        key=lambda s: str(s.get("decision_utc") or ""),
    )
    by_reason: dict[str, int] = {}
    rows: list[dict[str, Any]] = []
    for i, setup in enumerate(setups):
        reason = str(setup.get("selection_reason") or "UNKNOWN")
        by_reason[reason] = by_reason.get(reason, 0) + 1
        prev = setups[i - 1] if i > 0 else None
        category = "UNKNOWN"
        if reason == "FIRST_DYNAMIC_CANDIDATE_BT_D21_LOWEST":
            category = "FIRST_SELECT"
        elif reason == "LOWER_ELIGIBLE_UPPER_POOL_CHANGED":
            category = "ZONE_CHANGE_LOWER_ELIGIBLE"
        elif reason == "RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST":
            category = "RESELECT_AFTER_INVALIDATION"
        elif reason == "TRACKED_CANDIDATE_UNCHANGED":
            category = "UNCHANGED"
        pool_changed = False
        origin_changed = False
        if prev:
            pool_changed = (
                prev.get("pool_lower") != setup.get("pool_lower")
                or prev.get("pool_upper") != setup.get("pool_upper")
            )
            origin_changed = prev.get("origin_set_key") != setup.get("origin_set_key")
        rows.append(
            {
                "decision_utc": setup.get("decision_utc"),
                "setup_id": setup.get("setup_id"),
                "selection_reason": reason,
                "category": category,
                "pool_changed_vs_previous": pool_changed,
                "origin_set_changed_vs_previous": origin_changed,
                "invalidation_reason": setup.get("invalidation_reason"),
            }
        )
    return {
        "setup_count": len(setups),
        "by_selection_reason": by_reason,
        "episodes": rows,
    }
