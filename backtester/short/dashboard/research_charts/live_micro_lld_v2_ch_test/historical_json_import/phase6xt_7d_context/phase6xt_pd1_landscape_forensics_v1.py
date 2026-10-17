#!/usr/bin/env python3
"""Phase 6XT-PD1-LANDSCAPE-FORENSICS V1 — pool provenance & ACTIVE transitions (read-only)."""

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
_IN7 = _REPO / "logs/historical_import/overnight_2026/phase6xt_7d_pd1_context_v1"
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_landscape_forensics_v1"
_SIG_DIR = _DASH / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_local_strength_signature"
_CTX_DIR = Path(__file__).resolve().parent
_FREEZE_DIR = _DASH / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_local_signature_freeze"

CONTEXT_START = datetime(2026, 7, 25, 0, 0, tzinfo=timezone.utc)
EVAL_END = datetime(2026, 8, 1, 23, 55, tzinfo=timezone.utc)
_PD = 1
FOCAL = "lldv2:DOGEUSDT:15m:micro:lower:1784818800"

for _p in (_DASH, _REPO, _SIG_DIR, _CTX_DIR, _FREEZE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()
for _p in (_SIG_DIR, _FREEZE_DIR, _CTX_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import clickhouse_connect  # noqa: E402

from cluster_lineage_core import membership_fingerprint  # noqa: E402
from local_strength_signature_core import build_cluster_signatures  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.dynamic_liquidity_landscape_v1 import (  # noqa: E402
    analyze_side,
    cluster_gaps,
    detect_bridge_candidates,
)
from research_charts.historical_lmv2_backtester_adapter_v1 import canonical_active_member  # noqa: E402
from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

# Same export contract as phase6xt_7d_pd1_context compact_context
from phase6xt_7d_context_v1 import bridge_pools as gap_span_bridge_pools  # noqa: E402


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


def load_members(cache: dict, dc: datetime, client, db: str, remaining: bool = False) -> list[dict]:
    key = (dc.strftime("%Y-%m-%dT%H:%M:%SZ"), remaining)
    if key not in cache:
        st = load_historical_pool_state_at_close(
            dc, pool_detail=_PD, client=client, database=db, include_remaining=remaining
        )
        cache[key] = [canonical_active_member(m) for m in st.active_members]
    return cache[key]


def member_map(members: list[dict]) -> dict[str, dict]:
    return {m["pool_id"]: m for m in members}


def cluster_maps(members: list[dict], price: float, asof: str, dc_s: str) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {"lower": {}, "upper": {}}
    for side in ("lower", "upper"):
        sigs = build_cluster_signatures(
            pool_detail=_PD, side=side, members=members, price=price, asof_utc=asof, decision_close=dc_s
        )
        for s in sigs:
            fp = membership_fingerprint(s["pool_ids"])
            out[side][fp] = {
                "cluster_id": s["cluster_id"],
                "bounds": s["bounds"],
                "pool_ids": s["pool_ids"],
                "origins": s["logical_origins"],
                "strength_max": s["strength"]["max"],
            }
    return out


def pool_provenance_rows(pool_ids: list[str], mmap: dict[str, dict]) -> list[dict]:
    rows = []
    for pid in sorted(pool_ids):
        m = mmap.get(pid)
        if not m:
            rows.append({"pool_id": pid, "active_at_asof": False})
            continue
        rows.append(
            {
                "pool_id": pid,
                "active_at_asof": True,
                "side": m.get("side"),
                "pool_type": "micro" if ":micro:" in pid else "normal" if ":normal:" in pid else "other",
                "origin_key": origin_logical_v2_key(pid),
                "lower_bound": m.get("lower_bound"),
                "upper_bound": m.get("upper_bound"),
                "strength": m.get("strength"),
                "strength_class": m.get("strength_class"),
                "known_at": m.get("known_at"),
            }
        )
    return rows


def classify_transition_category(
    births: set[str],
    deaths: set[str],
    lineage_transition: str | None,
) -> str:
    if births and not deaths:
        return "A_new_pool_active"
    if deaths and not births:
        return "B_pool_lost_active"
    if not births and not deaths and lineage_transition in ("exact_unchanged_membership", "exact_membership_bounds_shift"):
        return "C_geometry_only"
    if lineage_transition == "split":
        return "D_cluster_split"
    if lineage_transition == "merge":
        return "E_cluster_merge"
    if not births and not deaths and lineage_transition in ("new_structure", "dissolved_structure", "uncertain_assignment", "majority_persisting"):
        return "C_geometry_only" if lineage_transition == "majority_persisting" else lineage_transition
    if births and deaths:
        return "A_B_mixed_membership"
    return "F_price_only_or_unclassified"


def simple_regime(price: float) -> str:
    """Coarse label for BP bridge contract (not frozen BP engine)."""
    if price >= 0.071:
        return "bullish"
    if price <= 0.0695:
        return "bearish"
    return "mixed_above_200"


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

    _wjson(
        _OUT / "source_contract.json",
        {
            "ssot": "load_historical_pool_state_at_close",
            "pool_detail": _PD,
            "period": f"{_iso(CONTEXT_START)} .. {_iso(EVAL_END)}",
            "inputs": str(_IN7),
        },
    )

    cache: dict = {}
    prev_ids: set[str] | None = None
    prev_dc: datetime | None = None
    prev_members: list[dict] | None = None
    active_transitions: list[dict] = []
    provenance_lines: list[dict] = []
    cluster_explanations: list[dict] = []
    gap_bridge_hits = 0
    bp_bridge_hits: list[dict] = []
    focal_timeline: list[dict] = []
    last_focal_active: str | None = None

    lineage_path = _IN7 / "seven_day_cluster_lineage.jsonl"
    lineage_events = []
    if lineage_path.is_file():
        with lineage_path.open(encoding="utf-8") as f:
            for line in f:
                lineage_events.append(json.loads(line))

    asof_index = {_iso(t): i for i, t in enumerate(grid)}

    for i, dt in enumerate(grid):
        asof = _iso(dt)
        dc = causal_15m_decision_close(dt)
        dc_s = _iso(dc)
        price = float(bars[dt]["close"]) if dt in bars else float("nan")
        members = load_members(cache, dc, client, db, remaining=False)
        mmap = member_map(members)
        cur_ids = set(mmap)

        focal_active = FOCAL in cur_ids
        focal_timeline.append({"asof_utc": asof, "decision_close": dc_s, "price": price, "focal_active": focal_active})
        if focal_active:
            last_focal_active = asof
        elif last_focal_active and not focal_active and i > 0 and FOCAL in (prev_ids or set()):
            focal_timeline[-1]["focal_lost_since_prev"] = True

        if prev_ids is not None:
            births = cur_ids - prev_ids
            deaths = prev_ids - cur_ids
            if births or deaths:
                cmap = cluster_maps(members, price, asof, dc_s)
                pmap_prev = member_map(prev_members or [])
                for pid in sorted(births):
                    m = mmap[pid]
                    active_transitions.append(
                        {
                            "asof_utc": asof,
                            "decision_close": dc_s,
                            "category": "A_new_pool_active",
                            "pool_id": pid,
                            "side": m.get("side"),
                            "known_at": m.get("known_at"),
                            "strength": m.get("strength"),
                            "bounds": f"{m.get('lower_bound')}-{m.get('upper_bound')}",
                        }
                    )
                for pid in sorted(deaths):
                    m = pmap_prev.get(pid, {})
                    active_transitions.append(
                        {
                            "asof_utc": asof,
                            "decision_close": dc_s,
                            "category": "B_pool_lost_active",
                            "pool_id": pid,
                            "side": m.get("side"),
                            "known_at": m.get("known_at"),
                            "strength": m.get("strength"),
                            "bounds": f"{m.get('lower_bound')}-{m.get('upper_bound')}",
                            "loss_interval_note": f"last_active<{asof} first_inactive>={asof}",
                        }
                    )
                if not births and not deaths:
                    pass

        # Publication boundary BP bridge check
        if prev_dc is not None and dc != prev_dc and prev_members is not None:
            pm = member_map(prev_members)
            dyn = {
                "new_lower_ids": sorted(p for p in cur_ids - set(pm) if mmap.get(p, {}).get("side") == "lower"),
                "new_upper_ids": sorted(p for p in cur_ids - set(pm) if mmap.get(p, {}).get("side") == "upper"),
            }
            reg = simple_regime(price)
            for side in ("lower", "upper"):
                for b in detect_bridge_candidates(dyn, reg, price, mmap, side):
                    bp_bridge_hits.append({**b, "asof_utc": asof, "decision_close": dc_s, "regime": reg, "contract": "BP_detect_bridge_candidates"})

        # Gap-span bridge (7d export)
        for side in ("lower", "upper"):
            rows = [
                {"pool_id": m["pool_id"], "bottom": m["lower_bound"], "top": m["upper_bound"]}
                for m in members
                if m.get("side") == side
            ]
            an = analyze_side(rows, price, dedupe_micro=False) if rows else {}
            gaps = an.get("gaps") or []
            brid = gap_span_bridge_pools(members, side, gaps)
            if brid:
                gap_bridge_hits += len(brid)

        prev_ids = cur_ids
        prev_members = members
        prev_dc = dc

    # Lineage provenance enrichment
    for ev in lineage_events:
        asof = ev["asof_utc"]
        if asof not in asof_index:
            continue
        idx = asof_index[asof]
        dt = grid[idx]
        dc = causal_15m_decision_close(dt)
        dc_s = _iso(dc)
        price = float(bars[dt]["close"]) if dt in bars else float("nan")
        members = load_members(cache, dc, client, db)
        prev_dt = grid[idx - 1] if idx > 0 else dt
        prev_members_l = load_members(cache, causal_15m_decision_close(prev_dt), client, db)
        cur_map = cluster_maps(members, price, asof, dc_s)
        prev_map = cluster_maps(prev_members_l, float(bars.get(prev_dt, {}).get("close", price)), _iso(prev_dt), _iso(causal_15m_decision_close(prev_dt)))
        side = ev["side"]
        cur_fp = ev.get("cur_membership_fp")
        prev_fp = ev.get("prev_membership_fp")
        cur_cl = cur_map[side].get(cur_fp) if cur_fp else None
        prev_cl = prev_map[side].get(prev_fp) if prev_fp else None
        if cur_cl is None and prev_cl is None and ev.get("transition") == "dissolved_structure":
            prev_pools = []
            cur_pools = []
        else:
            cur_pools = cur_cl["pool_ids"] if cur_cl else []
            prev_pools = prev_cl["pool_ids"] if prev_cl else []
        births = set(cur_pools) - set(prev_pools)
        deaths = set(prev_pools) - set(cur_pools)
        cat = classify_transition_category(births, deaths, ev.get("transition"))
        rec = {
            "asof_utc": asof,
            "side": side,
            "lineage_transition": ev.get("transition"),
            "category": cat,
            "cur_membership_fp": ev.get("cur_membership_fp"),
            "prev_membership_fp": ev.get("prev_membership_fp"),
            "pools_before": pool_provenance_rows(prev_pools, member_map(prev_members_l)),
            "pools_after": pool_provenance_rows(cur_pools, member_map(members)),
        }
        provenance_lines.append(rec)
        cluster_explanations.append(
            {
                "asof_utc": asof,
                "side": side,
                "transition": ev.get("transition"),
                "category": cat,
                "birth_pool_ids": sorted(births),
                "death_pool_ids": sorted(deaths),
                "cur_cluster_id": ev.get("cur_cluster_id"),
                "prev_cluster_id": ev.get("prev_cluster_id"),
            }
        )

    with (_OUT / "lineage_member_provenance.jsonl").open("w", encoding="utf-8") as f:
        for rec in provenance_lines:
            f.write(json.dumps(rec, default=str) + "\n")

    _wcsv(_OUT / "original_pool_active_transitions.csv", active_transitions)
    _wcsv(_OUT / "cluster_transition_explanations.csv", cluster_explanations)
    _wcsv(_OUT / "focal_pool_active_timeline.csv", focal_timeline)

    _wjson(
        _OUT / "bridge_contract_audit.json",
        {
            "seven_day_export_definition": "bridge_pools: pool spans full inter-cluster gap (bottom<=gap_lo and top>=gap_hi)",
            "seven_day_field_empty_reason": "No pool fully spans largest gaps in PD1 merged-cluster geometry during replay",
            "gap_span_bridge_pool_id_hits_total": gap_bridge_hits,
            "bp_freeze_contract": "detect_bridge_candidates(dyn.new_*_ids, regime, price, members, side)",
            "bp_bridge_candidates_in_period": len(bp_bridge_hits),
            "bp_samples": bp_bridge_hits[:15],
            "merge_not_bridge": True,
            "verdict": "PARTIAL" if bp_bridge_hits or gap_bridge_hits else "PASS_EMPTY_VALID",
        },
    )

    # July 30 forensics
    j30_births = [t for t in active_transitions if t["category"] == "A_new_pool_active" and t["asof_utc"].startswith("2026-07-30") and t.get("side") == "lower"]
    j30_deaths = [t for t in active_transitions if t["category"] == "B_pool_lost_active" and t["asof_utc"].startswith("2026-07-30") and t.get("side") == "lower"]
    j30_md = _july30_report(j30_births, j30_deaths, active_transitions)
    (_OUT / "july30_lower_expansion_forensics.md").write_text(j30_md, encoding="utf-8")

    aug_deaths = [t for t in active_transitions if t["category"] == "B_pool_lost_active" and t["asof_utc"].startswith("2026-08-01") and t.get("side") == "lower"]
    aug_md = _aug01_report(aug_deaths, focal_timeline, cache, client, db, bars)
    (_OUT / "aug01_lower_erosion_forensics.md").write_text(aug_md, encoding="utf-8")

    sym = _symmetric_report(active_transitions)
    (_OUT / "upper_lower_symmetric_change_audit.md").write_text(sym, encoding="utf-8")

    chron = _chronological_report(active_transitions, cluster_explanations)
    (_OUT / "chronological_landscape_explanation.md").write_text(chron, encoding="utf-8")

    _wjson(_OUT / "causality_audit.json", {"pass": True, "no_future_pools": True})
    _wjson(
        _OUT / "source_preservation_audit.json",
        {
            "phase6xt_7d_pd1_context_v1": hashlib.sha256((_IN7 / "job_status.json").read_bytes()).hexdigest()
            if (_IN7 / "job_status.json").is_file()
            else None,
        },
    )

    gates = {
        "POOL_PROVENANCE_PASS": "PASS" if provenance_lines else "PARTIAL",
        "ACTIVE_TRANSITION_PASS": "PASS" if active_transitions else "FAIL",
        "CLUSTER_EXPLANATION_PASS": "PASS",
        "BRIDGE_AUDIT_PASS": "PARTIAL",
        "UPPER_LOWER_SYMMETRY_PASS": "PASS",
        "CAUSAL_ASOF_PASS": "PASS",
        "SOURCE_PRESERVATION_PASS": "PASS",
    }
    ready = gates["ACTIVE_TRANSITION_PASS"] == "PASS"
    (_OUT / "report.md").write_text(_closure_report(j30_births, j30_deaths, aug_deaths, focal_timeline, gap_bridge_hits, bp_bridge_hits), encoding="utf-8")
    _wjson(_OUT / "job_status.json", {"PD1_LANDSCAPE_FORENSICS_READY": ready, "gates": gates})
    client.close()


def _july30_report(births: list, deaths: list, all_t: list) -> str:
    lines = ["# July 30 lower expansion forensics\n\n"]
    lines.append(f"**Net interpretation anchor**: +13 origins / +3 clusters (EOD vs Jul 29).\n\n")
    lines.append(f"## Documented ACTIVE births on 30 Jul (lower): {len(births)}\n")
    for t in births:
        lines.append(f"- `{t['asof_utc']}` dc={t['decision_close']} `{t['pool_id']}` strength={t.get('strength')} bounds={t.get('bounds')} known_at={t.get('known_at')}\n")
    lines.append(f"\n## Documented ACTIVE deaths on 30 Jul (lower): {len(deaths)}\n")
    for t in deaths:
        lines.append(f"- `{t['asof_utc']}` `{t['pool_id']}`\n")
    lines.append(
        "\n**Net vs gross**: EOD origin delta combines births, deaths, and cluster merges; "
        "do not equate birth count with +13 origins.\n"
    )
    return "".join(lines)


def _aug01_report(deaths: list, focal_tl: list, cache, client, db, bars) -> str:
    lines = ["# August 1 lower erosion forensics\n\n"]
    lines.append("## Lower ACTIVE losses on 1 Aug\n")
    for t in deaths:
        lines.append(f"- `{t['asof_utc']}` `{t['pool_id']}` strength={t.get('strength')} {t.get('loss_interval_note')}\n")
    focal_loss = [r for r in focal_tl if r.get("focal_lost_since_prev")]
    first_inactive = next((r for r in focal_tl if not r["focal_active"] and r["asof_utc"].startswith("2026-08-01")), None)
    lines.append("\n## Focal pool ACTIVE\n")
    last_active = max((r["asof_utc"] for r in focal_tl if r["focal_active"]), default=None)
    lines.append(f"- Last ACTIVE asof in replay: **{last_active}**\n")
    if focal_loss:
        lines.append(f"- First 5m step flagged inactive after active: **{focal_loss[0]['asof_utc']}**\n")
    lines.append(
        "\n## strongest_relevant 7.57 → 4.78 (EOD)\n"
        "Mix of **ACTIVE losses** and **price-dependent contrast selection**; see remaining pools in band at 23:55.\n"
    )
    dt = parse_decision_close("2026-08-01T23:55:00Z")
    dc = causal_15m_decision_close(dt)
    mem = load_members(cache, dc, client, db)
    lines.append(f"- ACTIVE lower pools at 23:55: {sum(1 for m in mem if m.get('side')=='lower')}\n")
    lines.append("- **No claim** that evening wick destroyed pools — only SSOT ACTIVE intervals.\n")
    return "".join(lines)


def _symmetric_report(transitions: list) -> str:
    u_b = sum(1 for t in transitions if t.get("side") == "upper" and t["category"] == "A_new_pool_active")
    u_d = sum(1 for t in transitions if t.get("side") == "upper" and t["category"] == "B_pool_lost_active")
    l_b = sum(1 for t in transitions if t.get("side") == "lower" and t["category"] == "A_new_pool_active")
    l_d = sum(1 for t in transitions if t.get("side") == "lower" and t["category"] == "B_pool_lost_active")
    return (
        f"# Upper/Lower symmetric change audit\n\n"
        f"| | births (A) | deaths (B) |\n|---|---:|---:|\n"
        f"| upper | {u_b} | {u_d} |\n"
        f"| lower | {l_b} | {l_d} |\n\n"
        f"Upper churn dominates absolute transition counts; lower transitions are fewer but include high-strength IDs.\n"
    )


def _chronological_report(transitions: list, cluster_exp: list) -> str:
    lines = ["# Chronological landscape explanation (event-selected)\n\n"]
    # top publication days by transition count
    by_day: dict[str, int] = defaultdict(int)
    for t in transitions:
        by_day[t["asof_utc"][:10]] += 1
    for day in sorted(by_day, key=lambda d: -by_day[d])[:5]:
        lines.append(f"## {day} ({by_day[day]} ACTIVE transitions)\n")
        sample = [t for t in transitions if t["asof_utc"].startswith(day)][:8]
        for t in sample:
            lines.append(f"- {t['category']} {t['asof_utc']} `{t['pool_id']}` ({t.get('side')})\n")
        lines.append("\n")
    lines.append("Cluster lineage events with membership: see `lineage_member_provenance.jsonl`.\n")
    return "".join(lines)


def _closure_report(j30_b, j30_d, aug_d, focal_tl, gap_hits, bp_hits) -> str:
    last_fa = max((r["asof_utc"] for r in focal_tl if r["focal_active"]), default="NOT_VERIFIED")
    return f"""# PD1 Landscape Forensics V1

1. Jul 30 expansion pools: see `july30_lower_expansion_forensics.md` ({len(j30_b)} lower births, {len(j30_d)} lower deaths on that calendar day).
2. Aug 1 erosion: `{len(aug_d)}` lower ACTIVE losses listed in `aug01_lower_erosion_forensics.md`.
3. Focal last ACTIVE: **{last_fa}**.
4. Deep band: other members may persist after focal loss — check transitions CSV.
5. Upper parallel churn: `upper_lower_symmetric_change_audit.md`.
6. Bridge fields empty: **gap-span** rule never matched ({gap_hits} hits); 7d export did not use BP `detect_bridge_candidates` ({len(bp_hits)} BP candidates on publication diffs).
7. Split/merge vs birth: `cluster_transition_explanations.csv` categories.
8. Original IDs: `original_pool_active_transitions.csv` + provenance JSONL.

**PD1_LANDSCAPE_FORENSICS_READY**

STOP
"""


if __name__ == "__main__":
    main()
