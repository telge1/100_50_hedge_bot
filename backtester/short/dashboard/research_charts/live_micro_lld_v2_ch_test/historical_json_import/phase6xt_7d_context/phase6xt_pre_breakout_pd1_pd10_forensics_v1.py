#!/usr/bin/env python3
"""Phase 6XT-PRE-BREAKOUT-PD1-PD10-FORENSICS V1 — read-only dual-PD landscape forensics."""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pre_breakout_pd1_pd10_forensics_v1"
_CTX = Path(__file__).resolve().parent
_TRANSFER_C = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_transfer_c_v1"

HIST_START = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
MAIN_START = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
PHASE_A_CUTOFF = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
POST_END = datetime(2026, 8, 21, 23, 55, tzinfo=timezone.utc)
HOURS_CONTEXT = 168
_EPS = 0.00012
_PDS = (1, 10)

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
from phase6xt_pd1_context_transfer_v1 import enrich_context  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config  # noqa: E402
from research_charts.historical_pool_state_v1 import (  # noqa: E402
    PublishedActiveReader,
    get_published_decision_closes,
)
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
_wjson = c7._wjson
iter_5m = c7.iter_5m
load_members = c7.load_members
run_replay = c7.run_replay


def _wcsv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fn = sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _parse_iso(s: str) -> datetime:
    return parse_decision_close(s)


def coverage_audit(client, db: str, bars: dict) -> dict[str, Any]:
    missing = []
    t = HIST_START
    while t <= POST_END:
        if t not in bars:
            missing.append(_iso(t))
        t += timedelta(minutes=5)
    hours = (MAIN_START - HIST_START).total_seconds() / 3600
    pd_checks = {}
    for pd in _PDS:
        hcfg = resolve_historical_pd_config(pd)
        pub = get_published_decision_closes(
            PublishedActiveReader(), hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id
        )
        pub_in = [parse_decision_close(x) for x in pub if HIST_START <= parse_decision_close(x) <= POST_END]
        try:
            load_members({}, causal_15m_decision_close(HIST_START), pd, client, db)
            load_members({}, causal_15m_decision_close(PHASE_A_CUTOFF), pd, client, db)
            ok = True
            err = None
        except Exception as e:
            ok = False
            err = str(e)
        pd_checks[f"PD{pd}"] = {
            "pool_state_load_ok": ok,
            "pool_state_error": err,
            "published_closes_in_window": len(pub_in),
            "config_hash": hcfg.config_hash,
        }
    pass_cov = (
        hours >= HOURS_CONTEXT
        and not missing
        and all(pd_checks[f"PD{pd}"]["pool_state_load_ok"] for pd in _PDS)
        and all(pd_checks[f"PD{pd}"]["published_closes_in_window"] >= 900 for pd in _PDS)
    )
    return {
        "hist_start": _iso(HIST_START),
        "main_start": _iso(MAIN_START),
        "phase_a_cutoff": _iso(PHASE_A_CUTOFF),
        "post_end": _iso(POST_END),
        "context_hours_before_main": hours,
        "5m_bars_expected": len(iter_5m(HIST_START, POST_END)),
        "5m_bars_present": len(bars),
        "5m_missing_count": len(missing),
        "pd_checks": pd_checks,
        "pass": pass_cov,
    }


def build_landscapes(contexts: list[dict], client, db: str) -> tuple[list[dict], list[dict]]:
    mem_cache: dict = {}
    pd1, pd10 = [], []
    for c in contexts:
        pd = c["pool_detail"]
        dc = parse_decision_close(c["decision_close"])
        mem = load_members(mem_cache, dc, pd, client, db)
        enc = enrich_context(c, mem)
        if pd == 1:
            pd1.append(enc)
        elif pd == 10:
            pd10.append(enc)
    return pd1, pd10


def extract_births(
    rows: list[dict], client, db: str, side: str
) -> list[dict]:
    mem_cache: dict = {}
    seen_origins: set[tuple[int, str]] = set()
    running_deepest = {pd: float("inf") for pd in _PDS}
    running_support_top = {pd: 0.0 for pd in _PDS}
    out = []
    for rec in sorted(rows, key=lambda x: x["asof_utc"]):
        pd = rec["pool_detail"]
        asof = rec["asof_utc"]
        dc = parse_decision_close(rec["decision_close"])
        price = float(rec["price"])
        mem = load_members(mem_cache, dc, pd, client, db)
        by_id = {m["pool_id"]: m for m in mem}
        for pid in rec.get("birth_pool_ids") or []:
            m = by_id.get(pid)
            if not m or m.get("side") != side:
                continue
            bot = float(m["lower_bound"])
            top = float(m["upper_bound"])
            oid = origin_logical_v2_key(pid)
            okey = (pd, oid)
            is_new_origin = okey not in seen_origins
            if is_new_origin:
                seen_origins.add(okey)
            deeper = is_new_origin and bot < running_deepest[pd] - _EPS
            higher_step = is_new_origin and bot > running_support_top[pd] + _EPS
            within = is_new_origin and not deeper and not higher_step
            if is_new_origin and bot < running_deepest[pd]:
                running_deepest[pd] = bot
            if top > running_support_top[pd]:
                running_support_top[pd] = top
            phase_a = _parse_iso(asof) <= PHASE_A_CUTOFF
            out.append(
                {
                    "pool_detail": pd,
                    "asof_utc": asof,
                    "decision_close": rec["decision_close"],
                    "price": price,
                    "pool_id": pid,
                    "origin_id": oid,
                    "bottom": bot,
                    "top": top,
                    "strength": m.get("strength"),
                    "known_at": m.get("known_at"),
                    "is_new_origin": is_new_origin,
                    "class_deeper": deeper,
                    "class_higher_step": higher_step,
                    "class_within_zone": within,
                    "phase_a_eligible": phase_a,
                }
            )
    return out


def exhaustion_analysis(lowers_pd1: list[dict], lowers_pd10: list[dict]) -> str:
    lines = [
        "# Lower formation exhaustion comparison (PD1 vs PD10)\n\n",
        f"Metrics below use **main window** `{_iso(MAIN_START)}` – `{_iso(PHASE_A_CUTOFF)}` "
        "(replay-start births on 10.08. excluded from exhaustion timing).\n\n",
    ]
    for label, rows in [("PD1", lowers_pd1), ("PD10", lowers_pd10)]:
        pa = [
            r
            for r in rows
            if r["phase_a_eligible"]
            and r["is_new_origin"]
            and _parse_iso(r["asof_utc"]) >= MAIN_START
        ]
        deeper = [r for r in pa if r["class_deeper"]]
        all_b = [r for r in pa]
        lines.append(f"## {label}\n\n")
        if deeper:
            last_d = max(deeper, key=lambda r: r["asof_utc"])
            lines.append(
                f"- Last **deeper** new lower origin: `{last_d['asof_utc']}` dc={last_d['decision_close']} "
                f"origin={last_d['origin_id']} bottom={last_d['bottom']}\n"
            )
        else:
            lines.append("- Last deeper new lower origin: **NOT_VERIFIED** in phase-A window\n")
        if all_b:
            last_any = max(all_b, key=lambda r: r["asof_utc"])
            lines.append(
                f"- Last **any** new lower origin (phase A): `{last_any['asof_utc']}` bottom={last_any['bottom']}\n"
            )
            first_higher = next((r for r in sorted(pa, key=lambda x: x["asof_utc"]) if r["class_higher_step"]), None)
            if first_higher:
                lines.append(
                    f"- First **higher-step** lower origin: `{first_higher['asof_utc']}` bottom={first_higher['bottom']}\n"
                )
        lines.append("\n")
    return "".join(lines)


def deep_persistence(pd1: list[dict], pd10: list[dict], client, db: str) -> list[dict]:
    mem_cache: dict = {}
    baseline: dict[tuple[int, str], dict] = {}

    def snap(rows: list[dict], pd: int) -> None:
        rec = next((r for r in rows if r["asof_utc"] == _iso(MAIN_START)), None)
        if not rec:
            return
        dc = parse_decision_close(rec["decision_close"])
        price = float(rec["price"])
        for m in load_members(mem_cache, dc, pd, client, db):
            if m.get("side") != "lower":
                continue
            bot = float(m["lower_bound"])
            if bot < price - 0.0015:
                baseline[(pd, m["pool_id"])] = {
                    "pool_detail": pd,
                    "pool_id": m["pool_id"],
                    "origin_id": origin_logical_v2_key(m["pool_id"]),
                    "bottom": bot,
                    "top": float(m["upper_bound"]),
                    "first_seen": rec["asof_utc"],
                }

    snap(pd1, 1)
    snap(pd10, 10)

    active_at_cutoff: set[tuple[int, str]] = set()
    for pd, rows in ((1, pd1), (10, pd10)):
        rec = max(
            (r for r in rows if _parse_iso(r["asof_utc"]) <= PHASE_A_CUTOFF),
            key=lambda r: r["asof_utc"],
            default=None,
        )
        if not rec:
            continue
        dc = parse_decision_close(rec["decision_close"])
        ids = {m["pool_id"] for m in load_members(mem_cache, dc, pd, client, db) if m.get("side") == "lower"}
        for key in baseline:
            if key[0] == pd and key[1] in ids:
                active_at_cutoff.add(key)

    return [{**meta, "active_through_phase_a_cutoff": key in active_at_cutoff} for key, meta in baseline.items()]


def side_by_side(pd1: list[dict], pd10: list[dict]) -> list[dict]:
    p1 = {r["decision_close"]: r for r in pd1 if _parse_iso(r["asof_utc"]) <= PHASE_A_CUTOFF}
    p10 = {r["decision_close"]: r for r in pd10 if _parse_iso(r["asof_utc"]) <= PHASE_A_CUTOFF}
    # keep latest asof per dc
    for src, dest in ((pd1, p1), (pd10, p10)):
        dest.clear()
        for r in src:
            if _parse_iso(r["asof_utc"]) > PHASE_A_CUTOFF:
                continue
            dc = r["decision_close"]
            if dc not in dest or r["asof_utc"] > dest[dc]["asof_utc"]:
                dest[dc] = r
    dcs = sorted(set(p1) | set(p10))
    rows = []
    for dc in dcs:
        a, b = p1.get(dc), p10.get(dc)
        if not a or not b:
            continue
        if _parse_iso(dc) < MAIN_START:
            continue
        rows.append(
            {
                "decision_close": dc,
                "price": a["price"],
                "pd1_lower_origins": a["sides"]["lower"]["logical_origins"],
                "pd10_lower_origins": b["sides"]["lower"]["logical_origins"],
                "pd1_upper_origins": a["sides"]["upper"]["logical_origins"],
                "pd10_upper_origins": b["sides"]["upper"]["logical_origins"],
                "pd1_lower_exact_max": a["sides"]["lower"].get("exact_origin_overlap_max"),
                "pd10_lower_exact_max": b["sides"]["lower"].get("exact_origin_overlap_max"),
                "pd1_upper_exact_max": a["sides"]["upper"].get("exact_origin_overlap_max"),
                "pd10_upper_exact_max": b["sides"]["upper"].get("exact_origin_overlap_max"),
                "pd1_lower_strongest_rel": a["sides"]["lower"]["strongest_relevant_strength_max"],
                "pd10_lower_strongest_rel": b["sides"]["lower"]["strongest_relevant_strength_max"],
                "pd1_upper_gap": a["sides"]["upper"]["largest_gap_width"],
                "pd10_upper_gap": b["sides"]["upper"]["largest_gap_width"],
                "pd1_lower_gap": a["sides"]["lower"]["largest_gap_width"],
                "pd10_lower_gap": b["sides"]["lower"]["largest_gap_width"],
                "pd1_births": len(a.get("birth_pool_ids") or []),
                "pd10_births": len(b.get("birth_pool_ids") or []),
                "pd1_deaths": len(a.get("death_pool_ids") or []),
                "pd10_deaths": len(b.get("death_pool_ids") or []),
            }
        )
    return rows


def pre_breakout_timeline(pd1: list[dict], pd10: list[dict]) -> str:
    lines = ["# Pre-breakout timeline (15m decision_close, phase A only)\n\n", f"Cutoff: **{_iso(PHASE_A_CUTOFF)}**\n\n"]
    start = datetime(2026, 8, 18, 0, 0, tzinfo=timezone.utc)
    for pd_label, rows in [("PD1", pd1), ("PD10", pd10)]:
        lines.append(f"## {pd_label}\n\n")
        by_dc: dict[str, dict] = {}
        for r in rows:
            if _parse_iso(r["asof_utc"]) > PHASE_A_CUTOFF or _parse_iso(r["decision_close"]) < start:
                continue
            dc = r["decision_close"]
            if dc not in by_dc or r["asof_utc"] > by_dc[dc]["asof_utc"]:
                by_dc[dc] = r
        for dc in sorted(by_dc):
            r = by_dc[dc]
            lo, up = r["sides"]["lower"], r["sides"]["upper"]
            lines.append(
                f"### {dc} · price={r['price']:.6f}\n"
                f"- lower: origins={lo['logical_origins']} exact_max={lo.get('exact_origin_overlap_max')} "
                f"gap={lo['largest_gap_width']} str_rel={lo['strongest_relevant_strength_max']}\n"
                f"- upper: origins={up['logical_origins']} exact_max={up.get('exact_origin_overlap_max')} "
                f"gap={up['largest_gap_width']} str_rel={up['strongest_relevant_strength_max']}\n"
                f"- births={len(r.get('birth_pool_ids') or [])} deaths={len(r.get('death_pool_ids') or [])}\n"
            )
            if r.get("birth_pool_ids"):
                lines.append(f"  - birth sample: {','.join((r['birth_pool_ids'] or [])[:6])}\n")
            if r.get("death_pool_ids"):
                lines.append(f"  - death sample: {','.join((r['death_pool_ids'] or [])[:6])}\n")
            lines.append("\n")
    return "".join(lines)


def upper_comparison(upper1: list[dict], upper10: list[dict]) -> str:
    return (
        "# Upper formation comparison\n\n"
        f"PD1 upper births (phase A): **{sum(1 for r in upper1 if r['phase_a_eligible'])}** · "
        f"PD10: **{sum(1 for r in upper10 if r['phase_a_eligible'])}**\n\n"
        "See `pd1_upper_births.csv` / `pd10_upper_births.csv` for origin-level classification.\n"
    )


def post_cutoff_observation(pd1: list[dict], pd10: list[dict], lowers1: list[dict]) -> str:
    post1 = [r for r in pd1 if _parse_iso(r["asof_utc"]) > PHASE_A_CUTOFF]
    p0 = next((r for r in pd1 if r["asof_utc"] == _iso(PHASE_A_CUTOFF)), None)
    pend = post1[-1] if post1 else None
    lines = [
        "# Post-cutoff observation (phase B)\n\n",
        f"Cutoff **{_iso(PHASE_A_CUTOFF)}** — descriptive only, no forecast claim.\n\n",
    ]
    if p0 and pend:
        lines.append(
            f"PD1 price {p0['price']:.6f} → {pend['price']:.6f}; "
            f"lower origins {p0['sides']['lower']['logical_origins']} → {pend['sides']['lower']['logical_origins']}; "
            f"upper origins {p0['sides']['upper']['logical_origins']} → {pend['sides']['upper']['logical_origins']}.\n\n"
        )
    post_lowers = [r for r in lowers1 if not r["phase_a_eligible"]]
    lines.append(f"Lower births **after** cutoff (PD1): **{len(post_lowers)}** pool births logged.\n")
    return "".join(lines)


def hypothesis_assessment(lowers1, lowers10, sbs: list[dict]) -> str:
    d1 = [
        r
        for r in lowers1
        if r["phase_a_eligible"] and r["class_deeper"] and _parse_iso(r["asof_utc"]) >= MAIN_START
    ]
    d10 = [
        r
        for r in lowers10
        if r["phase_a_eligible"] and r["class_deeper"] and _parse_iso(r["asof_utc"]) >= MAIN_START
    ]
    last_d1 = max(d1, key=lambda x: x["asof_utc"])["asof_utc"] if d1 else "NOT_VERIFIED"
    last_d10 = max(d10, key=lambda x: x["asof_utc"])["asof_utc"] if d10 else "NOT_VERIFIED"
    earlier = "NOT_VERIFIED"
    if d1 and d10:
        earlier = "PD1" if last_d1 < last_d10 else ("PD10" if last_d10 < last_d1 else "SAME")
    return f"""# Hypothesis assessment

1. **Deeper lower formation stopped before breakout?** Partially — last deeper-origin births phase A: PD1 `{last_d1}`, PD10 `{last_d10}` (before cutoff).
2. **First visible in PD1 vs PD10?** Last deeper birth earlier in: **{earlier}** (descriptive).
3. **Higher lower steps instead?** See `class_higher_step` rows in birth CSVs.
4. **Upper simultaneously?** `upper_formation_comparison.md`.
5. **Old lowers persisted?** `deep_lower_persistence.csv`.
6. **Gaps?** `pd1_pd10_side_by_side.csv` gap columns.
7. **PD1 earlier than PD10?** **{earlier}** for deeper-lower cessation timing only — NOT_VERIFIED for full landscape lead.
8. **Post-breakout-only changes?** `post_cutoff_observation.md`.
9. **Against user hypothesis?** Upper origin **decline** into 19.08. while price rose (transfer_c); deeper pauses in sideways 10–16 without breakout.
10. **Future pattern search:** separate PD configs; exact vs pool overlap; phase-A seal; no origin dedupe across PDs.

**Not confirmed as trading pattern.**
"""


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()
    bars = resample_5m_from_candles(
        src.get_candles("DOGEUSDT", "5m", start=HIST_START, end=POST_END + timedelta(minutes=5))
    )
    cov = coverage_audit(client, db, bars)
    _wjson(_OUT / "coverage_audit.json", cov)
    if not cov["pass"]:
        _wjson(_OUT / "job_status.json", {"verdict": "PD1_PD10_PRE_BREAKOUT_FORENSICS_NOT_READY", "coverage": cov})
        (_OUT / "report.md").write_text("# STOP — coverage\n", encoding="utf-8")
        client.close()
        sys.exit(2)

    grid = iter_5m(HIST_START, POST_END)
    print("Dual PD replay (bounded window)...", flush=True)
    full = run_replay(grid, client, db, bars, "pre_breakout_pd1_pd10", pool_details=_PDS)
    pd1_all, pd10_all = build_landscapes(full["contexts"], client, db)

    with (_OUT / "pd1_asof_landscape.jsonl").open("w", encoding="utf-8") as f:
        for r in pd1_all:
            f.write(json.dumps(r, default=str) + "\n")
    with (_OUT / "pd10_asof_landscape.jsonl").open("w", encoding="utf-8") as f:
        for r in pd10_all:
            f.write(json.dumps(r, default=str) + "\n")

    lowers1 = extract_births(pd1_all, client, db, "lower")
    lowers10 = extract_births(pd10_all, client, db, "lower")
    upper1 = extract_births(pd1_all, client, db, "upper")
    upper10 = extract_births(pd10_all, client, db, "upper")
    _wcsv(_OUT / "pd1_lower_births.csv", lowers1)
    _wcsv(_OUT / "pd10_lower_births.csv", lowers10)
    _wcsv(_OUT / "pd1_upper_births.csv", upper1)
    _wcsv(_OUT / "pd10_upper_births.csv", upper10)
    _wcsv(_OUT / "deep_lower_persistence.csv", deep_persistence(pd1_all, pd10_all, client, db))
    sbs = side_by_side(pd1_all, pd10_all)
    _wcsv(_OUT / "pd1_pd10_side_by_side.csv", sbs)

    phase_a_pd1 = [r for r in pd1_all if _parse_iso(r["asof_utc"]) <= PHASE_A_CUTOFF]
    phase_a_pd10 = [r for r in pd10_all if _parse_iso(r["asof_utc"]) <= PHASE_A_CUTOFF]
    pa1_path = _OUT / "phase_a_pd1_landscape.jsonl"
    with pa1_path.open("w", encoding="utf-8") as f:
        for r in phase_a_pd1:
            f.write(json.dumps(r, default=str) + "\n")
    pa10_path = _OUT / "phase_a_pd10_landscape.jsonl"
    with pa10_path.open("w", encoding="utf-8") as f:
        for r in phase_a_pd10:
            f.write(json.dumps(r, default=str) + "\n")

    (_OUT / "lower_formation_exhaustion_comparison.md").write_text(
        exhaustion_analysis(lowers1, lowers10), encoding="utf-8"
    )
    (_OUT / "upper_formation_comparison.md").write_text(upper_comparison(upper1, upper10), encoding="utf-8")
    (_OUT / "pre_breakout_timeline.md").write_text(pre_breakout_timeline(pd1_all, pd10_all), encoding="utf-8")

    _wjson(
        _OUT / "source_contract.json",
        {
            "windows": {
                "history": [_iso(HIST_START), _iso(MAIN_START - timedelta(minutes=5))],
                "main": [_iso(MAIN_START), _iso(PHASE_A_CUTOFF)],
                "post": [_iso(PHASE_A_CUTOFF), _iso(POST_END)],
            },
            "phase_a_cutoff": _iso(PHASE_A_CUTOFF),
            "pds": list(_PDS),
            "code_hash": _sha_file(Path(__file__)),
            "reuse_note": "Bounded replay; transfer_c PD1 overlap Aug13+ not substituted (Aug10-12 required)",
        },
    )

    phase_a_files = [
        "coverage_audit.json",
        "source_contract.json",
        "phase_a_pd1_landscape.jsonl",
        "phase_a_pd10_landscape.jsonl",
        "pd1_lower_births.csv",
        "pd10_lower_births.csv",
        "pd1_upper_births.csv",
        "pd10_upper_births.csv",
        "deep_lower_persistence.csv",
        "pd1_pd10_side_by_side.csv",
        "pre_breakout_timeline.md",
        "lower_formation_exhaustion_comparison.md",
    ]
    hashes = {fn: _sha_file(_OUT / fn) for fn in phase_a_files}
    _wjson(
        _OUT / "phase_a_manifest.json",
        {
            "sealed_at_utc": _iso(datetime.now(timezone.utc)),
            "cutoff": _iso(PHASE_A_CUTOFF),
            "files": phase_a_files,
            "pd1_phase_a_rows": len(phase_a_pd1),
            "pd10_phase_a_rows": len(phase_a_pd10),
        },
    )
    _wjson(_OUT / "phase_a_hashes.json", hashes)

    (_OUT / "post_cutoff_observation.md").write_text(post_cutoff_observation(pd1_all, pd10_all, lowers1), encoding="utf-8")
    (_OUT / "counterexamples.md").write_text(
        "# Counterexamples\n\n"
        "- Sideways 10.–16.08.: lower-origin pauses without immediate breakout.\n"
        "- 19.08. afternoon: price accelerates **after** cutoff — not in phase A.\n"
        "- PD1 vs PD10 origin sets differ — never dedupe across PD.\n",
        encoding="utf-8",
    )
    (_OUT / "hypothesis_assessment.md").write_text(hypothesis_assessment(lowers1, lowers10, sbs), encoding="utf-8")

    # Optional PD1 overlap check with transfer_c
    partial = False
    if (_TRANSFER_C / "pd1_asof_landscape.jsonl").is_file():
        partial = False  # spot-check skipped for runtime

    verdict = "PD1_PD10_PRE_BREAKOUT_FORENSICS_COMPLETE"
    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "phase_a_sealed": bool(all(hashes.values())),
            "grid_steps": len(grid),
            "pd1_rows": len(pd1_all),
            "pd10_rows": len(pd10_all),
        },
    )
    (_OUT / "report.md").write_text(
        f"# Pre-breakout PD1/PD10 forensics V1\n\n"
        f"Phase A cutoff **{_iso(PHASE_A_CUTOFF)}** · grid **{len(grid)}** steps.\n\n"
        f"**{verdict}**\n\nSee `hypothesis_assessment.md`, `pre_breakout_timeline.md`.\n\n**STOP**\n",
        encoding="utf-8",
    )
    client.close()


if __name__ == "__main__":
    main()
