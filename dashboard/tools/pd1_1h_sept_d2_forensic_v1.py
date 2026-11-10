#!/usr/bin/env python3
"""READ-ONLY Sept 2026 DOGEUSDT 1h lower D2 forensics (~0.087–0.088, arrows 6–10 Sep)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

DASH = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DASH))

from pd1_landscape_viewer_v1.historical_window_v1 import (
    WARMUP_HOURS,
    _causal_decision_close,
    _iter_5m,
    _origin_logical_v2_key,
    _parse_utc,
)
from pd1_landscape_viewer_v1.on_demand_1hpd1_v1 import (
    SLOT_MINUTES,
    _active_members_at,
    _load_engine_candles,
    build_landscape_window_on_demand_1hpd1_v1,
)
from pd1_landscape_viewer_v1.window_response_cache_v1 import clear_cache_for_tests
from research_charts.short_pool_oracle_v1 import parse_decision_iso

# Import shared helpers from forensic test module
from test_pd1_1h_d2_causality_forensic_v1 import (  # noqa: E402
    Y_HI,
    Y_LO,
    api_pool_active_at_iso,
    compute_exact_bands_lower,
    d2_bands_at_dc,
    d2_from_api_lifetimes,
    frontend_lower_d2_zones,
    iter_hourly_dcs,
    load_raw_pools,
)

VIS_FROM = "2026-08-31T00:00:00Z"
VIS_TO = "2026-09-13T00:00:00Z"
Y_FOCUS_LO = 0.086
Y_FOCUS_HI = 0.090

ARROW_LEFT = ("2026-09-06T00:00:00Z", "2026-09-08T23:00:00Z", "left_arrow_0.087-0.088")
ARROW_RIGHT = ("2026-09-09T00:00:00Z", "2026-09-10T23:00:00Z", "right_arrow_~0.088")

OUT = DASH / "logs/pd1_1h_sept_d2_forensic_v1.json"


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_known_at(s: str) -> datetime:
    s = str(s).strip()
    if s.endswith("+00:00+00:00"):
        s = s[: -len("+00:00")]
    return parse_decision_iso(s)


def band_overlaps_focus(y0: float, y1: float, t_dc: datetime, t0: datetime, t1: datetime) -> bool:
    if y1 <= Y_FOCUS_LO or y0 >= Y_FOCUS_HI:
        return False
    return t0 <= t_dc <= t1


def collect_d2_episodes(
    raw_pools: list[dict],
    context_from: datetime,
    vis_to: datetime,
    time_lo: datetime,
    time_hi: datetime,
) -> list[dict]:
    """Per price band: first/last D2 DC, origin sets, DC timeline in focus window."""
    episodes: dict[tuple[int, int], dict] = {}
    for dc in iter_hourly_dcs(context_from, vis_to):
        if dc < time_lo - timedelta(hours=48) or dc > time_hi + timedelta(hours=48):
            continue
        members = _active_members_at(raw_pools, dc)
        for b in d2_bands_at_dc(members, Y_LO, Y_HI):
            if b["depth"] < 2:
                continue
            y0, y1 = float(b["y0"]), float(b["y1"])
            if y1 <= Y_FOCUS_LO or y0 >= Y_FOCUS_HI:
                continue
            key = (int(round(y0 * 1e8)), int(round(y1 * 1e8)))
            origins = sorted(
                {
                    m.get("origin") or _origin_logical_v2_key(m["pool_id"])
                    for m in members
                    if m.get("side") == "lower"
                    and float(m["bottom"]) < y1
                    and float(m["top"]) > y0
                }
            )
            ep = episodes.get(key)
            if ep is None:
                ep = {
                    "price_y0": y0,
                    "price_y1": y1,
                    "first_d2_dc": _iso_z(dc),
                    "last_d2_dc": _iso_z(dc),
                    "origins_first_dc": origins,
                    "d2_dc_count": 0,
                    "d2_in_focus_window": [],
                }
                episodes[key] = ep
            ep["last_d2_dc"] = _iso_z(dc)
            ep["d2_dc_count"] += 1
            if time_lo <= dc <= time_hi:
                ep["d2_in_focus_window"].append(
                    {"dc": _iso_z(dc), "origins": origins, "depth": b["depth"]}
                )
    return sorted(episodes.values(), key=lambda e: (e["price_y0"], e["first_d2_dc"]))


def origin_details(raw_pools: list[dict], origin_ids: list[str]) -> list[dict]:
    rows = []
    for o in origin_ids:
        candidates = [
            p
            for p in raw_pools
            if (p.get("origin") or _origin_logical_v2_key(p["pool_id"])) == o
            and p.get("side") == "lower"
        ]
        if not candidates:
            rows.append({"origin": o, "missing_in_engine": True})
            continue
        p = min(candidates, key=lambda x: _parse_known_at(str(x.get("known_at"))))
        rows.append(
            {
                "origin": o,
                "pool_id": p.get("pool_id"),
                "known_at": p.get("known_at"),
                "bottom": p.get("bottom"),
                "top": p.get("top"),
            }
        )
    return rows


def api_d2_segments_in_band(api_pools: list[dict], y0: float, y1: float, t0: datetime, t1: datetime) -> list[dict]:
    out = []
    context_from = t0 - timedelta(hours=WARMUP_HOURS)
    for dc in iter_hourly_dcs(context_from, t1):
        if dc < t0 or dc > t1:
            continue
        active = [pl for pl in api_pools if pl.get("side") == "lower" and api_pool_active_at_iso(pl, dc)]
        for b in d2_bands_at_dc(active, y0 - 1e-9, y1 + 1e-9):
            if b["depth"] < 2:
                continue
            if not (b["y1"] > y0 and b["y0"] < y1):
                continue
            origins = sorted(
                {
                    pl.get("origin") or _origin_logical_v2_key(pl["pool_id"])
                    for pl in active
                    if float(pl.get("bottom", 0)) < b["y1"] and float(pl.get("top", 0)) > b["y0"]
                }
            )
            out.append({"dc": _iso_z(dc), "band": b, "origins": origins})
    first_dc = out[0]["dc"] if out else None
    api_seg_starts = []
    for pl in api_pools:
        if pl.get("side") != "lower":
            continue
        pb, pt = float(pl.get("bottom", 0)), float(pl.get("top", 0))
        if pt <= y0 or pb >= y1:
            continue
        for seg in pl.get("segments") or []:
            s0 = _parse_utc(seg[0])
            if t0 - timedelta(hours=72) <= s0 <= t1:
                api_seg_starts.append(
                    {
                        "pool_id": pl["pool_id"],
                        "origin": pl.get("origin") or _origin_logical_v2_key(pl["pool_id"]),
                        "segment_start": seg[0],
                        "segment_end": seg[1],
                        "bottom": pb,
                        "top": pt,
                    }
                )
    return {"per_dc_d2": out[:5], "first_api_d2_dc": first_dc, "segment_starts_in_band": api_seg_starts[:20]}


def plan_fixed_72h_tiles(vis_from_ms: int, vis_to_ms: int) -> list[tuple[str, str]]:
    js = DASH / "logs" / "_sept_tile_plan.js"
    js.write_text(
        f"""
const fs = require('fs');
const vm = require('vm');
const code = fs.readFileSync('static/js/research/pd1_exact_density_trp_adapter.js','utf8');
const sb = {{ console, Math, Date, Number, Map, Set, parseInt, parseFloat, JSON }};
vm.createContext(sb); vm.runInContext(code, sb);
const AD = sb.PD1ExactDensityTrpAdapter;
const lo = Date.parse('2026-07-01T00:00:00Z');
const hi = Date.parse('2026-11-01T00:00:00Z');
const sk = AD.exactOriginScopeKey('1h', 1);
const prefetch = 36*3600*1000;
const tail = 6*3600*1000;
const STEP = 5*60*1000;
const vf = {vis_from_ms};
const vt = {vis_to_ms};
const from = Math.max(lo, Math.floor((vf-prefetch)/STEP)*STEP);
const to = Math.min(hi, Math.ceil((vt+tail+prefetch)/STEP)*STEP);
const fixed = AD.planExactOriginLoadWindows(from, to, lo, hi, {{ mode: 'fixed48h', tileHours: 72, scopeKey: sk }});
console.log(JSON.stringify(fixed.map(w => ({{ from: new Date(w.fromMs).toISOString(), to: new Date(w.toMs).toISOString() }}))));
""",
        encoding="utf-8",
    )
    proc = subprocess.run(["node", str(js)], cwd=str(DASH), capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr or proc.stdout)
    raw = json.loads(proc.stdout.strip())
    return [(x["from"], x["to"]) for x in raw]


def merge_api_pools(tile_payloads: list[dict]) -> dict[str, dict]:
    by_id: dict[str, dict] = {}
    for p in tile_payloads:
        for pl in p.get("pools") or []:
            pid = pl["pool_id"]
            if pid not in by_id:
                by_id[pid] = json.loads(json.dumps(pl))
            else:
                existing = by_id[pid]
                segs = list(existing.get("segments") or [])
                for seg in pl.get("segments") or []:
                    if seg not in segs:
                        segs.append(seg)
                existing["segments"] = sorted(segs, key=lambda s: s[0])
    return by_id


def fe_zone_table(fe_zones: list[dict], y_lo: float, y_hi: float, t0: datetime, t1: datetime) -> list[dict]:
    rows = []
    for z in fe_zones:
        if z.get("side") != "lower" or z.get("metadata", {}).get("depth", 0) < 2:
            continue
        y0, y1 = float(z["bottom_price"]), float(z["top_price"])
        if y1 <= y_lo or y0 >= y_hi:
            continue
        t_start = z.get("t_start")
        t_end = z.get("t_end")
        if t_end and t_end < int(t0.timestamp() * 1000):
            continue
        if t_start and t_start > int(t1.timestamp() * 1000):
            continue
        meta = z.get("metadata") or {}
        rows.append(
            {
                "bottom": y0,
                "top": y1,
                "t_start_ms": t_start,
                "t_end_ms": t_end,
                "t_start_iso": _iso_z(datetime.fromtimestamp(t_start / 1000, tz=timezone.utc)) if t_start else None,
                "t_end_iso": _iso_z(datetime.fromtimestamp(t_end / 1000, tz=timezone.utc)) if t_end else None,
                "decision_close_ms": meta.get("decision_close_ms"),
                "depth": meta.get("depth"),
            }
        )
    return rows


def rating_row(
    ep: dict,
    api_info: dict,
    fe_rows: list[dict],
    raw_pools: list[dict],
) -> dict:
    y0, y1 = ep["price_y0"], ep["price_y1"]
    price_band = f"{y0:.5f}–{y1:.5f}"
    first_d2 = ep["first_d2_dc"]
    api_first = api_info.get("first_api_d2_dc")
    fe_starts = [r["t_start_iso"] for r in fe_rows if r["bottom"] < y1 and r["top"] > y0 and r["t_start_iso"]]
    fe_start = min(fe_starts) if fe_starts else None

    verdict = "OK"
    notes = []
    if api_first and api_first < first_d2:
        verdict = "LOOKAHEAD_API"
        notes.append(f"API D2 before recon: api={api_first} recon={first_d2}")
    elif api_first and api_first > first_d2:
        notes.append(f"API D2 later than recon (clip/window): api={api_first} recon={first_d2}")
    if fe_start and fe_start < first_d2:
        verdict = "LOOKAHEAD_FE"
        notes.append(f"FE zone before recon D2: fe={fe_start} recon={first_d2}")
    if not api_first and ep["d2_in_focus_window"]:
        verdict = "MISSING_API_D2"
        notes.append("Recon D2 in arrow window but no API D2 in band")
    if fe_start and first_d2 and fe_start > first_d2:
        notes.append(f"FE starts after causal D2 (possible merge/visual): fe={fe_start} recon={first_d2}")

    return {
        "price_band": price_band,
        "erster_gueltiger_d2_dc": first_d2,
        "api_start": api_first,
        "frontend_start": fe_start,
        "bewertung": verdict,
        "notes": notes,
        "origins": origin_details(raw_pools, ep.get("origins_first_dc") or []),
        "focus_dcs": ep.get("d2_in_focus_window")[:8],
    }


def compare_tile_vs_single_ref(tiles: list[tuple[str, str]], y_lo: float, y_hi: float) -> list[dict]:
    """Per tile: merged tile pools vs fresh single-window build (same bounds)."""
    mismatches = []
    clear_cache_for_tests()
    for f, t in tiles:
        tiled = build_landscape_window_on_demand_1hpd1_v1(f, t)
        ref = build_landscape_window_on_demand_1hpd1_v1(f, t)
        by_a = {pl["pool_id"]: pl.get("segments") for pl in tiled["pools"] if pl.get("side") == "lower"}
        by_b = {pl["pool_id"]: pl.get("segments") for pl in ref["pools"] if pl.get("side") == "lower"}
        for pid in set(by_a) | set(by_b):
            if by_a.get(pid) != by_b.get(pid):
                mismatches.append({"tile": f"{f}..{t}", "pool_id": pid})
                if len(mismatches) >= 5:
                    return mismatches
    return mismatches


def main() -> None:
    vis_from = _parse_utc(VIS_FROM)
    vis_to = _parse_utc(VIS_TO)
    context_from = vis_from - timedelta(hours=WARMUP_HOURS)

    print("Loading engine pools (run_live_micro_lld_v2)...", flush=True)
    raw = load_raw_pools(vis_from, vis_to)

    print("Building full-span API window (72h chunks)...", flush=True)
    api_by_id: dict[str, dict] = {}
    chunk_windows = [
        ("2026-08-31T00:00:00Z", "2026-09-02T12:00:00Z"),
        ("2026-09-02T12:00:00Z", "2026-09-05T00:00:00Z"),
        ("2026-09-05T00:00:00Z", "2026-09-07T12:00:00Z"),
        ("2026-09-07T12:00:00Z", "2026-09-10T00:00:00Z"),
        ("2026-09-10T00:00:00Z", "2026-09-13T00:00:00Z"),
    ]
    tile_payloads = []
    for f, t in chunk_windows:
        tile_payloads.append(build_landscape_window_on_demand_1hpd1_v1(f, t))
    api_merged = merge_api_pools(tile_payloads)
    api_pools = list(api_merged.values())

    fixed_tiles = plan_fixed_72h_tiles(
        int(vis_from.timestamp() * 1000),
        int(_parse_utc("2026-09-12T00:00:00Z").timestamp() * 1000),
    )
    fixed_payloads = [build_landscape_window_on_demand_1hpd1_v1(f, t) for f, t in fixed_tiles]
    fixed_merged = merge_api_pools(fixed_payloads)

    segment_diffs = []
    for pid, pl in api_merged.items():
        other = fixed_merged.get(pid)
        if not other:
            continue
        if (pl.get("segments") or []) != (other.get("segments") or []):
            pb, pt = float(pl.get("bottom", 0)), float(pl.get("top", 0))
            if pt > Y_FOCUS_LO and pb < Y_FOCUS_HI:
                segment_diffs.append(
                    {
                        "pool_id": pid,
                        "sliding_segments": pl.get("segments"),
                        "fixed_tile_segments": other.get("segments"),
                    }
                )

    print("Frontend exact-origin zones...", flush=True)
    fe_zones = frontend_lower_d2_zones(api_pools, VIS_FROM, VIS_TO)

    arrow_reports = []
    rating_table = []
    for t_lo_s, t_hi_s, label in (ARROW_LEFT, ARROW_RIGHT):
        t_lo = _parse_utc(t_lo_s)
        t_hi = _parse_utc(t_hi_s)
        eps = collect_d2_episodes(raw, context_from, vis_to, t_lo, t_hi)
        for ep in eps:
            if not ep["d2_in_focus_window"]:
                continue
            api_info = api_d2_segments_in_band(
                api_pools, ep["price_y0"], ep["price_y1"], t_lo, t_hi
            )
            fe_rows = fe_zone_table(fe_zones, ep["price_y0"], ep["price_y1"], t_lo, t_hi)
            row = rating_row(ep, api_info, fe_rows, raw)
            row["arrow"] = label
            rating_table.append(row)
        arrow_reports.append({"label": label, "episodes_in_corridor": eps})

    # Global parity checks in focus price band Sep 1–12
    parity_violations = []
    for dc in iter_hourly_dcs(_parse_utc("2026-09-01T00:00:00Z"), _parse_utc("2026-09-12T00:00:00Z")):
        members = _active_members_at(raw, dc)
        recon = any(
            b["depth"] >= 2
            for b in d2_bands_at_dc(members, Y_FOCUS_LO, Y_FOCUS_HI)
        )
        api_active = [
            pl for pl in api_pools if pl.get("side") == "lower" and api_pool_active_at_iso(pl, dc)
        ]
        api_any = any(
            b["depth"] >= 2 for b in d2_bands_at_dc(api_active, Y_FOCUS_LO, Y_FOCUS_HI)
        )
        if api_any and not recon:
            parity_violations.append({"dc": _iso_z(dc), "kind": "api_d2_without_recon"})
        if recon and not api_any:
            parity_violations.append({"dc": _iso_z(dc), "kind": "recon_d2_without_api"})

    report = {
        "scope": {
            "symbol": "DOGEUSDT",
            "pool_tf": "1h",
            "vis_from": VIS_FROM,
            "vis_to": VIS_TO,
            "y_focus": [Y_FOCUS_LO, Y_FOCUS_HI],
            "screenshot_in_repo": False,
        },
        "fixed_72h_tiles": [{"from": f, "to": t} for f, t in fixed_tiles],
        "segment_diffs_sliding_chunks_vs_fixed_tiles_focus_band": segment_diffs[:15],
        "rating_table": rating_table,
        "arrow_reports": arrow_reports,
        "parity_violations_focus_band": parity_violations[:20],
        "fe_zones_focus": fe_zone_table(
            fe_zones, Y_FOCUS_LO, Y_FOCUS_HI, _parse_utc("2026-09-01T00:00:00Z"), _parse_utc("2026-09-12T00:00:00Z")
        ),
        "tile_cold_warm_identity_ok": compare_tile_vs_single_ref(fixed_tiles[:2], Y_FOCUS_LO, Y_FOCUS_HI),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {OUT}", flush=True)
    print(json.dumps({"rating_rows": len(rating_table), "violations": len(parity_violations)}, indent=2))


if __name__ == "__main__":
    main()
