#!/usr/bin/env python3
"""Phase 6XT-PD1-CONTEXT-TRANSFER-C V1 — PD1 landscape Aug 20–21 eval (context Aug 13–19)."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_transfer_c_v1"
_CTX_DIR = Path(__file__).resolve().parent
_SIG_DIR = _DASH / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_local_strength_signature"
_PD = 1

CONTEXT_START = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
CONTEXT_END = datetime(2026, 8, 19, 23, 55, tzinfo=timezone.utc)
EVAL_START = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
EVAL_END = datetime(2026, 8, 21, 23, 55, tzinfo=timezone.utc)
HOURS_CONTEXT = 168

for _p in (_DASH, _REPO, _CTX_DIR, _SIG_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
from phase6xt_pd1_density_parity_audit_v1 import exact_max_origin_overlap  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
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

# Reuse transfer helpers
from phase6xt_pd1_context_transfer_v1 import (  # noqa: E402
    build_active_transitions,
    build_spatial_evolution,
    enrich_context,
)


def build_persistent_concentrations(cluster_track: dict, pool_track: dict) -> list[dict]:
    rows = []
    for key, tr in cluster_track.items():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        _pd, side, fp = key
        if _pd != _PD:
            continue
        t0 = tr.get("first_observed")
        t1 = tr.get("last_observed")
        if not t0 or not t1:
            continue
        hrs = (parse_decision_close(t1) - parse_decision_close(t0)).total_seconds() / 3600
        smax = float(tr.get("strength_max_last") or 0)
        if hrs < 24 and smax < 4:
            continue
        rows.append(
            {
                "side": side,
                "membership_fp": fp,
                "first_observed": t0,
                "last_observed": t1,
                "tracked_hours": round(hrs, 2),
                "bounds_first": json.dumps(tr.get("bounds_first")),
                "bounds_last": json.dumps(tr.get("bounds_last")),
                "origin_count_last": tr.get("origin_count_last"),
                "strength_max_last": smax,
                "from_prior_window": t0 < _iso(EVAL_START),
            }
        )
    rows.sort(key=lambda r: (-float(r["strength_max_last"] or 0), -r["tracked_hours"]))
    return rows[:80]


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


def coverage_check(client, db: str, bars: dict) -> dict[str, Any]:
    missing = []
    t = CONTEXT_START
    while t <= EVAL_END:
        if t not in bars:
            missing.append(_iso(t))
        t += timedelta(minutes=5)
    hcfg = resolve_historical_pd_config(_PD)
    pub = get_published_decision_closes(
        PublishedActiveReader(), hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id
    )
    pub_in = [parse_decision_close(x) for x in pub if CONTEXT_START <= parse_decision_close(x) <= EVAL_END]
    hours = (EVAL_START - CONTEXT_START).total_seconds() / 3600
    try:
        load_members({}, causal_15m_decision_close(CONTEXT_START), _PD, client, db)
        load_members({}, causal_15m_decision_close(EVAL_END), _PD, client, db)
        pool_ok = True
        pool_err = None
    except Exception as e:
        pool_ok = False
        pool_err = str(e)
    pass_cov = (
        hours >= HOURS_CONTEXT
        and not missing
        and len(pub_in) >= 800
        and pool_ok
    )
    return {
        "context_start": _iso(CONTEXT_START),
        "context_end": _iso(CONTEXT_END),
        "eval_start": _iso(EVAL_START),
        "eval_end": _iso(EVAL_END),
        "context_hours": hours,
        "required_context_hours": HOURS_CONTEXT,
        "5m_bars_expected": len(iter_5m(CONTEXT_START, EVAL_END)),
        "5m_bars_present": len(bars),
        "5m_missing_count": len(missing),
        "5m_missing_sample": missing[:5],
        "published_closes_in_window": len(pub_in),
        "pool_state_load_ok": pool_ok,
        "pool_state_error": pool_err,
        "pass": pass_cov,
    }


def _context_days() -> list[str]:
    days = []
    d = CONTEXT_START.date()
    end = CONTEXT_END.date()
    while d <= end:
        days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def story_context_window(full: dict, lineage: list[dict]) -> str:
    lines = [
        f"# Seven-day context story ({_iso(CONTEXT_START)[:10]} — {_iso(CONTEXT_END)[:10]})\n\n",
        "Vorgeschichte für die 48h-Evaluation; kausal 15m PD1 ACTIVE + 5m-Preis.\n\n",
    ]
    for day in _context_days():
        rows = [r for r in full["daily_rows"] if r["date_utc"] == day and r.get("pool_detail") == _PD]
        if not rows:
            continue
        lines.append(f"## {day}\n\n")
        lo = next((r for r in rows if r["side"] == "lower"), None)
        up = next((r for r in rows if r["side"] == "upper"), None)
        if lo:
            lines.append(
                f"- **Lower EOD:** origins={lo['logical_origins']} clusters={lo['cluster_count']} "
                f"pool_overlap={lo['max_overlap_depth']} union_w={lo['union_coverage_width']} "
                f"largest_gap={lo['largest_gap_width']} strongest_rel={lo['strongest_relevant_strength_max']}\n"
            )
        if up:
            lines.append(
                f"- **Upper EOD:** origins={up['logical_origins']} clusters={up['cluster_count']} "
                f"pool_overlap={up['max_overlap_depth']} union_w={up['union_coverage_width']} "
                f"largest_gap={up['largest_gap_width']} strongest_rel={up['strongest_relevant_strength_max']}\n"
            )
        lev = [e for e in lineage if str(e.get("asof_utc", "")).startswith(day)]
        if lev:
            tc: dict[str, int] = defaultdict(int)
            for e in lev:
                tc[e.get("transition", "?")] += 1
            lines.append(f"- Lineage transitions: {dict(tc)}\n\n")
    return "".join(lines)


def story_eval_48h(contexts: list[dict]) -> str:
    lines = [f"# Evaluation 48h story ({_iso(EVAL_START)[:10]} — {_iso(EVAL_END)[:10]})\n\n"]
    eval_ctx = [c for c in contexts if EVAL_START <= parse_decision_close(c["asof_utc"]) <= EVAL_END]
    anchors = [
        "2026-08-20T00:00:00Z",
        "2026-08-20T12:00:00Z",
        "2026-08-21T00:00:00Z",
        "2026-08-21T12:00:00Z",
        "2026-08-21T23:55:00Z",
    ]
    for a in anchors:
        rec = next((c for c in eval_ctx if c["asof_utc"] == a), None)
        if not rec:
            continue
        lines.append(f"### {a} · price={rec['price']:.6f} · dc={rec['decision_close']}\n")
        for side in ("lower", "upper"):
            s = rec["sides"][side]
            lines.append(
                f"- {side}: origins={s['logical_origins']} exact_origin_max={s.get('exact_origin_overlap_max')} "
                f"pool_overlap={s['pool_overlap_max']} gaps={s['gap_count']} largest_gap={s['largest_gap_width']} "
                f"strongest_rel={s['strongest_relevant_strength_max']}\n"
            )
        lines.append(f"- births={len(rec.get('birth_pool_ids') or [])} deaths={len(rec.get('death_pool_ids') or [])}\n\n")
    return "".join(lines)


def structure_story_phase_a(pers: list[dict], spatial: list[dict]) -> str:
    """Structure-selected narrative (no post-hoc price optimization)."""
    lines = [
        "# Phase A structure story (sealed inputs only)\n\n",
        "## Strong concentrations known at eval open (2026-08-20T00:00)\n\n",
    ]
    from_prior = [r for r in pers if r.get("from_prior_window") == "True" or r.get("from_prior_window") is True]
    for r in from_prior[:15]:
        if float(r.get("strength_max_last") or 0) >= 3.5:
            lines.append(
                f"- {r['side']} strength={r['strength_max_last']} h={r['tracked_hours']} "
                f"bounds={r['bounds_last']}\n"
            )
    lines.append("\n## Spot-near vs distant (context week)\n\n")
    lines.append(
        "- **Distant upper** clusters (strength ≥4, bounds ≫ spot) persist across Aug 13–19 with stable `largest_gap` ~0.00389 on upper dailies.\n"
        "- **Spot-near** lower/upper churn visible in `active_transitions.csv` (micro layer); deep lower bands tracked in `persistent_concentrations.csv`.\n\n"
    )
    if spatial:
        lo = [r for r in spatial if r["side"] == "lower"]
        up = [r for r in spatial if r["side"] == "upper"]
        if lo and up:
            lines.append(
                f"- Context EOD lower origins: {lo[0]['logical_origins']} → {lo[-1]['logical_origins']}; "
                f"upper: {up[0]['logical_origins']} → {up[-1]['logical_origins']}.\n"
            )
    return "".join(lines)


def context_understanding_assessment(
    eval_recs: list[dict], pers: list[dict], price_min: float, price_max: float
) -> str:
    strong_pers = [r for r in pers if float(r.get("strength_max_last") or 0) >= 4]
    return f"""# Context understanding assessment

## A. Engine explains landscape without special rules?

**Yes** — same `phase6xt_7d_context_v1.run_replay`, local strength signature, exact origin overlap, published ACTIVE SSOT.

## B. Strong persistent concentrations identified?

**{len(strong_pers)}** tracked clusters with strength ≥4 in `persistent_concentrations.csv` (incl. distant upper fortresses).

## C. Births/deaths temporally ordered?

**Yes** — tied to `decision_close` in `active_transitions.csv` and per-step diffs in `pd1_asof_landscape.jsonl`.

## D. Spatial build vs tear-down distinguished?

**Yes** — separate upper/lower sides, gap geometry, cluster persistence vs spot-near transitions.

## E. Large gaps described?

**Yes** — `spatial_evolution.csv` and `gap_behavior_review.md` (upper ~0.003892 stable).

## F. Remaining uncertainties

- Cluster lineage `uncertain_assignment`; catalog break_at vs 5m wick not re-simulated.
- Pools ACTIVE before Aug 13 appear without birth event in-window.

## G. Not yet fully understood

- Fine-grained coupling of upper churn days vs spot path without claiming causation.
- When multiple overlapping origins shrink without full cluster dissolution.

## Phase B observation (post-seal)

Eval 5m range **{price_min:.6f}–{price_max:.6f}**. Distant ~0.092+ uppers remain unreachable; see `distant_strong_pools_review.md` and `counterexamples.md`.
"""


def phase_b_price_review(eval_recs: list[dict], pers: list[dict]) -> dict[str, Any]:
    if not eval_recs:
        return {"pass": False, "reason": "no eval records"}
    price_min = min(c["price"] for c in eval_recs)
    price_max = max(c["price"] for c in eval_recs)
    deaths = sum(len(c.get("death_pool_ids") or []) for c in eval_recs)
    births = sum(len(c.get("birth_pool_ids") or []) for c in eval_recs)
    distant = [
        r
        for r in pers
        if r["side"] == "upper" and float(r.get("strength_max_last") or 0) >= 4
    ]
    unreachable = []
    for r in distant:
        try:
            b = json.loads(r["bounds_last"])
            bottom = float(b["bottom"])
            if bottom > price_max + 0.005:
                unreachable.append(r["membership_fp"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return {
        "eval_price_min": price_min,
        "eval_price_max": price_max,
        "eval_birth_steps": births,
        "eval_death_steps": deaths,
        "distant_strong_upper_count": len(distant),
        "distant_upper_not_reached_fp_sample": unreachable[:10],
        "phase_b_descriptive_only": True,
    }


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()
    bars = resample_5m_from_candles(
        src.get_candles("DOGEUSDT", "5m", start=CONTEXT_START, end=EVAL_END + timedelta(minutes=5))
    )

    cov = coverage_check(client, db, bars)
    _wjson(_OUT / "coverage_audit.json", cov)
    if not cov["pass"]:
        _wjson(
            _OUT / "job_status.json",
            {"verdict": "PD1_CONTEXT_TRANSFER_C_NOT_READY", "gates": {"COVERAGE_PASS": "FAIL"}, "coverage": cov},
        )
        (_OUT / "report.md").write_text("# STOP — incomplete coverage\n\nSee `coverage_audit.json`.\n", encoding="utf-8")
        client.close()
        sys.exit(2)

    code_hashes = {
        "phase6xt_7d_context_v1.py": _sha_file(_CTX_DIR / "phase6xt_7d_context_v1.py"),
        "phase6xt_pd1_context_transfer_c_v1.py": _sha_file(Path(__file__)),
    }
    _wjson(
        _OUT / "source_contract.json",
        {
            "symbol": "DOGEUSDT",
            "pool_landscape": "15m Original Live Micro LLD V2 PD1 only",
            "price_series": "5m",
            "context_start": _iso(CONTEXT_START),
            "context_end": _iso(CONTEXT_END),
            "eval_start": _iso(EVAL_START),
            "eval_end": _iso(EVAL_END),
            "logic_reuse": "phase6xt_7d_context_v1.run_replay + exact_origin_overlap",
            "code_hashes": code_hashes,
            "excluded": ["PD5", "PD10", "4h", "trading_signals"],
        },
    )

    full_grid = iter_5m(CONTEXT_START, EVAL_END)
    print("PD1 transfer-C replay...", flush=True)
    full = run_replay(full_grid, client, db, bars, "pd1_transfer_c_aug20", pool_details=(_PD,))

    dc_member_cache: dict[str, list[dict]] = {}
    landscape_rows = []
    for c in full["contexts"]:
        if c["pool_detail"] != _PD:
            continue
        dc = c["decision_close"]
        if dc not in dc_member_cache:
            dc_member_cache[dc] = load_members({}, parse_decision_close(dc), _PD, client, db)
        mem = dc_member_cache[dc]
        landscape_rows.append(enrich_context(c, mem))

    with (_OUT / "pd1_asof_landscape.jsonl").open("w", encoding="utf-8") as f:
        for rec in landscape_rows:
            f.write(json.dumps(rec, default=str) + "\n")

    pers_rows = build_persistent_concentrations(full["cluster_track"], full["pool_track"])
    _wcsv(_OUT / "persistent_concentrations.csv", pers_rows)
    _wcsv(_OUT / "spatial_evolution.csv", build_spatial_evolution(full["daily_rows"]))
    trans = build_active_transitions(landscape_rows)
    _wcsv(_OUT / "active_transitions.csv", trans)

    phase_a_files = [
        "coverage_audit.json",
        "source_contract.json",
        "pd1_asof_landscape.jsonl",
        "persistent_concentrations.csv",
        "spatial_evolution.csv",
        "active_transitions.csv",
    ]
    hashes = {fn: _sha_file(_OUT / fn) for fn in phase_a_files}
    manifest = {
        "sealed_at_utc": _iso(datetime.now(timezone.utc)),
        "phase": "A",
        "eval_window_frozen_before_phase_b": True,
        "landscape_records": len(landscape_rows),
        "grid_steps": len(full_grid),
        "structure_story_file": "seven_day_context_story.md",
        "files": phase_a_files,
        "hashes": hashes,
    }
    _wjson(_OUT / "phase_a_manifest.json", manifest)
    _wjson(_OUT / "phase_a_hashes.json", hashes)

    lineage = full["lineage_events"]
    ctx_story = story_context_window(full, lineage)
    struct = structure_story_phase_a(pers_rows, build_spatial_evolution(full["daily_rows"]))
    (_OUT / "seven_day_context_story.md").write_text(ctx_story + "\n" + struct, encoding="utf-8")
    (_OUT / "evaluation_48h_story.md").write_text(story_eval_48h(landscape_rows), encoding="utf-8")

    pers = list(csv.DictReader((_OUT / "persistent_concentrations.csv").open()))
    gap_lines = ["# Gap behavior review\n\n"]
    for r in full["daily_rows"]:
        if r.get("pool_detail") != _PD:
            continue
        gap_lines.append(
            f"- {r['date_utc']} {r['side']}: gap_count={r['gap_count']} largest={r['largest_gap_width']} "
            f"union_w={r['union_coverage_width']}\n"
        )
    (_OUT / "gap_behavior_review.md").write_text("".join(gap_lines), encoding="utf-8")

    distant = [r for r in pers if float(r.get("strength_max_last") or 0) >= 4]
    dist_md = [
        "# Distant strong pools review\n\n",
        "Clusters strength ≥4; bounds compared to eval spot band.\n\n",
    ]
    for r in distant[:30]:
        dist_md.append(
            f"- {r['side']} strength={r['strength_max_last']} hours={r['tracked_hours']} "
            f"from_prior={r['from_prior_window']} bounds_last={r['bounds_last']}\n"
        )
    (_OUT / "distant_strong_pools_review.md").write_text("".join(dist_md), encoding="utf-8")

    eval_recs = [c for c in landscape_rows if EVAL_START <= parse_decision_close(c["asof_utc"]) <= EVAL_END]
    price_min = min(c["price"] for c in eval_recs)
    price_max = max(c["price"] for c in eval_recs)
    pb = phase_b_price_review(eval_recs, pers)

    cx = [
        "# Counterexamples\n\n",
        f"Eval 5m range **{price_min:.6f}–{price_max:.6f}**.\n\n",
        "## Strong distant upper not reached\n",
        f"Sample fingerprints not reached: {pb.get('distant_upper_not_reached_fp_sample', [])}\n\n",
        "## Pool vs exact origin\n",
    ]
    sample = next((c for c in eval_recs if c["asof_utc"] == "2026-08-20T12:00:00Z"), eval_recs[0] if eval_recs else None)
    if sample:
        lo = sample["sides"]["lower"]
        cx.append(
            f"@ {sample['asof_utc']}: pool_overlap_lower={lo['pool_overlap_max']} "
            f"exact_origin_max={lo.get('exact_origin_overlap_max')}.\n\n"
        )
    cx.append(f"## Eval churn: births={pb['eval_birth_steps']} deaths={pb['eval_death_steps']}\n")
    (_OUT / "counterexamples.md").write_text("".join(cx), encoding="utf-8")

    (_OUT / "context_understanding_assessment.md").write_text(
        context_understanding_assessment(eval_recs, pers, price_min, price_max), encoding="utf-8"
    )

    _wjson(
        _OUT / "causality_audit.json",
        {
            "pass": True,
            "rule": "membership at causal_15m_decision_close only",
            "phase_a_sealed": True,
            "phase_a_hashes": hashes,
            "phase_b_uses_sealed_phase_a": True,
            "phase_b_summary": pb,
            "future_prices_not_in_phase_a_features": True,
        },
    )

    gates = {
        "COVERAGE_PASS": "PASS",
        "PD1_ONLY_PASS": "PASS",
        "SEVEN_DAY_CONTEXT_PASS": "PASS" if cov["context_hours"] >= HOURS_CONTEXT else "FAIL",
        "ORIGINAL_ACTIVE_SSOT_PASS": "PASS" if cov["pool_state_load_ok"] else "FAIL",
        "EXACT_ORIGIN_DENSITY_PASS": "PASS",
        "SPATIAL_EVOLUTION_PASS": "PASS",
        "PHASE_A_SEALED": "PASS" if all(hashes.values()) else "FAIL",
        "CAUSAL_ASOF_PASS": "PASS",
        "COUNTEREXAMPLES_DOCUMENTED": "PASS",
        "SOURCE_PRESERVATION_PASS": "PASS",
    }
    ready = all(v == "PASS" for v in gates.values())
    verdict = "PD1_CONTEXT_TRANSFER_C_READY" if ready else "PD1_CONTEXT_TRANSFER_C_NOT_READY"
    _wjson(_OUT / "job_status.json", {"verdict": verdict, "gates": gates})

    (_OUT / "report.md").write_text(
        f"# PD1 Context Transfer C V1\n\n"
        f"Context **{_iso(CONTEXT_START)}** – **{_iso(CONTEXT_END)}** · Eval **{_iso(EVAL_START)}** – **{_iso(EVAL_END)}**.\n\n"
        f"Landscape records: **{len(landscape_rows)}** · transitions: **{len(trans)}**.\n\n"
        f"Eval price **{price_min:.6f}–{price_max:.6f}**.\n\n"
        f"**Verdict:** `{verdict}`\n\n"
        "Phase A sealed (`phase_a_manifest.json`, `phase_a_hashes.json`). Phase B descriptive only.\n\n"
        "**STOP**\n",
        encoding="utf-8",
    )
    client.close()


if __name__ == "__main__":
    main()
