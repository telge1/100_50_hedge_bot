"""Build capture record + landscape bundle."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TOOLS_1B = ROOT / "tools" / "phase1b_depth2_landscape_pilot_v1"
TOOLS_1C = ROOT / "tools" / "phase1c_depth2_manual_case_parity_v1"
RESULTS_1C = ROOT / "results" / "phase1c_depth2_manual_case_parity_v1"
CAPTURE_VERSION = "phase1e_depth2_reference_capture_v1"

import sys

sys.path.insert(0, str(TOOLS_1B))
sys.path.insert(0, str(TOOLS_1C))

from entry_geometry import classify_entry_vs_depth2  # noqa: E402
from profile_lib import enrich_intervals_with_distance, parse_utc  # noqa: E402
from run_phase1c import build_verified_case  # noqa: E402
from run_pilot import asof_semantics_report, pool_persistence_rows, stable_digest  # noqa: E402


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_valid_decision_time(s: str | None) -> bool:
    if not s or s.strip().upper() in ("UNKNOWN", "NEEDS_TIMESTAMP_VERIFICATION", ""):
        return False
    try:
        parse_utc(s)
        return True
    except ValueError:
        return False


def try_reuse_landscape(case_id: str, as_of: str) -> dict[str, Any] | None:
    for base in (
        ROOT / "results" / "phase1c_depth2_manual_case_parity_v1",
        ROOT / "results" / "phase1d_depth2_reference_contrast_v1",
    ):
        path = base / case_id / "landscape_profile.json"
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        spec_as = (data.get("spec") or {}).get("as_of_utc")
        sem = (data.get("asof_semantics") or {}).get("as_of_anchor_utc")
        if spec_as == as_of or sem == as_of:
            if (data.get("parity") or {}).get("ok"):
                data["_reuse_provenance"] = str(path)
                return data
    return None


def gaps_flat_from_bundle(bundle: dict[str, Any]) -> list[dict]:
    gaps = bundle.get("gaps_flat")
    if gaps:
        return gaps
    out = []
    ranked = bundle.get("gaps_ranked") or {}
    for side in ("lower", "upper"):
        for g in ranked.get(side) or []:
            out.append(g)
    return out


def build_capture_record(
    case_id: str,
    decision: str,
    decision_time_utc: str | None,
    manual_entry_price: float | None,
    user_reason: str | None,
    screenshot_reference: str | None,
    *,
    force_ssot: bool = False,
) -> dict[str, Any]:
    run_id = hashlib.sha256(
        f"{case_id}:{decision}:{decision_time_utc}:{manual_entry_price}:{CAPTURE_VERSION}".encode()
    ).hexdigest()[:16]

    base = {
        "case_id": case_id,
        "decision": decision,
        "decision_time_utc": decision_time_utc,
        "manual_entry_price": manual_entry_price,
        "user_reason": user_reason,
        "screenshot_reference": screenshot_reference,
        "capture_version": CAPTURE_VERSION,
        "analysis_run_id": run_id,
        "captured_at_utc": utc_now_iso(),
        "config": {
            "symbol": "DOGEUSDT",
            "timeframe": "5m",
            "pool_detail": 1,
            "min_depth": 2,
            "large_gap_pct": 0.5,
        },
    }

    if not is_valid_decision_time(decision_time_utc):
        base["timestamp_status"] = "NEEDS_TIMESTAMP_VERIFICATION"
        base["landscape_status"] = "SKIPPED_NO_TIMESTAMP"
        base["entry_geometry"] = None
        return base

    as_of = decision_time_utc.strip()
    if not as_of.endswith("Z"):
        as_of += "Z"

    bundle = None if force_ssot else try_reuse_landscape(case_id, as_of)
    ssot_source = "reused_prior_phase_profile"
    if bundle is None:
        spec = {
            "as_of_utc": as_of,
            "manual_entry_hint": manual_entry_price,
            "reference_price_for_gaps": manual_entry_price,
            "status": "VERIFY",
        }
        bundle = build_verified_case(case_id, spec)
        ssot_source = "build_verified_case_live_ssot"

    ref = manual_entry_price
    if ref is None:
        ref = bundle.get("reference_price")

    intervals_raw = bundle.get("depth2_intervals") or []
    if intervals_raw and "distance_to_ref_bottom" not in (intervals_raw[0] or {}):
        intervals_raw = enrich_intervals_with_distance(intervals_raw, float(ref or 0))

    side_focus = "upper" if decision in ("SHORT", "NO_SHORT") else "lower"
    entry_geom = classify_entry_vs_depth2(intervals_raw, ref, side_focus=side_focus)

    base.update(
        {
            "timestamp_status": "VERIFIED",
            "landscape_status": "COMPLETE",
            "ssot_source": ssot_source,
            "reuse_path": bundle.get("_reuse_provenance"),
            "as_of_utc": as_of,
            "parity_ok": (bundle.get("parity") or {}).get("ok"),
            "reference_price_used": ref,
            "entry_geometry": entry_geom,
            "landscape_digest": stable_digest(
                {
                    "intervals": bundle.get("depth2_intervals")
                    or intervals_raw,
                    "gaps": gaps_flat_from_bundle(bundle),
                }
            ),
            "asof_semantics": bundle.get("asof_semantics")
            or asof_semantics_report(
                {"window": {}, "provenance": bundle.get("provenance")},
                as_of,
            ),
            "ema": bundle.get("ema"),
            "pool_persistence_summary": {
                "origin_rows": len(bundle.get("origin_provenance_rows") or []),
            },
            "landscape_bundle": bundle,
        }
    )
    return base


def strip_bundle_for_registry(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    out.pop("landscape_bundle", None)
    return out
