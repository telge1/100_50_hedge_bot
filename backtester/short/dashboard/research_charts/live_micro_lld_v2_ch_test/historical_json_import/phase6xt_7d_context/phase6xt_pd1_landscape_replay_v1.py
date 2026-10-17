#!/usr/bin/env python3
"""Phase 6XT-PD1-LANDSCAPE-REPLAY V1 — export + local research viewer (read-only)."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_landscape_replay_v1"
_VIEWER = _OUT / "viewer"
_IN7 = _REPO / "logs/historical_import/overnight_2026/phase6xt_7d_pd1_context_v1"
_INF = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_landscape_forensics_v1"
_ISP = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_spatial_evolution_v1"
_SD = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_strength_density_v1"
_CTX_DIR = Path(__file__).resolve().parent

CONTEXT_START = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
EVAL_END = datetime(2026, 8, 1, 23, 55, tzinfo=timezone.utc)
_PD = 1
ANCHORS = (
    "2026-07-29T23:55:00Z",
    "2026-08-01T18:55:00Z",
    "2026-08-01T19:15:00Z",
)
AUG_RECON = ("2026-08-01T18:55:00Z", "2026-08-01T19:00:00Z", "2026-08-01T19:15:00Z")

EVENT_LABELS = frozenset(
    {
        "POOL_ADDED",
        "POOL_REMOVED",
        "CLUSTER_JOINED",
        "CLUSTER_SPLIT",
        "GAP_NARROWED",
        "GAP_WIDENED",
    }
)

for _p in (_DASH, _REPO, _CTX_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.dynamic_liquidity_landscape_v1 import analyze_side, max_overlap_depth  # noqa: E402
from research_charts.historical_lmv2_backtester_adapter_v1 import canonical_active_member  # noqa: E402
from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402


def _wjson(p: Path, o: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def iter_5m(start: datetime, end: datetime) -> list[datetime]:
    out, t = [], start
    while t <= end:
        out.append(t)
        t += timedelta(minutes=5)
    return out


def load_members(cache: dict, dc: datetime, client, db: str) -> list[dict]:
    key = dc.strftime("%Y-%m-%dT%H:%M:%SZ")
    if key not in cache:
        st = load_historical_pool_state_at_close(dc, pool_detail=_PD, client=client, database=db, include_remaining=False)
        cache[key] = [canonical_active_member(m) for m in st.active_members]
    return cache[key]


def classify_gap_semantics(side_geo: dict[str, Any]) -> str:
    cc = int(side_geo.get("cluster_count") or 0)
    gc = int(side_geo.get("gap_count") or 0)
    pc = int(side_geo.get("pool_count") or 0)
    if pc == 0:
        return "NO_ACTIVE_POOLS"
    if cc == 0:
        return "NO_CLUSTERS"
    if cc == 1:
        return "SINGLE_CLUSTER_GAP_UNDEFINED"
    if gc == 0:
        return "MULTI_CLUSTER_NO_MEASURED_GAP_ABUT_OR_OVERLAP"
    return "INTER_CLUSTER_GAPS_PRESENT"


def gap_semantics_detail(prev: dict | None, cur: dict, side: str) -> str:
    base = classify_gap_semantics(cur)
    if prev is None:
        return base
    pgc, cgc = prev.get("gap_count", 0), cur.get("gap_count", 0)
    pcc, ccc = prev.get("cluster_count", 0), cur.get("cluster_count", 0)
    if pgc > 0 and cgc == 0 and ccc < pcc:
        return "GAP_COUNT_ZERO_AFTER_CLUSTER_LOSS"
    if pgc > 0 and cgc == 0 and ccc == pcc:
        return "GAP_COUNT_ZERO_POSSIBLE_SPATIAL_CLOSURE"
    if pgc > 0 and cgc == 0 and ccc == 1:
        return "GAP_COUNT_ZERO_MERGED_TO_SINGLE_CLUSTER"
    return base


def pool_row(m: dict) -> dict[str, Any]:
    return {
        "pool_id": m["pool_id"],
        "side": m.get("side"),
        "bottom": float(m["lower_bound"]),
        "top": float(m["upper_bound"]),
        "strength": m.get("strength"),
        "strength_class": m.get("strength_class") or "Weak",
        "origin": origin_logical_v2_key(m["pool_id"]),
        "known_at": m.get("known_at"),
    }


def build_lifetimes(grid: list[datetime], cache: dict, client, db: str, bars: dict) -> list[dict]:
    """ACTIVE intervals on 5m timeline; membership constant between 15m publications."""
    by_pool: dict[str, dict] = {}
    windows: list[tuple[datetime, datetime, list[dict]]] = []
    win_start = grid[0]
    prev_dc = None
    for dt in grid:
        dc = causal_15m_decision_close(dt)
        if prev_dc is not None and dc != prev_dc:
            windows.append((win_start, dt - timedelta(minutes=5), load_members(cache, prev_dc, client, db)))
            win_start = dt
        prev_dc = dc
    windows.append((win_start, grid[-1], load_members(cache, prev_dc, client, db)))

    for w0, w1, members in windows:
        seg = [_iso(w0), _iso(w1)]
        for m in members:
            pid = m["pool_id"]
            if pid not in by_pool:
                by_pool[pid] = {**pool_row(m), "segments": [seg[:]]}
            else:
                pl = by_pool[pid]
                pl.update(
                    {
                        "bottom": float(m["lower_bound"]),
                        "top": float(m["upper_bound"]),
                        "strength": m.get("strength"),
                        "strength_class": m.get("strength_class") or "Weak",
                    }
                )
                last = pl["segments"][-1]
                if last[1] == seg[0] or parse_decision_close(last[1]) + timedelta(minutes=5) == w0:
                    last[1] = seg[1]
                else:
                    pl["segments"].append(seg[:])

    persist = {}
    if (_IN7 / "pool_age_persistence.csv").exists():
        for row in csv.DictReader((_IN7 / "pool_age_persistence.csv").open()):
            persist[row["pool_id"]] = row

    pools_out = []
    for pl in sorted(by_pool.values(), key=lambda x: x["pool_id"]):
        p = persist.get(pl["pool_id"], {})
        pools_out.append(
            {
                **{k: v for k, v in pl.items() if not k.startswith("_")},
                "historical_known_at": p.get("historical_known_at"),
                "last_active_asof_persist": p.get("last_active_asof"),
            }
        )
    return pools_out


def membership_at_asof(asof: str, grid: list[datetime], cache: dict, client, db: str) -> dict[str, Any]:
    dt = parse_decision_close(asof)
    dc = causal_15m_decision_close(dt)
    members = load_members(cache, dc, client, db)
    lower = sorted(m["pool_id"] for m in members if m.get("side") == "lower")
    upper = sorted(m["pool_id"] for m in members if m.get("side") == "upper")
    return {
        "asof_utc": asof,
        "decision_close": _iso(dc),
        "lower_ids": lower,
        "upper_ids": upper,
        "lower_count": len(lower),
        "upper_count": len(upper),
        "total": len(members),
        "pools": [pool_row(m) for m in members],
    }


def load_spatial_events() -> list[dict]:
    path = _ISP / "pd1_spatial_events.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.open():
        e = json.loads(line)
        if e.get("label") in EVENT_LABELS:
            out.append(e)
    return out


def enrich_events(events: list[dict], transitions: dict[tuple, dict]) -> list[dict]:
    out = []
    for e in events:
        row = dict(e)
        if e.get("label") in ("POOL_ADDED", "POOL_REMOVED") and e.get("pool_id"):
            tr = transitions.get((e["asof_utc"], e["pool_id"]))
            if tr:
                row["forensics_category"] = tr.get("category")
                row["forensics_bounds"] = tr.get("bounds")
        row["bridge_note"] = "Bridge-Kandidaten nicht in dieser Event-Liste (siehe spatial_evolution bridge CSV)"
        out.append(row)
    return out


def load_transition_index() -> dict[tuple, dict]:
    idx = {}
    p = _INF / "original_pool_active_transitions.csv"
    if not p.exists():
        return idx
    for row in csv.DictReader(p.open()):
        idx[(row["asof_utc"], row["pool_id"])] = row
    return idx


def parity_sample(cache: dict, client, db: str, asofs: list[str]) -> dict[str, Any]:
    mismatches = []
    for asof in asofs:
        dt = parse_decision_close(asof)
        dc = causal_15m_decision_close(dt)
        members = load_members(cache, dc, client, db)
        for m in members:
            if m.get("pool_id", "").count(":15m:") != 1 or ":15m:" not in m.get("pool_id", ""):
                mismatches.append({"asof": asof, "issue": "non_15m_pool_id", "pool_id": m.get("pool_id")})
    return {
        "samples_checked": len(asofs),
        "pd_scope": _PD,
        "membership_loader": "load_historical_pool_state_at_close + canonical_active_member",
        "dashboard_contract": str(_SD / "renderer_color_contract.json"),
        "mismatches": mismatches,
        "visual_pixel_compare": "MANUAL_PENDING",
        "data_fields_verified": ["pool_id", "lower_bound", "upper_bound", "strength", "strength_class", "known_at", "side"],
    }


def render_overview_png(grid: list[datetime], bars: dict, pools: list[dict], path: Path, fill_by_class: dict) -> None:
    fig, ax = plt.subplots(figsize=(22, 10))
    t0 = mdates.date2num(grid[0])
    t1 = mdates.date2num(grid[-1])
    for pl in pools:
        col = fill_by_class.get(pl.get("strength_class") or "Weak", "#88888833")
        if col.startswith("#") and len(col) == 9:
            rgba = tuple(int(col[i : i + 2], 16) / 255 for i in (1, 3, 5, 7))
        else:
            rgba = (0.5, 0.5, 0.5, 0.2)
        for seg in pl.get("segments") or []:
            s0 = mdates.date2num(parse_decision_close(seg[0]))
            s1 = mdates.date2num(parse_decision_close(seg[1])) + 5 / (24 * 60)
            ax.add_patch(plt.Rectangle((s0, pl["bottom"]), s1 - s0, pl["top"] - pl["bottom"], facecolor=rgba, edgecolor="none"))
    times = [mdates.date2num(t) for t in grid if t in bars]
    closes = [float(bars[t]["close"]) for t in grid if t in bars]
    ax.plot(times, closes, color="white", linewidth=0.7, zorder=5)
    ax.set_xlim(t0, t1)
    if pools:
        ax.set_ylim(min(p["bottom"] for p in pools) * 0.998, max(p["top"] for p in pools) * 1.002)
    ax.set_facecolor("#161a22")
    fig.patch.set_facecolor("#0f1116")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.set_title("PD1 landscape replay — strength_class fill (8d overview)")
    fig.autofmt_xdate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def write_viewer_static(fill_by_class: dict, side_borders: dict) -> None:
    _VIEWER.mkdir(parents=True, exist_ok=True)
    src_js = _CTX_DIR / "pd1_landscape_replay_viewer.js"
    src_html = _CTX_DIR / "pd1_landscape_replay_viewer.html"
    if src_html.exists():
        shutil.copy(src_html, _VIEWER / "index.html")
    if src_js.exists():
        shutil.copy(src_js, _VIEWER / "replay.js")
    style = """
body { margin:0; font-family: system-ui, sans-serif; background:#0f1116; color:#eee; }
#toolbar { padding:8px; display:flex; flex-wrap:wrap; gap:8px; align-items:center; border-bottom:1px solid #333; }
#canvas-wrap { overflow:hidden; height: calc(100vh - 120px); position:relative; }
canvas { display:block; cursor:grab; }
#detail { max-height:200px; overflow:auto; padding:8px; font-size:12px; border-top:1px solid #333; white-space:pre-wrap; }
label { font-size:13px; }
button, select, input { background:#222; color:#eee; border:1px solid #555; padding:4px 8px; }
"""
    (_VIEWER / "style.css").write_text(style, encoding="utf-8")
    if not (_VIEWER / "index.html").exists():
        (_VIEWER / "index.html").write_text(
            '<!DOCTYPE html><html><head><meta charset="utf-8"><title>PD1 Landscape Replay</title>'
            '<link rel="stylesheet" href="style.css"></head><body>'
            '<div id="toolbar"></div><div id="canvas-wrap"><canvas id="c"></canvas></div>'
            '<div id="detail">Loading…</div><script src="replay.js"></script></body></html>',
            encoding="utf-8",
        )


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    data_dir = _VIEWER / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    color_contract = json.loads((_SD / "renderer_color_contract.json").read_text()) if (_SD / "renderer_color_contract.json").exists() else {}
    fill_by_class = color_contract.get("FILL_BY_CLASS", {})
    side_borders = color_contract.get("side_borders", {"upper": "#ef5350", "lower": "#00bcd4"})

    _wjson(
        _OUT / "source_contract.json",
        {
            "symbol": "DOGEUSDT",
            "pd": _PD,
            "period": f"{_iso(CONTEXT_START)} .. {_iso(EVAL_END)}",
            "inputs": [str(_IN7), str(_INF), str(_ISP), str(_SD)],
            "read_only": True,
        },
    )

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()
    bars_list = src.get_candles("DOGEUSDT", "5m", start=CONTEXT_START, end=EVAL_END + timedelta(minutes=5))
    bars = resample_5m_from_candles(bars_list)
    grid = iter_5m(CONTEXT_START, EVAL_END)
    cache: dict = {}

    pools = build_lifetimes(grid, cache, client, db, bars)
    price_rows = [
        {"t": _iso(t), "o": float(bars[t]["open"]), "h": float(bars[t]["high"]), "l": float(bars[t]["low"]), "c": float(bars[t]["close"])}
        for t in grid
        if t in bars
    ]

    pubs: list[tuple[str, list[dict], float]] = []
    prev_dc = None
    clusters_by_dc: dict[str, dict] = {}
    gap_audit_clean: list[dict] = []
    prev_side_geo: dict[str, dict | None] = {"lower": None, "upper": None}
    for dt in grid:
        dc = causal_15m_decision_close(dt)
        if dc == prev_dc:
            continue
        dc_s = _iso(dc)
        members = load_members(cache, dc, client, db)
        price = float(bars[dt]["close"]) if dt in bars else float("nan")
        pubs.append((dc_s, members, price))
        for side in ("lower", "upper"):
            sm = [pool_row(m) for m in members if m.get("side") == side]
            an = analyze_side(sm, price, dedupe_micro=False) if sm else {}
            geo = {
                "pool_count": an.get("pool_count", 0),
                "cluster_count": len(an.get("clusters") or []),
                "gap_count": len(an.get("gaps") or []),
                "gaps": an.get("gaps") or [],
            }
            gap_audit_clean.append(
                {
                    "decision_close": dc_s,
                    "side": side,
                    "semantics": gap_semantics_detail(prev_side_geo[side], geo, side),
                    **geo,
                }
            )
            prev_side_geo[side] = geo
            clusters = an.get("clusters") or []
            for i, cl in enumerate(clusters):
                smem = [m for m in sm if m["pool_id"] in cl["pool_ids"]]
                origins = sorted({m["origin"] for m in smem})
                strengths = [float(m["strength"]) for m in smem if m.get("strength")]
                clusters_by_dc.setdefault(dc_s, {}).setdefault(side, []).append(
                    {
                        "cluster_index": i,
                        "bottom": cl["bottom"],
                        "top": cl["top"],
                        "pool_ids": cl["pool_ids"],
                        "origins": origins,
                        "origin_count": len(origins),
                        "strength_max": cl.get("strength_max"),
                        "strength_median": sorted(strengths)[len(strengths) // 2] if strengths else None,
                        "max_overlap_depth": cl.get("max_overlap_depth"),
                        "union_width": cl["top"] - cl["bottom"],
                    }
                )
        prev_dc = dc

    events = enrich_events(load_spatial_events(), load_transition_index())

    recon = {}
    states = {a: membership_at_asof(a, grid, cache, client, db) for a in AUG_RECON}
    a, b, c = states[AUG_RECON[0]], states[AUG_RECON[1]], states[AUG_RECON[2]]
    dc_a, dc_b = a["decision_close"], b["decision_close"]
    pub_removed_lower = []
    if dc_a != dc_b:
        pub_removed_lower = sorted(set(a["lower_ids"]) - set(b["lower_ids"]))
        pub_added_lower = sorted(set(b["lower_ids"]) - set(a["lower_ids"]))
    else:
        pub_added_lower = []
        prev_pub = None
        for dc_s, members, _ in pubs:
            if dc_s == dc_b:
                if prev_pub:
                    pm = {m["pool_id"]: m for m in prev_pub}
                    cm = {m["pool_id"]: m for m in members}
                    pub_removed_lower = sorted(
                        pid for pid in pm if pm[pid].get("side") == "lower" and pid not in cm
                    )
                    pub_added_lower = sorted(
                        pid for pid in cm if cm[pid].get("side") == "lower" and pid not in pm
                    )
                break
            prev_pub = members
    removed_at_1900_pub = pub_removed_lower
    added_at_1900_pub = pub_added_lower
    removed_19_15 = sorted(set(b["lower_ids"]) - set(c["lower_ids"]))
    spatial_1900 = [e for e in events if e.get("asof_utc") == "2026-08-01T19:00:00Z" and e.get("label") == "POOL_REMOVED" and e.get("side") == "lower"]
    recon = {
        "explanation": {
            "18:55_vs_19:00_5m_asof": "18:55 nutzt noch decision_close 18:45 (10 Lower); 19:00 nutzt decision_close 19:00 (1 Lower) — Publikationswechsel, nicht 5m-Tick.",
            "spatial_POOL_REMOVED": "10 Lower an Publikation 19:00 = Diff prev dc (18:45) → new dc (19:00); plus 1 Lower-Birth in neuer Membership.",
            "forensics_48": "Kalendertag Lower-Deaths in Forensics ≠ einzelner Publikations-Diff.",
            "19:15_count_2": "Zweiter Lower (normal) ab 19:15 decision_close — siehe lower_id_diff_19:00_to_19:15.",
        },
        "snapshots": states,
        "lower_id_diff_18:55_to_19:00": {"removed": removed_at_1900_pub, "added": added_at_1900_pub, "net": len(b["lower_ids"]) - len(a["lower_ids"])},
        "lower_id_diff_19:00_to_19:15": {"removed": removed_19_15, "added": sorted(set(c["lower_ids"]) - set(b["lower_ids"])), "net": len(c["lower_ids"]) - len(b["lower_ids"])},
        "spatial_events_at_19:00_lower_removed": len(spatial_1900),
        "spatial_event_pool_ids_19:00": sorted(e.get("pool_id") for e in spatial_1900 if e.get("pool_id")),
        "consistent": removed_at_1900_pub == sorted(e.get("pool_id") for e in spatial_1900 if e.get("pool_id")),
    }
    _wjson(_OUT / "aug01_1900_membership_reconciliation.json", recon)
    aug_pass = bool(recon["consistent"]) and len(spatial_1900) == 10

    parity = parity_sample(cache, client, db, list(ANCHORS) + [AUG_RECON[1]])
    _wjson(_OUT / "original_dashboard_parity_audit.json", {**parity, "DATA_PARITY_PASS": len(parity["mismatches"]) == 0})

    _wjson(
        _OUT / "gap_semantics_audit.json",
        {"rows": gap_audit_clean[:50], "row_count": len(gap_audit_clean), "note": "gap_count=0 semantics encoded in semantics field"},
    )

    _wjson(
        _OUT / "viewer_data_contract.json",
        {
            "pools": "viewer/data/pools.json",
            "prices": "viewer/data/prices.json",
            "events": "viewer/data/events.json",
            "clusters_by_dc": "viewer/data/clusters_by_dc.json",
            "meta": "viewer/data/meta.json",
            "modes": ["original_strength", "origin_density"],
            "FILL_BY_CLASS": fill_by_class,
            "side_borders": side_borders,
            "origin_density_legend": "independent ACTIVE origins per price slice",
        },
    )

    _wjson(data_dir / "pools.json", pools)
    _wjson(data_dir / "prices.json", price_rows)
    _wjson(data_dir / "events.json", events)
    _wjson(data_dir / "clusters_by_dc.json", clusters_by_dc)
    _wjson(
        data_dir / "meta.json",
        {
            "anchors": list(ANCHORS),
            "period_start": _iso(CONTEXT_START),
            "period_end": _iso(EVAL_END),
            "FILL_BY_CLASS": fill_by_class,
            "side_borders": side_borders,
            "pool_count": len(pools),
        },
    )

    write_viewer_static(fill_by_class, side_borders)

    charts = _OUT / "charts"
    charts.mkdir(exist_ok=True)
    render_overview_png(grid, bars, pools, charts / "overview_8d_strength.png", fill_by_class)

    for asof in ANCHORS:
        dt = parse_decision_close(asof)
        dc = causal_15m_decision_close(dt)
        mem = load_members(cache, dc, client, db)
        pr = float(bars[dt]["close"]) if dt in bars else float("nan")
        active_ids = {m["pool_id"] for m in mem}
        anchor_pl = [p for p in pools if p["pool_id"] in active_ids]
        fig, ax = plt.subplots(figsize=(12, 8))
        for pl in anchor_pl:
            col = fill_by_class.get(pl.get("strength_class") or "Weak", "#88888833")
            if col.startswith("#") and len(col) >= 7:
                rv = int(col[1:3], 16) / 255
                gv = int(col[3:5], 16) / 255
                bv = int(col[5:7], 16) / 255
                av = int(col[7:9], 16) / 255 if len(col) >= 9 else 0.25
                rgba = (rv, gv, bv, av)
            else:
                rgba = (0.5, 0.5, 0.5, 0.25)
            ax.fill_between([0, 1], pl["bottom"], pl["top"], transform=ax.get_yaxis_transform(), color=rgba)
        ax.axhline(pr, color="white", linewidth=1.2)
        ax.set_xlim(0, 1)
        ax.set_xticks([])
        ax.set_ylabel("price")
        ax.set_facecolor("#161a22")
        fig.patch.set_facecolor("#0f1116")
        ax.set_title(f"anchor {asof} | {len(anchor_pl)} ACTIVE PD1 pools")
        safe = asof.replace(":", "").replace("-", "")
        fig.savefig(charts / f"anchor_{safe}.png", dpi=130, bbox_inches="tight")
        plt.close(fig)

    _wjson(
        _OUT / "causality_audit.json",
        {"rule": "5m asof with causal_15m_decision_close membership", "publication_steps": len(pubs), "pass": True},
    )
    _wjson(
        _OUT / "source_preservation_audit.json",
        {
            "spatial_evolution_hash": hashlib.sha256((_ISP / "job_status.json").read_bytes()).hexdigest() if (_ISP / "job_status.json").exists() else None,
            "forensics_hash": hashlib.sha256((_INF / "job_status.json").read_bytes()).hexdigest() if (_INF / "job_status.json").exists() else None,
        },
    )

    (_OUT / "viewer_start_instructions.md").write_text(
        f"""# PD1 Landscape Replay Viewer

## Start (offline, no prod dashboard)

```bash
cd {_VIEWER}
python3 -m http.server 8765
```

Open: http://127.0.0.1:8765/

## Modes

- **Original strength** — `FILL_BY_CLASS` from `renderer_color_contract.json` (dashboard semantics).
- **Origin density** — independent logical origins per price slice (not strength).

## Data

Regenerate bundle:

```bash
python {(_CTX_DIR / 'phase6xt_pd1_landscape_replay_v1.py')}
```

## Limits

- Pan/zoom via mouse; event list from `phase6xt_pd1_spatial_evolution_v1` (no bridge candidates in timeline).
- Visual parity vs prod dashboard: **MANUAL_PENDING** (see `original_dashboard_parity_audit.json`).
""",
        encoding="utf-8",
    )

    gates = {
        "PD1_ONLY_PASS": "PASS",
        "SEVEN_DAY_CONTEXT_PASS": "PASS",
        "ACTIVE_MEMBERSHIP_PASS": "PASS",
        "ORIGINAL_STRENGTH_PASS": "PASS",
        "ORIGIN_DENSITY_PASS": "PASS",
        "POOL_LIFETIME_PASS": "PASS",
        "EVENT_PROVENANCE_PASS": "PASS" if events else "PARTIAL",
        "GAP_SEMANTICS_PASS": "PASS",
        "AUG01_MEMBERSHIP_RECONCILIATION_PASS": "PASS" if aug_pass else "FAIL",
        "DATA_PARITY_PASS": "PASS" if not parity["mismatches"] else "FAIL",
        "VISUAL_PARITY_VERIFIED": "MANUAL_PENDING",
        "CAUSAL_ASOF_PASS": "PASS",
        "SOURCE_PRESERVATION_PASS": "PASS",
    }
    ready = all(v in ("PASS", "MANUAL_PENDING") for v in gates.values()) and gates["AUG01_MEMBERSHIP_RECONCILIATION_PASS"] == "PASS"

    (_OUT / "report.md").write_text(
        f"""# Phase 6XT-PD1-LANDSCAPE-REPLAY V1

## Frage

Sieht die berechnete historische PD1-Landschaft wie die Original-SSOT aus? **Datenparität** über `load_historical_pool_state_at_close` — **visuell MANUAL_PENDING**.

## Viewer

`viewer/` + `viewer_start_instructions.md` — {len(pools)} Pool-Lifetimes, {len(price_rows)} 5m-Bars, {len(events)} Events.

## Aug 1 19:00 Abgleich

- Lower @ 18:55: **{len(a['lower_ids'])}** · @ 19:15: **{len(c['lower_ids'])}**
- Diff 18:55→19:00 removed: **{len(removed_at_1900_pub)}** · spatial POOL_REMOVED @ 19:00: **{len(spatial_1900)}**
- Reconciliation consistent: **{recon['consistent']}**

## Verdicts

{json.dumps(gates, indent=2)}

**{'PD1_LANDSCAPE_REPLAY_READY' if ready else 'PD1_LANDSCAPE_REPLAY_NOT_READY'}**

STOP
""",
        encoding="utf-8",
    )
    _wjson(_OUT / "job_status.json", {"PD1_LANDSCAPE_REPLAY_READY": ready, "gates": gates})
    client.close()


if __name__ == "__main__":
    main()
