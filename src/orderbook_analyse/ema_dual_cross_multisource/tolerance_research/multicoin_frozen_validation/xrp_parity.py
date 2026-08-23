"""XRP parity helpers against existing frozen exports (no market reload)."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import PRIMARY_CELLS, PRIMARY_COST_PCT, PRIMARY_MODE, PRIMARY_TF

DEFAULT_XRP_CANDIDATES_EXPORT = (
    "results/edc_sync_tolerance/xrp_30d_core_sources_comparison/candidates_with_sources.csv"
)

# Fields required for causal multi-coin ↔ XRP export parity.
XRP_PARITY_FIELDS = (
    "candidate_id",
    "decision_at",
    "entry_at",
    "entry_price",
    "direction",
    "mode_id",
    "core_research_verdict",  # source group / research label
)


def frozen_cells_match_xrp_matrix_defs() -> dict[str, Any]:
    """Structural parity: four M0 cells match the XRP-frozen primary set."""
    expected = {
        (0.60, 0.50, "6h"),
        (0.60, 0.50, "8h"),
        (0.75, 0.50, "6h"),
        (0.75, 0.50, "8h"),
    }
    got = {(c["tp_pct"], c["sl_pct"], c["horizon"]) for c in PRIMARY_CELLS}
    ref = next(c for c in PRIMARY_CELLS if c.get("is_reference"))
    return {
        "primary_tf": PRIMARY_TF,
        "primary_mode": PRIMARY_MODE,
        "primary_cost_pct": PRIMARY_COST_PCT,
        "cells_match": got == expected,
        "reference_is_tp075_sl050_8h": ref["tp_pct"] == 0.75
        and ref["sl_pct"] == 0.50
        and ref["horizon"] == "8h",
        "got": sorted(got),
        "expected": sorted(expected),
    }


def _norm_ts(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt.isoformat()
    except Exception:
        return str(value)


def _norm_price(value: Any, tol: float = 1e-8) -> float | None:
    if value is None or value == "":
        return None
    return round(float(value), 8)


def _norm_direction(value: Any) -> str:
    return str(value or "").upper()


def load_xrp_export_candidates(path: str | Path) -> list[dict[str, Any]]:
    """Load local XRP export CSV (file only — no ClickHouse)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"XRP candidates export missing: {p}")
    with p.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def compare_xrp_candidates_to_export(
    produced: list[dict[str, Any]],
    export_rows: list[dict[str, Any]],
    *,
    modes: tuple[str, ...] | None = None,
    timeframes: tuple[str, ...] | None = None,
    scopes: tuple[tuple[str, str], ...] | None = None,
    price_tol: float = 1e-6,
) -> dict[str, Any]:
    """Parity vs export restricted to multicoin detection scopes (fixes scope bug).

    Default scope: (5m,M0), (5m,M5), (15m,M4) — not all mode×tf combinations.
    """
    from ..shared_strategy.semantics import MULTICOIN_DETECTION_SCOPES

    scope_set = set(scopes or MULTICOIN_DETECTION_SCOPES)
    if modes is not None or timeframes is not None:
        # Legacy kwargs: intersect with explicit lists if provided
        mode_filter = set(modes) if modes is not None else None
        tf_filter = set(timeframes) if timeframes is not None else None
    else:
        mode_filter = None
        tf_filter = None

    def relevant(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for r in rows:
            sym = str(r.get("symbol", "XRPUSDT")).upper()
            if sym not in ("XRPUSDT", ""):
                continue
            tf = str(r.get("timeframe") or "")
            mid = str(r.get("mode_id") or "")
            if (tf, mid) not in scope_set:
                continue
            if mode_filter is not None and mid not in mode_filter:
                continue
            if tf_filter is not None and tf not in tf_filter:
                continue
            out.append(r)
        return out

    prod = relevant(produced)
    exp = relevant(export_rows)
    exp_by_id = {str(r.get("candidate_id")): r for r in exp}

    mismatches: list[dict[str, Any]] = []
    missing_in_export: list[str] = []
    matched = 0

    for p in prod:
        cid = str(p.get("candidate_id"))
        e = exp_by_id.get(cid)
        if e is None:
            # Re-detect may find candidates absent from the frozen CSV artifact.
            # That is recorded as an extra, not a hard failure (CSV is not SoT).
            missing_in_export.append(cid)
            continue
        field_diffs = []
        for field in XRP_PARITY_FIELDS:
            if field == "candidate_id":
                continue
            if field in ("decision_at", "entry_at"):
                pv, ev = _norm_ts(p.get(field)), _norm_ts(e.get(field))
                if pv != ev:
                    field_diffs.append({"field": field, "produced": pv, "export": ev})
            elif field == "entry_price":
                pv, ev = _norm_price(p.get(field)), _norm_price(e.get(field))
                if pv is None or ev is None or abs(pv - ev) > price_tol:
                    field_diffs.append({"field": field, "produced": pv, "export": ev})
            elif field == "direction":
                if _norm_direction(p.get(field)) != _norm_direction(e.get(field)):
                    field_diffs.append(
                        {"field": field, "produced": p.get(field), "export": e.get(field)}
                    )
            else:
                if str(p.get(field)) != str(e.get(field)):
                    field_diffs.append(
                        {"field": field, "produced": p.get(field), "export": e.get(field)}
                    )
        if field_diffs:
            mismatches.append({"candidate_id": cid, "reason": "FIELD_MISMATCH", "diffs": field_diffs})
        else:
            matched += 1

    prod_ids = {str(r.get("candidate_id")) for r in prod}
    missing_in_produced = sorted(
        str(r.get("candidate_id")) for r in exp if str(r.get("candidate_id")) not in prod_ids
    )

    # Hard fail only if frozen export rows are missing or field-mismatched.
    # Extras in produced (missing_in_export) are warnings — frozen CSV is an artifact.
    ok = not mismatches and not missing_in_produced
    status = "OK" if ok else "FAILED_PARITY"
    if ok and missing_in_export:
        status = "OK_WITH_EXPORT_EXTRAS"
    return {
        "ok": ok,
        "status": status,
        "n_produced": len(prod),
        "n_export": len(exp),
        "n_matched": matched,
        "n_mismatches": len(mismatches),
        "n_extras_vs_export": len(missing_in_export),
        "missing_in_export": missing_in_export,
        "missing_in_produced": missing_in_produced,
        "mismatches": mismatches[:50],
        "parity_fields": list(XRP_PARITY_FIELDS),
        "scopes": sorted(scope_set),
        "scope_note": (
            "Compares only multicoin detection scopes. "
            "Fails if export candidates are missing/mismatched; "
            "extra re-detect candidates vs frozen CSV are warnings only."
        ),
    }


def verify_xrp_candidates_against_export(
    produced: list[dict[str, Any]],
    *,
    repo: Path,
    export_path: str | Path | None = None,
) -> dict[str, Any]:
    """Load export from disk and compare. Does not query ClickHouse."""
    path = Path(export_path) if export_path else repo / DEFAULT_XRP_CANDIDATES_EXPORT
    if not path.exists():
        return {
            "ok": False,
            "status": "FAILED_PARITY",
            "reason": "EXPORT_MISSING",
            "path": str(path),
        }
    export_rows = load_xrp_export_candidates(path)
    result = compare_xrp_candidates_to_export(produced, export_rows)
    result["export_path"] = str(path)
    return result


def compare_engine_to_xrp_matrix_row(
    *,
    repo: Path,
    net_pnl_usdt: float,
    cell: dict[str, Any],
    group: str = "CORE_RESEARCH_SUPPORTIVE",
    tol: float = 1e-3,
) -> dict[str, Any]:
    """If XRP matrix CSV exists, compare a recomputed net PnL for the same cell definition."""
    path = repo / "results/edc_sync_tolerance/xrp_30d_horizon_tp_sl_matrix/primary_supportive_matrix.csv"
    if not path.exists():
        return {"status": "EXPORT_MISSING", "path": str(path)}
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    sid = cell["strategy_id"]
    horizon = cell["horizon"]
    match = next(
        (
            r
            for r in rows
            if r.get("signal_tf") == PRIMARY_TF
            and r.get("mode") == PRIMARY_MODE
            and r.get("group") == group
            and r.get("strategy_id") == sid
            and r.get("horizon") == horizon
        ),
        None,
    )
    if match is None:
        return {"status": "ROW_MISSING", "strategy_id": sid, "horizon": horizon}
    export_pnl = float(match.get("net_pnl_usdt") or 0)
    return {
        "status": "COMPARED",
        "export_net_pnl_usdt": export_pnl,
        "recomputed_net_pnl_usdt": net_pnl_usdt,
        "abs_diff": abs(export_pnl - net_pnl_usdt),
        "match_within_tol": abs(export_pnl - net_pnl_usdt) <= tol,
        "note": "Uses export entry_at/entry_price identity when comparing engine PnL.",
    }
