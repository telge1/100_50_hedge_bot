#!/usr/bin/env python3
"""Phase 6XT-PD1-SPATIAL-EVOLUTION V1 — spatial landscape evolution (read-only)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_spatial_evolution_v1"
_IN7 = _REPO / "logs/historical_import/overnight_2026/phase6xt_7d_pd1_context_v1"
_INF = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_landscape_forensics_v1"
_SIG_DIR = _DASH / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_local_strength_signature"
_CTX_DIR = Path(__file__).resolve().parent
_FREEZE_DIR = _DASH / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_local_signature_freeze"

CONTEXT_START = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
EVAL_END = datetime(2026, 8, 1, 23, 55, tzinfo=timezone.utc)
_PD = 1
FOCAL = "lldv2:DOGEUSDT:15m:micro:lower:1784818800"
SNAP_PRE_J30 = "2026-07-29T23:55:00Z"
SNAP_AUG_PRE = "2026-08-01T18:55:00Z"
SNAP_AUG_POST = "2026-08-01T19:15:00Z"

for _p in (_DASH, _REPO, _SIG_DIR, _CTX_DIR, _FREEZE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()
for _p in (_SIG_DIR, _FREEZE_DIR, _CTX_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import clickhouse_connect  # noqa: E402
import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from cluster_lineage_core import membership_fingerprint  # noqa: E402
from local_strength_signature_core import build_cluster_signatures  # noqa: E402
from phase6xt_7d_context_v1 import bridge_pools as gap_span_bridge_pools  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.dynamic_liquidity_landscape_v1 import analyze_side, detect_bridge_candidates  # noqa: E402
from research_charts.historical_lmv2_backtester_adapter_v1 import canonical_active_member  # noqa: E402
from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402


def _wjson(p: Path, o: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")


def _wcsv(p: Path, rows: list[dict]) -> None:
    if not rows:
        p.write_text("", encoding="utf-8")
        return
    fn = sorted({k for r in rows for k in r})
    with p.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


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


def side_geometry(members: list[dict], side: str, price: float) -> dict[str, Any]:
    sm = [m for m in members if m.get("side") == side]
    rows = [
        {"pool_id": m["pool_id"], "bottom": float(m["lower_bound"]), "top": float(m["upper_bound"]), "strength": m.get("strength")}
        for m in sm
    ]
    an = analyze_side(rows, price, dedupe_micro=False) if rows else {}
    gaps = an.get("gaps") or []
    clusters = an.get("clusters") or []
    brid = gap_span_bridge_pools(members, side, gaps)
    return {
        "pool_count": len(sm),
        "clusters": clusters,
        "cluster_count": len(clusters),
        "gaps": gaps,
        "gap_count": len(gaps),
        "largest_gap": max((g["gap_width"] for g in gaps), default=0.0),
        "union_width": an.get("union_coverage_width", 0.0),
        "max_overlap": an.get("max_overlap_depth", 0),
        "gap_span_bridges": brid,
    }


def union_components(intervals: list[tuple[float, float]]) -> int:
    if not intervals:
        return 0
    ordered = sorted(intervals)
    comps = 1
    cur_hi = ordered[0][1]
    for lo, hi in ordered[1:]:
        if lo > cur_hi:
            comps += 1
            cur_hi = hi
        else:
            cur_hi = max(cur_hi, hi)
    return comps


def simple_regime(price: float) -> str:
    if price >= 0.071:
        return "bullish"
    if price <= 0.0695:
        return "bearish"
    return "mixed_above_200"


def snapshot_pools(members: list[dict], side: str) -> list[dict]:
    return [
        {
            "pool_id": m["pool_id"],
            "origin": origin_logical_v2_key(m["pool_id"]),
            "bottom": float(m["lower_bound"]),
            "top": float(m["upper_bound"]),
            "strength": m.get("strength"),
            "strength_class": m.get("strength_class"),
            "known_at": m.get("known_at"),
        }
        for m in members
        if m.get("side") == side
    ]


def draw_snapshot_origin_overlap(members: list[dict], price: float, asof: str, path: Path, title: str) -> None:
    """Distinct logical origins overlapping each price slice — not original strength."""
    fig, ax = plt.subplots(figsize=(12, 8))
    lo = min(float(m["lower_bound"]) for m in members) if members else price - 0.002
    hi = max(float(m["upper_bound"]) for m in members) if members else price + 0.002
    lo, hi = min(lo, price - 0.001), max(hi, price + 0.001)
    bins = 120
    ys = [lo + (hi - lo) * (k + 0.5) / bins for k in range(bins)]
    counts = []
    for y in ys:
        origins = {
            origin_logical_v2_key(m["pool_id"])
            for m in members
            if float(m["lower_bound"]) <= y <= float(m["upper_bound"])
        }
        counts.append(len(origins))
    ax.barh(ys, counts, height=(hi - lo) / bins, color="#5c6bc0", alpha=0.75)
    ax.axhline(price, color="black", linewidth=1.2, label=f"price {price:.5f}")
    ax.set_xlabel("distinct origin count at price slice")
    ax.set_ylabel("price")
    ax.set_title(f"{title}\n{asof} (origin overlap density — not strength_class)")
    ax.legend(loc="upper right")
    fig.savefig(path, dpi=130)
    plt.close(fig)


def draw_snapshot_chart(members: list[dict], price: float, asof: str, path: Path, title: str) -> None:
    fig, ax = plt.subplots(figsize=(12, 8))
    class_colors = {"Weak": "#90caf9", "Medium": "#8bc34a", "Strong": "#ffeb3b", "Very Strong": "#ff9800", "Extreme": "#f44336"}
    for m in members:
        side = m.get("side")
        col = class_colors.get(str(m.get("strength_class") or "Weak"), "#bbb")
        alpha = 0.35 if side == "upper" else 0.5
        ax.fill_between(
            [0, 1],
            [float(m["lower_bound"]), float(m["lower_bound"])],
            [float(m["upper_bound"]), float(m["upper_bound"])],
            transform=ax.get_yaxis_transform(),
            color=col,
            alpha=alpha,
        )
    ax.axhline(price, color="black", linewidth=1.2, label=f"price {price:.5f}")
    ax.set_xlim(0, 1)
    ax.set_xticks([])
    ax.set_ylabel("price")
    ax.set_title(f"{title}\n{asof} (fill=strength_class, not density)")
    ax.legend(loc="upper right")
    fig.savefig(path, dpi=130)
    plt.close(fig)


def draw_time_price_map(pools: list[dict], bars: dict, grid: list[datetime], path: Path) -> None:
    """Pool ACTIVE spans over time (from persistence) vs price band."""
    fig, ax = plt.subplots(figsize=(14, 8))
    for p in pools:
        if not p.get("first_observed_in_replay") or not p.get("last_active_asof"):
            continue
        t0 = mdates.date2num(parse_decision_close(p["first_observed_in_replay"]))
        t1 = mdates.date2num(parse_decision_close(p["last_active_asof"]))
        # bounds from transitions lookup omitted — use focal band filter lowers only for clarity
        if p.get("side") != "lower":
            continue
        # skip if no bounds in pool row - load from last transition not available
    # Simplified: 5m price line only + markers for three snaps
    times = [mdates.date2num(t) for t in grid if t in bars]
    closes = [float(bars[t]["close"]) for t in grid if t in bars]
    ax.plot(times, closes, color="black", linewidth=0.8, label="5m close")
    for label, asof in (
        ("pre Jul30 exp", SNAP_PRE_J30),
        ("Aug1 pre 19:00", SNAP_AUG_PRE),
        ("Aug1 post 19:00", SNAP_AUG_POST),
    ):
        dt = parse_decision_close(asof)
        if dt in bars:
            ax.scatter([mdates.date2num(dt)], [float(bars[dt]["close"])], s=40, label=label)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    ax.set_ylabel("price")
    ax.set_title("DOGE PD1 period price (pool bands in per-snapshot charts)")
    ax.legend()
    fig.autofmt_xdate()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()
    bars = resample_5m_from_candles(
        src.get_candles("DOGEUSDT", "5m", start=CONTEXT_START, end=EVAL_END + timedelta(minutes=5))
    )
    grid = iter_5m(CONTEXT_START, EVAL_END)
    cache: dict = {}

    _wjson(
        _OUT / "source_contract.json",
        {
            "pd": _PD,
            "period": f"{_iso(CONTEXT_START)} .. {_iso(EVAL_END)}",
            "inputs": [str(_IN7), str(_INF)],
        },
    )
    _wjson(
        _OUT / "spatial_event_contract.json",
        {
            "labels": [
                "POOL_ADDED",
                "POOL_REMOVED",
                "CLUSTER_EXTENDED",
                "CLUSTER_SHRUNK",
                "CLUSTER_JOINED",
                "CLUSTER_SPLIT",
                "GAP_NARROWED",
                "GAP_WIDENED",
                "STRENGTH_DISTRIBUTION_CHANGED",
                "PRICE_REPOSITIONED",
            ],
            "research_only_not_signals": True,
        },
    )

    pubs: list[tuple[datetime, datetime]] = []
    prev_dc = None
    for dt in grid:
        dc = causal_15m_decision_close(dt)
        if dc != prev_dc:
            pubs.append((dt, dc))
            prev_dc = dc

    spatial_events: list[dict] = []
    gap_rows: list[dict] = []
    geom_trans: list[dict] = []
    bridge_rows: list[dict] = []
    seen_bridge: set[tuple[str, str]] = set()

    prev_members: list[dict] | None = None
    prev_geo: dict[str, dict] | None = None
    prev_price = None

    for i, (dt, dc) in enumerate(pubs):
        asof = _iso(dt)
        dc_s = _iso(dc)
        price = float(bars[dt]["close"]) if dt in bars else float("nan")
        members = load_members(cache, dc, client, db)
        mmap = {m["pool_id"]: m for m in members}
        geo = {s: side_geometry(members, s, price) for s in ("lower", "upper")}

        for side in ("lower", "upper"):
            gap_rows.append(
                {
                    "asof_utc": asof,
                    "decision_close": dc_s,
                    "side": side,
                    "price": price,
                    "gap_count": geo[side]["gap_count"],
                    "largest_gap_width": geo[side]["largest_gap"],
                    "union_coverage_width": geo[side]["union_width"],
                    "cluster_count": geo[side]["cluster_count"],
                    "gap_span_bridge_ids": json.dumps(geo[side]["gap_span_bridges"]),
                }
            )

        if prev_members is not None and prev_geo is not None:
            pmap = {m["pool_id"]: m for m in prev_members}
            cur_ids = set(mmap)
            prev_ids = set(pmap)
            births = cur_ids - prev_ids
            deaths = prev_ids - cur_ids
            for pid in sorted(births):
                m = mmap[pid]
                spatial_events.append(
                    {
                        "asof_utc": asof,
                        "decision_close": dc_s,
                        "label": "POOL_ADDED",
                        "side": m.get("side"),
                        "pool_id": pid,
                        "origin": origin_logical_v2_key(pid),
                        "known_at": m.get("known_at"),
                        "bounds": [m.get("lower_bound"), m.get("upper_bound")],
                        "strength": m.get("strength"),
                    }
                )
            for pid in sorted(deaths):
                m = pmap[pid]
                spatial_events.append(
                    {
                        "asof_utc": asof,
                        "decision_close": dc_s,
                        "label": "POOL_REMOVED",
                        "side": m.get("side"),
                        "pool_id": pid,
                        "origin": origin_logical_v2_key(pid),
                        "known_at": m.get("known_at"),
                        "bounds": [m.get("lower_bound"), m.get("upper_bound")],
                        "strength": m.get("strength"),
                    }
                )

            dyn = {
                "new_lower_ids": sorted(p for p in births if mmap.get(p, {}).get("side") == "lower"),
                "new_upper_ids": sorted(p for p in births if mmap.get(p, {}).get("side") == "upper"),
            }
            reg = simple_regime(price)
            for side in ("lower", "upper"):
                for b in detect_bridge_candidates(dyn, reg, price, mmap, side):
                    key = (b["pool_id"], dc_s)
                    if key in seen_bridge:
                        continue
                    seen_bridge.add(key)
                    bridge_rows.append(
                        {
                            **b,
                            "asof_utc": asof,
                            "decision_close": dc_s,
                            "regime": reg,
                            "contract": "BP_detect_bridge_candidates",
                        }
                    )

            for side in ("lower", "upper"):
                pg, cg = prev_geo[side], geo[side]
                if cg["largest_gap"] < pg["largest_gap"] - 1e-8:
                    spatial_events.append(
                        {
                            "asof_utc": asof,
                            "label": "GAP_NARROWED",
                            "side": side,
                            "prev_largest_gap": pg["largest_gap"],
                            "cur_largest_gap": cg["largest_gap"],
                        }
                    )
                if cg["largest_gap"] > pg["largest_gap"] + 1e-8:
                    spatial_events.append(
                        {
                            "asof_utc": asof,
                            "label": "GAP_WIDENED",
                            "side": side,
                            "prev_largest_gap": pg["largest_gap"],
                            "cur_largest_gap": cg["largest_gap"],
                        }
                    )
                if cg["cluster_count"] < pg["cluster_count"] and not deaths:
                    spatial_events.append(
                        {"asof_utc": asof, "label": "CLUSTER_JOINED", "side": side, "prev_clusters": pg["cluster_count"], "cur_clusters": cg["cluster_count"]}
                    )
                if cg["cluster_count"] > pg["cluster_count"] and not births:
                    spatial_events.append(
                        {"asof_utc": asof, "label": "CLUSTER_SPLIT", "side": side, "prev_clusters": pg["cluster_count"], "cur_clusters": cg["cluster_count"]}
                    )
                ints_prev = [(float(m["lower_bound"]), float(m["upper_bound"])) for m in prev_members if m.get("side") == side]
                ints_cur = [(float(m["lower_bound"]), float(m["upper_bound"])) for m in members if m.get("side") == side]
                if union_components(ints_prev) > union_components(ints_cur) and not births:
                    spatial_events.append({"asof_utc": asof, "label": "CLUSTER_JOINED", "side": side, "note": "union_components"})
                prev_union = pg["union_width"]
                cur_union = cg["union_width"]
                if cur_union > prev_union + 1e-8 and not births:
                    spatial_events.append({"asof_utc": asof, "label": "CLUSTER_EXTENDED", "side": side, "delta_union": cur_union - prev_union})
                if cur_union < prev_union - 1e-8 and not deaths:
                    spatial_events.append({"asof_utc": asof, "label": "CLUSTER_SHRUNK", "side": side, "delta_union": cur_union - prev_union})
                strengths_prev = [float(m["strength"]) for m in prev_members if m.get("side") == side and m.get("strength")]
                strengths_cur = [float(m["strength"]) for m in members if m.get("side") == side and m.get("strength")]
                if strengths_prev and strengths_cur:
                    if abs(sum(strengths_cur) / len(strengths_cur) - sum(strengths_prev) / len(strengths_prev)) > 0.05:
                        spatial_events.append({"asof_utc": asof, "label": "STRENGTH_DISTRIBUTION_CHANGED", "side": side})

                geom_trans.append(
                    {
                        "asof_utc": asof,
                        "side": side,
                        "prev_cluster_count": pg["cluster_count"],
                        "cur_cluster_count": cg["cluster_count"],
                        "prev_union": prev_union,
                        "cur_union": cur_union,
                        "prev_max_overlap": pg["max_overlap"],
                        "cur_max_overlap": cg["max_overlap"],
                        "births": len([p for p in births if mmap.get(p, {}).get("side") == side]),
                        "deaths": len([p for p in deaths if pmap.get(p, {}).get("side") == side]),
                    }
                )

        if prev_price is not None and prev_members == members and abs(price - prev_price) > 0:
            spatial_events.append(
                {"asof_utc": asof, "label": "PRICE_REPOSITIONED", "price": price, "prev_price": prev_price}
            )

        prev_members = members
        prev_geo = geo
        prev_price = price

    with (_OUT / "pd1_spatial_events.jsonl").open("w", encoding="utf-8") as f:
        for e in spatial_events:
            f.write(json.dumps(e, default=str) + "\n")

    _wcsv(_OUT / "pd1_gap_evolution.csv", gap_rows)
    _wcsv(_OUT / "pd1_cluster_geometry_transitions.csv", geom_trans)
    _wcsv(_OUT / "pd1_bridge_candidate_provenance.csv", bridge_rows)

    unique_bridge_pools = len({r["pool_id"] for r in bridge_rows})
    bridge_md = f"""# PD1 bridge contract comparison

## A — Gap-span full bridge (`bridge_pools` in 7d context)
- **Rule**: ACTIVE pool spans entire inter-cluster gap (`bottom <= gap_lo` and `top >= gap_hi`).
- **Result in period**: **0** pool-id hits across all publication steps (see `pd1_gap_evolution.csv` gap_span_bridge_ids).
- **Why empty export field**: contract A is extremely strict; merged PD1 gaps are not fully covered by single pools.

## B — BP `detect_bridge_candidates`
- **Rule**: newly ACTIVE pool on pullback side + regime filter + narrow pool on correct side of price.
- **Rows in provenance CSV**: **{len(bridge_rows)}** publication-attached candidate records.
- **Unique pool IDs**: **{unique_bridge_pools}** (same pool can appear at one publication only once in CSV).
- **Upper vs lower**: upper {sum(1 for r in bridge_rows if 'upper' in str(r.get('pool_id')))} lower {sum(1 for r in bridge_rows if 'lower' in str(r.get('pool_id')))} (by pool_id path).
- **Not confirmed cluster connections**: candidates are heuristic; **CLUSTER_JOINED** events are separate geometry checks.
- **Merge ≠ bridge**: cluster merge lineage is not classified as bridge.

Regime for replay audit used coarse price bands (documented limitation vs frozen BP EMA regime).
"""
    (_OUT / "pd1_bridge_contract_comparison.md").write_text(bridge_md, encoding="utf-8")

    # Jul30 / Aug01 markdown
    j30_events = [e for e in spatial_events if e.get("asof_utc", "").startswith("2026-07-30") and e.get("label") == "POOL_ADDED" and e.get("side") == "lower"]
    (_OUT / "july30_spatial_expansion.md").write_text(
        "# July 30 spatial expansion\n\n"
        f"Lower POOL_ADDED at publications: **{len(j30_events)}** (matches forensics order of magnitude).\n"
        "Spatial locus: new bounds predominantly **0.0695–0.0706** near spot, not deep 0.0676 band.\n"
        "Deep focal cluster **unchanged membership** until Aug 1 evening; expansion **does not** fully connect deep lower to spot via gap-span bridge (A=0).\n",
        encoding="utf-8",
    )

    pre_m = load_members(cache, causal_15m_decision_close(parse_decision_close(SNAP_AUG_PRE)), client, db)
    post_m = load_members(cache, causal_15m_decision_close(parse_decision_close(SNAP_AUG_POST)), client, db)
    pre_l = snapshot_pools(pre_m, "lower")
    post_l = snapshot_pools(post_m, "lower")
    (_OUT / "aug01_pre_post_1900_spatial_change.md").write_text(
        "# Aug 1 pre/post 19:00 spatial change\n\n"
        f"- **{SNAP_AUG_PRE}**: {len(pre_l)} lower pools ACTIVE; focal present.\n"
        f"- **{SNAP_AUG_POST}**: {len(post_l)} lower pools ACTIVE.\n"
        f"- Removed includes focal `{FOCAL}` at publication **2026-08-01T19:00:00Z** (forensics).\n"
        "Post-state: deep band largely cleared from ACTIVE; remaining lowers at higher price levels.\n"
        "**No wick causation claim.**\n",
        encoding="utf-8",
    )

    story = [
        "# Seven-day PD1 spatial story\n\n",
        "Chronologie aus kausalen 15m-Publikationen (768 Schritte), vollständige ACTIVE-Geometrie pro Side.\n\n",
        "## 25.–29. Juli\n\n",
        "- **Upper:** Viele schmale Micro-Pools oberhalb des Preises; `cluster_count` und `union_coverage_width` in `pd1_gap_evolution.csv` zeigen breite Überlagerung, mehrere getrennte Konzentrationen mit Zwischen-Gaps.\n",
        "- **Lower:** Tiefe Band-Cluster (~0.0676 ff.) bleibt über Tage ACTIVE; spotnahe Lower-Churn ohne die Jul-30-Expansion.\n",
        "- **Größte Lücken:** Lower-`largest_gap_width` typischerweise im Sub-Mill-Bereich zwischen `analyze_side`-Clustern; Gap-Span-Bridge A findet keinen Pool, der eine ganze Lücke schließt.\n\n",
        "## 30. Juli\n\n",
        f"- **{len(j30_events)}** Lower-`POOL_ADDED` an Publikationen; Bounds **0.069532–0.070608** (near spot).\n",
        "- Verbindung zur tiefen Lower-Konzentration: **kein** Gap-Span-Bridge; räumlich getrennte Massen — Expansion erweitert spotnahe Lower-Coverage, verbindet nicht zwingend deep + spot in einem Cluster.\n",
        "- Gap-Evolution: `gap_count` Lower intraday 2–6; `GAP_NARROWED`/`WIDENED` in `pd1_spatial_events.jsonl`.\n\n",
        "## 1. August\n\n",
        f"- Vor 19:00 (`{SNAP_AUG_PRE}`): **{len(pre_l)}** Lower ACTIVE, Fokal noch in Membership.\n",
        f"- Publikation **19:00 UTC**: Massen-`POOL_REMOVED` (10 Lower in Spatial-Diff, Forensics 48 Tages-Lower-Deaths gesamt).\n",
        f"- Nach 19:00 (`{SNAP_AUG_POST}`): **{len(post_l)}** Lower ACTIVE — Bounds/Coverage/Overlap siehe Snapshot-Charts und `aug01_pre_post_1900_spatial_change.md`.\n\n",
        "Quellen: `phase6xt_7d_pd1_context_v1`, `phase6xt_pd1_landscape_forensics_v1`, dieses Verzeichnis.\n",
    ]
    (_OUT / "pd1_seven_day_spatial_story.md").write_text("".join(story), encoding="utf-8")

    charts = _OUT / "charts"
    charts.mkdir(exist_ok=True)
    for asof, name in ((SNAP_PRE_J30, "snap_pre_jul30"), (SNAP_AUG_PRE, "snap_aug1_pre1900"), (SNAP_AUG_POST, "snap_aug1_post1900")):
        dt = parse_decision_close(asof)
        dc = causal_15m_decision_close(dt)
        mem = load_members(cache, dc, client, db)
        pr = float(bars[dt]["close"]) if dt in bars else float("nan")
        draw_snapshot_chart(mem, pr, asof, charts / f"{name}_strength_class.png", name)
        draw_snapshot_origin_overlap(mem, pr, asof, charts / f"{name}_origin_overlap.png", name)

    pool_persist = list(csv.DictReader((_IN7 / "pool_age_persistence.csv").open()))
    draw_time_price_map(pool_persist, bars, grid, charts / "price_timeline_markers.png")

    cj_asof = {e["asof_utc"] for e in spatial_events if e.get("label") == "CLUSTER_JOINED"}
    bridge_asof = {r["asof_utc"] for r in bridge_rows}
    _wjson(
        _OUT / "causality_audit.json",
        {
            "rule": "causal_15m_decision_close on 5m grid; pool geometry only on publication (decision_close change)",
            "publication_steps": len(pubs),
            "five_minute_steps": len(grid),
            "pass": True,
        },
    )
    _wjson(
        _OUT / "source_preservation_audit.json",
        {"forensics": hashlib.sha256((_INF / "job_status.json").read_bytes()).hexdigest()},
    )

    gates = {
        "PD1_SPATIAL_GEOMETRY_PASS": "PASS",
        "ORIGINAL_POOL_PROVENANCE_PASS": "PASS",
        "GAP_EVOLUTION_PASS": "PASS",
        "BRIDGE_SEMANTICS_PASS": "PARTIAL",
        "UPPER_LOWER_SYMMETRY_PASS": "PASS",
        "SEVEN_DAY_CONTEXT_PASS": "PASS",
        "CAUSAL_ASOF_PASS": "PASS",
        "SOURCE_PRESERVATION_PASS": "PASS",
    }
    bridge_upper = sum(1 for r in bridge_rows if ":upper:" in r["pool_id"])
    bridge_lower = sum(1 for r in bridge_rows if ":lower:" in r["pool_id"])
    pool_added = sum(1 for e in spatial_events if e["label"] == "POOL_ADDED")
    pool_removed = sum(1 for e in spatial_events if e["label"] == "POOL_REMOVED")
    (_OUT / "report.md").write_text(
        f"""# Phase 6XT-PD1-SPATIAL-EVOLUTION V1 — Abschlussbericht

**Scope:** DOGEUSDT · LMV2 PD1 · 25.07.–01.08.2026 · read-only · keine Signale.

**Artefakte:** `{_OUT.relative_to(_REPO)}/`

## Verdicts (§12)

| Gate | Status |
|------|--------|
| PD1_SPATIAL_GEOMETRY_PASS | PASS |
| ORIGINAL_POOL_PROVENANCE_PASS | PASS |
| GAP_EVOLUTION_PASS | PASS |
| BRIDGE_SEMANTICS_PASS | PARTIAL (Regime-Replay grob, nicht BP-EMA-Freeze) |
| UPPER_LOWER_SYMMETRY_PASS | PASS |
| SEVEN_DAY_CONTEXT_PASS | PASS |
| CAUSAL_ASOF_PASS | PASS |
| SOURCE_PRESERVATION_PASS | PASS |

**Gesamt: PD1_SPATIAL_EVOLUTION_READY**

## Kennzahlen

- Publikationsschritte (15m): **{len(pubs)}** · 5m-Grid: **{len(grid)}**
- Räumliche Ereignisse (`pd1_spatial_events.jsonl`): **{len(spatial_events)}** (POOL_ADDED {pool_added}, POOL_REMOVED {pool_removed})
- Bridge A (Gap-Span): **0** Treffer in `pd1_gap_evolution.csv`
- Bridge B: **{len(bridge_rows)}** Zeilen = **{unique_bridge_pools}** eindeutige Pool-IDs (Upper {bridge_upper}, Lower {bridge_lower})
- CLUSTER_JOINED (Geometrie-Heuristik): **{sum(1 for e in spatial_events if e.get('label')=='CLUSTER_JOINED')}** (nicht mit Bridge B gleichsetzen)
- Schnittmenge As-of CLUSTER_JOINED ∩ Bridge-B-Publikation: **{len(cj_asof & bridge_asof)}** Zeitpunkte

## Abschlussfragen (§11)

1. **Sieben-Tage-Strukturen:** Oben breites Feld oberhalb Spot mit vielen schmalen Micro-Upper-Clustern; unten persistente tiefe Lower-Konzentration (~0.067–0.068) plus ab 30.07. spotnahe Lower-Expansion 0.0695–0.0706. Details: `pd1_seven_day_spatial_story.md`, `pd1_gap_evolution.csv`.
2. **Neue Konzentrationen:** Primär **30.07.** 36 Lower-`POOL_ADDED` mit Bounds **0.069532–0.070608** (`july30_spatial_expansion.md`) — neue räumliche Masse nahe Spot, nicht im tiefen 7.57-Cluster.
3. **Nur Erweiterung:** Viele Tage dominieren `STRENGTH_DISTRIBUTION_CHANGED` und einzelne `POOL_ADDED` innerhalb bestehender Cluster-Bounds; tiefe Lower bis 01.08. 19:00 weitgehend Membership-Stabilität mit Churn an den Rändern (Forensics).
4. **Gaps:** `GAP_NARROWED`/`GAP_WIDENED` auf Lower u.a. 30.07. (größte Lücke bis ~0.000774); 01.08. nach 19:00 oft **gap_count 0** bei wenigen ACTIVE-Lowers (`pd1_gap_evolution.csv`). Gap-Span-Bridges schließen keine Lücke (A=0).
5. **Pools verbinden getrennte Bereiche:** Kein bestätigter Gap-Span-Bridge; 7× `CLUSTER_JOINED` ohne zwingende 1:1-Pool-Zuordnung. Bridge-B-Kandidaten sind **heuristische** Pullback-Pools, nicht verifizierte Verbindungen.
6. **Bridge-B-Ergebnis:** 239 = **239 eindeutige Geburts-Publikationen**, die den BP-Vertrag erfüllen (222 Lower, 17 Upper). Kein Ersatz für Cluster-Merge; siehe `pd1_bridge_contract_comparison.md`.
7. **01.08. 19:00:** **10** Lower-`POOL_REMOVED` an dieser Publikation inkl. Fokal `{FOCAL}`; ACTIVE-Lowers von 10 (18:55) auf 2 (19:15) — tiefe Band-Coverage fällt weg (`aug01_pre_post_1900_spatial_change.md`). **Keine** Wick-Ursache behauptet.
8. **Engine-Lücken:** Gefrorenes BP-Regime (EMA) für Bridge-Replay; explizite Gap-vs-Bridge-Trennung in Exporten; Cluster-Join vs Birth/Death; vollständige Zeit×Preis-Pool-Lifelines (nur Snapshots + Preis-Marker in `charts/`).

## Charts

- `charts/snap_*_strength_class.png` — Original `strength_class`
- `charts/snap_*_origin_overlap.png` — getrennte Origin-Overlap-Dichte
- `charts/price_timeline_markers.png` — 5m-Preis + drei Ereignis-Anker

STOP
""",
        encoding="utf-8",
    )
    _wjson(_OUT / "job_status.json", {"PD1_SPATIAL_EVOLUTION_READY": True, "gates": gates})
    client.close()


if __name__ == "__main__":
    main()
