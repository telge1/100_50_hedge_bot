#!/usr/bin/env python3
"""
Read-only Exact Origin density bands at one 5m instant — mirrors
pd1_exact_density_trp_adapter.js computeExactBandsForSide + minOrigins>=2 filter.
"""

from __future__ import annotations

import csv
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

AS_OF_ISO = "2026-09-25T11:00:00Z"
MIN_ORIGINS = 2
TIMEFRAME = "5m"
POOL_DETAIL = 1
STEP_MS = 5 * 60 * 1000
SLOT_MINUTES = 5
PIPELINE = "historical_mixed_frozen_doge_apr_sep_2026_v1_5mpd1_v1"

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_density_chart_parity_20260925_1100"
REPORT = ROOT / "reports/PHASE_DENSITY_CHART_PARITY_20260925_1100.md"
SPREAD = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")


def _setup() -> None:
    # Spread dashboard first (5m viewer contract + pd1_landscape_viewer_v1).
    for p in (
        "/home/telgenbuescher/projects/trading_research_platform",
        str(ROOT),
        str(SPREAD.parent),
        str(SPREAD),
    ):
        if p not in sys.path:
            sys.path.insert(0, p)
    import os

    os.environ["HISTORICAL_LMV2_BATCH_READER"] = "1"
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()


def parse_iso_ms(s: str) -> int:
    s = str(s).strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def causal_decision_close_ms(t_ms: int, slot_minutes: int) -> int:
    d = datetime.fromtimestamp(t_ms / 1000, tz=timezone.utc)
    slot = (d.minute // slot_minutes) * slot_minutes
    dc = d.replace(minute=slot, second=0, microsecond=0)
    return int(dc.timestamp() * 1000)


def pool_active_at(pl: dict, t_ms: int) -> bool:
    for seg in pl.get("segments") or []:
        a = parse_iso_ms(seg[0])
        b = parse_iso_ms(seg[1])
        if t_ms >= a and t_ms <= b:
            return True
    return False


def compute_exact_bands_for_side(
    pools: list[dict], t_ms: int, side: str, y_min: float, y_max: float
) -> list[dict]:
    """Port of computeExactBandsForSide (adapter.js)."""
    active = [pl for pl in pools if pool_active_at(pl, t_ms) and pl.get("side") == side]
    if not active:
        return []
    events: list[dict] = []
    for pl in active:
        lo = max(y_min, float(pl["bottom"]))
        hi = min(y_max, float(pl["top"]))
        if hi < lo:
            continue
        events.append(
            {"price": lo, "delta": 1, "pid": pl["pool_id"], "origin": pl.get("origin")}
        )
        events.append(
            {"price": hi, "delta": -1, "pid": pl["pool_id"], "origin": pl.get("origin")}
        )
    if not events:
        return []
    events.sort(key=lambda e: (e["price"], -e["delta"]))

    active_pools: dict[str, object] = {}
    bands: list[dict] = []
    prev_price: float | None = None
    prev_depth = 0
    prev_origins: set[str] = set()

    def depth_now() -> int:
        return len({str(v) for v in active_pools.values() if v is not None})

    def origins_now() -> set[str]:
        return {str(v) for v in active_pools.values() if v is not None}

    for ev in events:
        if prev_price is not None and ev["price"] > prev_price and prev_depth > 0:
            bands.append(
                {
                    "lower_price": prev_price,
                    "upper_price": ev["price"],
                    "unique_origin_count": prev_depth,
                    "origin_ids": sorted(prev_origins),
                }
            )
        if ev["delta"] > 0:
            active_pools[ev["pid"]] = ev["origin"]
        else:
            active_pools.pop(ev["pid"], None)
        prev_depth = depth_now()
        prev_origins = origins_now()
        prev_price = ev["price"]

    merged: list[dict] = []
    for b in bands:
        last = merged[-1] if merged else None
        if (
            last
            and last["unique_origin_count"] == b["unique_origin_count"]
            and last["origin_ids"] == b["origin_ids"]
            and abs(b["lower_price"] - last["upper_price"]) < 1e-15
        ):
            last["upper_price"] = b["upper_price"]
        else:
            merged.append(dict(b))
    return merged


def main() -> int:
    _setup()
    OUT.mkdir(parents=True, exist_ok=True)
    REPORT.parent.mkdir(parents=True, exist_ok=True)

    from pd1_landscape_viewer_v1.historical_window_v1 import WARMUP_HOURS, build_landscape_window

    as_of_ms = parse_iso_ms(AS_OF_ISO)
    dc_ms = causal_decision_close_ms(as_of_ms, SLOT_MINUTES)
    dc_iso = datetime.fromtimestamp(dc_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Chart uses 5m grid time tDraw = floor5m(view); at exact 11:00:00 this equals 11:00.
    t_draw_ms = (as_of_ms // STEP_MS) * STEP_MS

    # API load span aligned with chart prefetch (~36h) + 48h tile (≤72h export cap).
    vis_from = "2026-09-23T11:00:00Z"
    vis_to = "2026-09-25T11:05:00Z"

    window = build_landscape_window(
        vis_from,
        vis_to,
        timeframe=TIMEFRAME,
        pool_detail=POOL_DETAIL,
    )
    pools = window.get("pools") or []
    prov = window.get("provenance") or {}
    win = window.get("window") or {}

    if not pools:
        raise SystemExit("no pools in landscape window")

    y_min = min(float(p["bottom"]) for p in pools) * 0.9995
    y_max = max(float(p["top"]) for p in pools) * 1.0005

    rows: list[dict] = []
    for side in ("upper", "lower"):
        bands = compute_exact_bands_for_side(pools, t_draw_ms, side, y_min, y_max)
        for i, b in enumerate(bands):
            if b["unique_origin_count"] < MIN_ORIGINS:
                continue
            side_label = "Upper" if side == "upper" else "Lower"
            rows.append(
                {
                    "segment_id": f"pd1-eod-{dc_iso}-{side}-{i}",
                    "pool_id": "",
                    "side": side_label,
                    "lower_price": format(b["lower_price"], ".17g"),
                    "upper_price": format(b["upper_price"], ".17g"),
                    "unique_origin_count": b["unique_origin_count"],
                    "origin_ids": "|".join(b["origin_ids"]),
                    "pool_status": "ACTIVE",
                    "source_close_utc": dc_iso,
                }
            )

    upper = [r for r in rows if r["side"] == "Upper"]
    lower = [r for r in rows if r["side"] == "Lower"]
    upper.sort(key=lambda r: float(r["lower_price"]))
    lower.sort(key=lambda r: float(r["lower_price"]))

    all_rows = upper + lower
    max_density = max((r["unique_origin_count"] for r in all_rows), default=0)

    meta = {
        "as_of_request_utc": AS_OF_ISO,
        "t_draw_ms_utc": datetime.fromtimestamp(t_draw_ms / 1000, tz=timezone.utc).isoformat(),
        "causal_decision_close_utc": dc_iso,
        "timestamp_semantics": (
            "5m chart grid: poolActiveAt uses wall-clock ms at floor5m(as_of); "
            "membership/evaluation uses causal 5m decision close (slot floor). "
            "Same as PD1ExactDensityTrpAdapter.causalDecisionCloseMs + poolActiveAt."
        ),
        "min_unique_origins": MIN_ORIGINS,
        "pipeline_line_id": prov.get("pipeline_line_id"),
        "config_hash": prov.get("config_hash"),
        "context_from_utc": win.get("context_from_utc") or prov.get("context_from"),
        "visible_from_utc": win.get("from_utc"),
        "visible_to_utc": win.get("to_utc"),
        "warmup_hours": WARMUP_HOURS,
        "pools_in_window": len(pools),
        "y_min": y_min,
        "y_max": y_max,
        "source": "build_landscape_window → pools (SSOT) + JS-identical computeExactBandsForSide",
        "api_equivalent": "POST /live-charts/viewer/api/window (same backend)",
        "upper_band_count": len(upper),
        "lower_band_count": len(lower),
        "max_unique_origin_density": max_density,
    }

    with (OUT / "pools.json").open("w", encoding="utf-8") as f:
        json.dump({"meta": meta, "upper": upper, "lower": lower}, f, indent=2)

    fields = [
        "segment_id",
        "pool_id",
        "side",
        "lower_price",
        "upper_price",
        "unique_origin_count",
        "origin_ids",
        "pool_status",
        "source_close_utc",
    ]
    with (OUT / "pools.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(upper + lower)

    def fmt_table(title: str, data: list[dict]) -> str:
        lines = [f"### {title}", "", f"**Count:** {len(data)}", ""]
        if not data:
            lines.append("(none)")
            return "\n".join(lines)
        lines.append(
            "| segment_id | side | lower_price | upper_price | unique_origin_count | origin_ids |"
        )
        lines.append("|---|---|---:|---:|---:|---|")
        for r in data:
            oid = r["origin_ids"]
            if len(oid) > 80:
                oid = oid[:77] + "..."
            lines.append(
                f"| {r['segment_id']} | {r['side']} | {r['lower_price']} | {r['upper_price']} | "
                f"{r['unique_origin_count']} | `{oid}` |"
            )
        return "\n".join(lines)

    report = "\n".join(
        [
            "# PHASE — Density Chart Parity 2026-09-25 11:00 UTC",
            "",
            "## Provenance",
            "",
            "```json",
            json.dumps(meta, indent=2),
            "```",
            "",
            fmt_table("Table A — UPPER bands (unique origin density ≥ 2)", upper),
            "",
            fmt_table("Table B — LOWER bands (unique origin density ≥ 2)", lower),
            "",
        ]
    )
    REPORT.write_text(report, encoding="utf-8")
    print(json.dumps({"upper": len(upper), "lower": len(lower), "max_density": max_density}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
