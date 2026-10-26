#!/usr/bin/env python3
"""Phase 1B read-only pilot: DOGEUSDT 5m PD1 Depth-2 landscape at as-of."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_POOLS = ROOT / "backtester" / "short" / "dashboard"
DASHBOARD_SPREAD = (
    Path(__file__).resolve().parents[2].parent
    / "spread_recovery_hedge_short_dev"
    / "dashboard"
)


def _dashboard_root() -> Path:
    env = os.environ.get("PHASE1B_DASHBOARD_ROOT")
    if env:
        return Path(env)
    if (DASHBOARD_SPREAD / "pd1_landscape_viewer_v1").is_dir():
        return DASHBOARD_SPREAD
    return DASHBOARD_POOLS
RESULTS_DIR = ROOT / "results" / "phase1b_depth2_landscape_pilot_v1"
ANALYZER_JS = Path(__file__).resolve().parent / "analyzer_depth2_landscape.js"

S1_AS_OF = "2026-09-15T01:45:00Z"
S1_REF_PRICE_HINT = 0.08384  # documentation only; not used for geometry
# Viewer API allows max 864 inclusive 5m steps (= 71h55m wall span).
MAX_VISIBLE_5M_STEPS = 864
WARMUP_HOURS = 168


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(s: str) -> datetime:
    s = s.strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_window_from_ssot(as_of: str) -> dict[str, Any]:
    dash = _dashboard_root()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(dash))
    from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window

    t = _parse_utc(as_of)
    vis_to = t
    vis_from = vis_to - timedelta(seconds=(MAX_VISIBLE_5M_STEPS - 1) * 300)
    return build_landscape_window(
        _iso(vis_from),
        _iso(vis_to),
        timeframe="5m",
        pool_detail=1,
    )


def run_node_analyzer(
    window_payload: dict[str, Any],
    as_of: str,
    reference_price: float | None,
) -> dict[str, Any]:
    inp = {
        "window_payload": window_payload,
        "as_of_utc": as_of,
        "slot_minutes": 5,
        "large_gap_pct": 0.5,
        "reference_price": reference_price,
    }
    proc = subprocess.run(
        ["node", str(ANALYZER_JS)],
        input=json.dumps(inp),
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ANALYZER_JS.parent),
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout or "node analyzer failed")
    return json.loads(proc.stdout)


def pool_persistence_rows(pools: list[dict], as_of: str) -> list[dict[str, Any]]:
    as_ms = int(_parse_utc(as_of).timestamp() * 1000)
    rows = []
    for pl in pools:
        segs = pl.get("segments") or []
        first_pub = None
        active_at_as_of = False
        for seg in segs:
            s0 = _parse_utc(seg[0])
            s1 = _parse_utc(seg[1])
            if first_pub is None or s0 < first_pub:
                first_pub = s0
            if s0.timestamp() * 1000 <= as_ms <= s1.timestamp() * 1000:
                active_at_as_of = True
        age_h = None
        if first_pub:
            age_h = (as_ms / 1000 - first_pub.timestamp()) / 3600.0
        rows.append(
            {
                "pool_id": pl.get("pool_id"),
                "origin": pl.get("origin"),
                "side": pl.get("side"),
                "bottom": pl.get("bottom"),
                "top": pl.get("top"),
                "known_at": pl.get("known_at"),
                "active_at_as_of": active_at_as_of,
                "first_segment_start_utc": _iso(first_pub) if first_pub else None,
                "age_hours_at_as_of": age_h,
                "segment_count": len(segs),
                "persistence_status": "VERIFIED" if segs else "NOT_VERIFIED",
            }
        )
    return rows


def asof_semantics_report(window: dict[str, Any], as_of: str) -> dict[str, Any]:
    prov = window.get("provenance") or {}
    win = window.get("window") or {}
    return {
        "as_of_anchor_utc": as_of,
        "interpretation": (
            "Anchor is UTC instant; 5m membership uses causal decision close with slot=5. "
            "Bands use pools active at as_of_ms via segment intervals. "
            "Window visible span max 72h; lifetimes built with 168h context_from "
            "so older ACTIVE origins remain in SSOT lifetimes before clip."
        ),
        "visible_from": win.get("from_utc"),
        "visible_to": win.get("to_utc"),
        "context_from_utc": win.get("context_from_utc"),
        "warmup_hours_expected": WARMUP_HOURS,
        "max_visible_5m_steps": MAX_VISIBLE_5M_STEPS,
        "pipeline_line_id": prov.get("pipeline_line_id"),
        "config_hash": prov.get("config_hash"),
        "membership_loader": prov.get("membership_loader"),
        "incomplete_candle_policy": (
            "Use closed 5m decision at anchor; anchor minute 45 aligns to 5m grid close."
        ),
        "window_limitation": (
            "Pools clipped to visible [from,to]; origins only active outside visible window "
            "still contributed via warmup lifetimes during _build_lifetimes."
        ),
    }


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def stable_digest(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def main() -> int:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    as_of = S1_AS_OF

    try:
        window = load_window_from_ssot(as_of)
        ssot_source = "build_landscape_window_live_ch"
    except Exception as exc:
        raise SystemExit(f"SSOT window load failed (no fallback for pilot): {exc}") from exc

    analysis = run_node_analyzer(window, as_of, None)

    intervals = analysis["landscape"]["display_intervals"]
    gap_rows = []
    for side in ("lower", "upper"):
        for g in analysis["gaps"].get(side) or []:
            gap_rows.append(g)

    interval_csv_rows = []
    for iv in intervals:
        interval_csv_rows.append(
            {
                "side": iv["side"],
                "bottom_price": repr(iv["bottom_price"]),
                "top_price": repr(iv["top_price"]),
                "depth": iv["depth"],
                "origins": "|".join(iv.get("origins") or []),
                "as_of_utc": iv["as_of_utc"],
            }
        )

    origin_rows = []
    for iv in intervals:
        for oid in iv.get("origins") or []:
            origin_rows.append(
                {
                    "side": iv["side"],
                    "origin_id": oid,
                    "bottom_price": repr(iv["bottom_price"]),
                    "top_price": repr(iv["top_price"]),
                    "depth": iv["depth"],
                    "provenance": iv.get("provenance"),
                }
            )

    persist = pool_persistence_rows(window.get("pools") or [], as_of)
    semantics = asof_semantics_report(window, as_of)

    parity = analysis["landscape"]["parity"]
    parity_report = {
        "chart_path_parity": parity,
        "adapter_path": analysis.get("adapter_path"),
        "depth2_interval_count": len(intervals),
        "manual_reference_entry_hint": S1_REF_PRICE_HINT,
        "manual_dense2_lower_hint": "0.07807–0.08227 (not used for pass/fail)",
        "note": "Compare intervals to chart visually; no hardcoded price pass criteria.",
    }

    out = {
        "pilot": "phase1b_depth2_landscape_pilot_v1",
        "ssot_source": ssot_source,
        "analysis": analysis,
        "pool_persistence": persist,
        "asof_semantics": semantics,
        "parity_report": parity_report,
        "results_digest": None,
    }
    out["results_digest"] = stable_digest(
        {
            "intervals": intervals,
            "gaps": analysis["gaps"],
        }
    )

    (RESULTS_DIR / "depth2_landscape_full.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8"
    )
    (RESULTS_DIR / "depth2_intervals.json").write_text(
        json.dumps(intervals, indent=2), encoding="utf-8"
    )
    write_csv(
        RESULTS_DIR / "depth2_intervals.csv",
        ["side", "bottom_price", "top_price", "depth", "origins", "as_of_utc"],
        interval_csv_rows,
    )
    write_csv(
        RESULTS_DIR / "pool_gaps.csv",
        [
            "side",
            "gap_bottom_price",
            "gap_top_price",
            "gap_pct",
            "large_gap",
            "previous_union_top",
            "next_union_bottom",
        ],
        [
            {
                "side": g["side"],
                "gap_bottom_price": repr(g["gap_bottom_price"]),
                "gap_top_price": repr(g["gap_top_price"]),
                "gap_pct": repr(g["gap_pct"]),
                "large_gap": g["large_gap"],
                "previous_union_top": repr(g["previous_union_top"]),
                "next_union_bottom": repr(g["next_union_bottom"]),
            }
            for g in gap_rows
        ],
    )
    write_csv(
        RESULTS_DIR / "origin_assignments.csv",
        ["side", "origin_id", "bottom_price", "top_price", "depth", "provenance"],
        origin_rows,
    )
    (RESULTS_DIR / "asof_semantics.json").write_text(
        json.dumps(semantics, indent=2), encoding="utf-8"
    )
    (RESULTS_DIR / "parity_check.json").write_text(
        json.dumps(parity_report, indent=2), encoding="utf-8"
    )
    (RESULTS_DIR / "pool_persistence.json").write_text(
        json.dumps(persist, indent=2), encoding="utf-8"
    )

    summary = {
        "verdict": "DEPTH2_LANDSCAPE_PILOT_PASS"
        if parity.get("ok")
        else "DEPTH2_LANDSCAPE_PILOT_BLOCKED",
        "parity_ok": parity.get("ok"),
        "intervals": len(intervals),
        "gaps_lower": len(analysis["gaps"].get("lower") or []),
        "gaps_upper": len(analysis["gaps"].get("upper") or []),
        "digest": out["results_digest"],
    }
    (RESULTS_DIR / "pilot_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0 if parity.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
