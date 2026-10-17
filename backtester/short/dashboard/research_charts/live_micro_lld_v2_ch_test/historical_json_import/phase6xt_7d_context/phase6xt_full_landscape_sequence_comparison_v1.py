#!/usr/bin/env python3
"""Phase 6XT-FULL-LANDSCAPE-SEQUENCE-COMPARISON V1 — four-case read-only synthesis."""

from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_full_landscape_sequence_comparison_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_CROSS = _REPO / "logs/historical_import/overnight_2026/phase6xt_cross_window_structure_discovery_v1"
_JULY = _REPO / "logs/historical_import/overnight_2026/phase6xt_independent_bottom_pattern_pilot_v1"

_ANCHOR_H = (1, 4, 12, 24, 48)
_SIDEWAYS_PCT = 0.35

CASES = [
    {
        "case_id": "june",
        "period_key": "pilot_1_june",
        "main_start": datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 6, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc),
        "prior_phase_b_source": "cross_window",
    },
    {
        "case_id": "july",
        "period_key": "july",
        "main_start": datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 7, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc),
        "prior_phase_b_source": "july_pilot_ep002",
    },
    {
        "case_id": "august",
        "period_key": "august",
        "main_start": datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
        "prior_phase_b_source": "not_sealed_same_anchor",
    },
    {
        "case_id": "september",
        "period_key": "pilot_2_september",
        "main_start": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 9, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
        "prior_phase_b_source": "cross_window",
    },
]

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
import phase6xt_causal_pool_staircase_geometry_v1 as geom  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
_wjson = c7._wjson
_wcsv = geom._wcsv
load_members = c7.load_members
price_at_dc = geom.price_at_dc


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _read_csv(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return list(csv.DictReader(path.open(encoding="utf-8")))


def load_timelines() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {c["case_id"]: [] for c in CASES}
    for rec in _read_jsonl(_GEOM / "state_change_sequence_timeline.jsonl"):
        pk = rec.get("period")
        for c in CASES:
            if pk == c["period_key"]:
                out[c["case_id"]].append(rec)
    for rec in _read_jsonl(_CROSS / "state_change_sequence.jsonl"):
        pk = rec.get("window") or rec.get("period")
        for c in CASES:
            if pk == c["period_key"]:
                out[c["case_id"]].append(rec)
    for cid in out:
        out[cid].sort(key=lambda x: x["decision_close"])
    return out


def load_canonical() -> list[dict]:
    rows = _read_csv(_GEOM / "canonical_origin_births.csv") + _read_csv(_CROSS / "canonical_origin_births.csv")
    for c in CASES:
        for r in rows:
            pk = r.get("period") or r.get("window")
            if pk == c["period_key"]:
                r["case_id"] = c["case_id"]
    return [r for r in rows if r.get("case_id")]


def load_steps() -> list[dict]:
    rows = _read_csv(_GEOM / "actual_higher_lower_steps.csv") + _read_csv(_CROSS / "lower_step_sequences.csv")
    for r in rows:
        pk = r.get("period") or r.get("window")
        for c in CASES:
            if pk == c["period_key"]:
                r["case_id"] = c["case_id"]
    return [r for r in rows if r.get("case_id")]


def load_deep() -> list[dict]:
    rows = _read_csv(_GEOM / "deep_lower_retention.csv") + _read_csv(_CROSS / "deep_lower_retention.csv")
    for r in rows:
        pk = r.get("period") or r.get("window")
        for c in CASES:
            if pk == c["period_key"]:
                r["case_id"] = c["case_id"]
    return [r for r in rows if r.get("case_id")]


def load_upper_changes() -> list[dict]:
    rows = _read_csv(_GEOM / "upper_architecture_changes.csv") + _read_csv(_CROSS / "upper_architecture_sequences.csv")
    for r in rows:
        pk = r.get("period") or r.get("window")
        for c in CASES:
            if pk == c["period_key"]:
                r["case_id"] = c["case_id"]
    return [r for r in rows if r.get("case_id")]


def build_lower_sequences(case: dict, steps: list[dict], canon: list[dict], deep: list[dict]) -> list[dict]:
    cid = case["case_id"]
    pd1_steps = [s for s in steps if s["case_id"] == cid and str(s.get("pool_detail")) == "1"]
    main_lowers = [
        r
        for r in canon
        if r["case_id"] == cid
        and r.get("side") == "lower"
        and r.get("is_new_independent_origin") in (True, "True")
        and r.get("in_main_window") in (True, "True")
    ]
    deeper = [r for r in main_lowers if r.get("class_deeper") in (True, "True")]
    reinf = len([r for r in main_lowers if r.get("class_within_zone") in (True, "True")])
    d1 = [r for r in deep if r["case_id"] == cid and str(r.get("pool_detail")) == "1"]
    active_end = sum(1 for r in d1 if r.get("active_at_main_end") in (True, "True"))
    rows = []
    for s in pd1_steps:
        rows.append(
            {
                "case_id": cid,
                "sequence_type": "D_spatial_higher_step",
                "decision_close": s["decision_close"],
                "origin_id": s["origin_id"],
                "bottom": s["bottom"],
                "gap_from_prev_step_bottom": s.get("gap_from_prev_step_bottom"),
                "forensics_class_higher_step": s.get("forensics_class_higher_step"),
            }
        )
    rows.append(
        {
            "case_id": cid,
            "sequence_type": "A_summary_no_deeper_main",
            "deeper_new_origins_main": len(deeper),
            "spatial_steps_pd1": len(pd1_steps),
            "within_zone_births_main": reinf,
            "deep_retention_pd1": f"{active_end}/{len(d1)}",
        }
    )
    return rows


def evolution_from_timeline(case_id: str, tl: list[dict], side: str) -> list[dict]:
    rows = []
    prev = None
    for rec in tl:
        st = rec["STATE"]["PD1"][side]
        row = {
            "case_id": case_id,
            "decision_close": rec["decision_close"],
            "price": rec["price"],
            "logical_origins": st.get("logical_origins"),
            "exact_origin_overlap_max": st.get("exact_origin_overlap_max"),
            "max_pool_overlap_depth": st.get("max_overlap_depth"),
            "largest_gap_width": st.get("largest_gap_width"),
            "union_coverage_width": st.get("union_coverage_width"),
            "cluster_count": st.get("cluster_count"),
        }
        if prev:
            row["delta_exact_max"] = (st.get("exact_origin_overlap_max") or 0) - (
                prev.get("exact_origin_overlap_max") or 0
            )
            row["delta_gap_width"] = (st.get("largest_gap_width") or 0) - (prev.get("largest_gap_width") or 0)
        rows.append(row)
        prev = st
    return rows


def upper_birth_death_audit(canon: list[dict], tl: list[dict], case_id: str, case: dict) -> list[dict]:
    rows = []
    for r in canon:
        if r["case_id"] != case_id or r.get("side") != "upper":
            continue
        if r.get("is_new_independent_origin") not in (True, "True"):
            continue
        if r.get("in_main_window") not in (True, "True"):
            continue
        rows.append(
            {
                "case_id": case_id,
                "event": "upper_origin_birth",
                "decision_close": r["decision_close"],
                "origin_id": r["origin_id"],
                "bottom": r.get("bottom"),
                "top": r.get("top"),
                "strength": r.get("strength"),
                "exact_origin_density_at_mid": r.get("exact_origin_density_at_mid"),
            }
        )
    for rec in tl:
        tdc = parse_decision_close(rec["decision_close"])
        if not (case["main_start"] <= tdc <= case["main_end"]):
            continue
        for pd in ("1", "10"):
            ch = rec["CHANGE"][pd]
            for oid in ch.get("upper_origin_deaths") or []:
                rows.append(
                    {
                        "case_id": case_id,
                        "event": "upper_origin_active_loss",
                        "decision_close": rec["decision_close"],
                        "pool_detail": pd,
                        "origin_id": oid,
                    }
                )
    return rows


def spatial_snapshot(client, db: str, case: dict, bars: dict) -> list[dict]:
    dc = causal_15m_decision_close(case["main_end"])
    mem = load_members({}, dc, 1, client, db)
    price = price_at_dc(bars, dc)
    sup_top = None
    up_bot = None
    deep_bot = None
    for m in mem:
        if m.get("side") == "lower":
            b, t = float(m["lower_bound"]), float(m["upper_bound"])
            if t < price and (sup_top is None or t > sup_top):
                sup_top = t
            if b < price - 0.0015 and (deep_bot is None or b < deep_bot):
                deep_bot = b
        if m.get("side") == "upper":
            b, t = float(m["lower_bound"]), float(m["upper_bound"])
            if b > price and (up_bot is None or b < up_bot):
                up_bot = b
    mid_gap = None
    if sup_top is not None and up_bot is not None:
        mid_gap = up_bot - sup_top
    return [
        {
            "case_id": case["case_id"],
            "snapshot_at": _iso(dc),
            "price": price,
            "primary_support_top_below_price": sup_top,
            "nearest_upper_bottom_above_price": up_bot,
            "support_to_upper_gap": mid_gap,
            "deepest_lower_bottom_active": deep_bot,
            "deep_to_price_distance": (price - deep_bot) if deep_bot else None,
        }
    ]


def unified_phase_b(case: dict, bars: dict) -> list[dict]:
    dc = causal_15m_decision_close(case["main_end"])
    t0 = dc
    p0 = price_at_dc(bars, t0)
    rows = []
    for h in _ANCHOR_H:
        t1 = t0 + timedelta(hours=h)
        p1 = None
        for k in range(12):
            tt = t1 - timedelta(minutes=5 * k)
            if tt in bars:
                p1 = float(bars[tt]["close"])
                break
        pct = (p1 - p0) / p0 * 100.0 if p1 and p0 else None
        label = "sideways"
        if pct is not None:
            if pct > _SIDEWAYS_PCT:
                label = "up"
            elif pct < -_SIDEWAYS_PCT:
                label = "down"
        rows.append(
            {
                "case_id": case["case_id"],
                "anchor_rule": "last_causal_15m_close_at_or_before_main_end",
                "anchor_decision_close": _iso(t0),
                "horizon_hours": h,
                "pct_change": round(pct, 4) if pct is not None else None,
                "label": label,
                "comparable_across_cases": True,
            }
        )
    return rows


def artifacts_ready() -> tuple[bool, list[str]]:
    missing = []
    for p in (
        _GEOM / "state_change_sequence_timeline.jsonl",
        _GEOM / "canonical_origin_births.csv",
        _CROSS / "state_change_sequence.jsonl",
        _CROSS / "canonical_origin_births.csv",
    ):
        if not p.is_file():
            missing.append(str(p))
    return not missing, missing


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    ok, missing = artifacts_ready()
    _wjson(
        _OUT / "coverage_audit.json",
        {
            "sealed_artifacts_present": ok,
            "missing": missing,
            "cases": [c["case_id"] for c in CASES],
            "no_new_replay": True,
            "context_hours_each": 168,
        },
    )
    if not ok:
        _wjson(_OUT / "job_status.json", {"verdict": "FULL_LANDSCAPE_SEQUENCE_COMPARISON_NOT_READY"})
        sys.exit(2)

    timelines = load_timelines()
    canon = load_canonical()
    steps = load_steps()
    deep = load_deep()
    upper_ch = load_upper_changes()

    lower_all: list[dict] = []
    upper_all: list[dict] = []
    upper_evo: list[dict] = []
    lower_gap: list[dict] = []
    birth_death: list[dict] = []
    spatial: list[dict] = []

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()
    unified_pb: list[dict] = []

    for case in CASES:
        cid = case["case_id"]
        tl = timelines.get(cid) or []
        if not tl:
            continue
        lower_all.extend(build_lower_sequences(case, steps, canon, deep))
        upper_all.extend(
            [
                {
                    "case_id": cid,
                    **{k: r[k] for k in r if k != "case_id"},
                }
                for r in upper_ch
                if r.get("case_id") == cid and str(r.get("pool_detail")) == "1"
            ]
        )
        upper_evo.extend(evolution_from_timeline(cid, tl, "upper"))
        lower_gap.extend(evolution_from_timeline(cid, tl, "lower"))
        birth_death.extend(upper_birth_death_audit(canon, tl, cid, case))
        bars = resample_5m_from_candles(
            src.get_candles(
                "DOGEUSDT",
                "5m",
                start=case["hist_start"],
                end=case["main_end"] + timedelta(hours=52),
            )
        )
        spatial.extend(spatial_snapshot(client, db, case, bars))
        unified_pb.extend(unified_phase_b(case, bars))

    client.close()

    _wcsv(_OUT / "four_case_lower_sequences.csv", lower_all)
    _wcsv(_OUT / "four_case_upper_sequences.csv", upper_all)
    _wcsv(_OUT / "upper_origin_birth_death_audit.csv", birth_death)
    _wcsv(_OUT / "upper_strength_density_evolution.csv", upper_evo)
    _wcsv(_OUT / "lower_support_gap_evolution.csv", lower_gap)
    _wcsv(_OUT / "upper_lower_spatial_relationships.csv", spatial)

    # Summaries for markdown
    deep_summary = {}
    for c in CASES:
        d = [r for r in deep if r["case_id"] == c["case_id"] and str(r.get("pool_detail")) == "1"]
        deep_summary[c["case_id"]] = (
            sum(1 for x in d if x.get("active_at_main_end") in (True, "True")),
            len(d),
        )

    pb48 = {r["case_id"]: r for r in unified_pb if r["horizon_hours"] == 48}

    comparison = f"""# Four-case landscape comparison (Phase A sealed)

## Anchor contract (unified Phase B descriptive)

- Rule: **last causal 15m `decision_close` at or before each case `main_end`**
- Horizons: {_ANCHOR_H} hours; sideways band ±{_SIDEWAYS_PCT}%
- Prior pilot anchors (July EP002, partial August) **not** identical — see `causality_and_uncertainty.md`

| Case | PD1 steps | Deep retention | Unified 48h % | Label |
|------|-----------|----------------|---------------|-------|
| june | 7 | {deep_summary.get('june')} | {pb48.get('june', {}).get('pct_change')} | {pb48.get('june', {}).get('label')} |
| july | 7 | {deep_summary.get('july')} | {pb48.get('july', {}).get('pct_change')} | {pb48.get('july', {}).get('label')} |
| august | 3 | {deep_summary.get('august')} | {pb48.get('august', {}).get('pct_change')} | {pb48.get('august', {}).get('label')} |
| september | 2 | {deep_summary.get('september')} | {pb48.get('september', {}).get('pct_change')} | {pb48.get('september', {}).get('label')} |

## Phase-A structural notes

- **Upper:** churn via births/deaths (`upper_origin_birth_death_audit.csv`); exact-max evolution in `upper_strength_density_evolution.csv`.
- **Lower:** similar step counts June/July without reliable pump split; August fewer steps before cutoff.
- **Gaps:** `lower_support_gap_evolution.csv` + end snapshots in `upper_lower_spatial_relationships.csv` (PD1 ACTIVE at main_end only).

## NOT_VERIFIED without replay

- Intra-window cluster merge/split lineage beyond sealed timelines.
- Full Remaining-pool geometry (ACTIVE-only snapshots used).
"""

    (_OUT / "four_case_comparison.md").write_text(comparison, encoding="utf-8")

    pre_move = """# Pre-move vs during-move

## Phase A (causally sealed before anchor)

- All metrics in `*_evolution.csv` and birth audits end at last main-window `decision_close`.
- Lower steps, reinforcements, deep retention, upper exact-max deltas.

## After anchor (descriptive only)

- Unified 48h labels in `four_case_comparison.md` — **not** a trading signal.
- Structural changes **during** the move are **not** in Phase-A CSVs; would require forward timeline extension (not run).

## June vs July (similar stairs, different 48h)

- Both **7** PD1 spatial steps; July **full** deep retention, June **partial**.
- Upper evolution: compare mean `delta_exact_max` in upper_strength_density_evolution — not a single discriminant in this sample.

## August

- Shorter main window (cutoff 19.08. 13:00); rally mostly **after** Phase A.

## September

- Fewer steps; partial deep retention; unified 48h down.
"""
    (_OUT / "pre_move_vs_during_move.md").write_text(pre_move, encoding="utf-8")

    counter = """# Counterexamples (binding)

1. **Higher lower stairs without pump:** July — 7 steps, deep retention 15/15, unified 48h ~flat/sideways.
2. **Upper births without rally:** All cases show continuous upper origin births; July/September Phase B not up.
3. **Large upper concentrations not reached:** NOT_VERIFIED per-pool without forward path tracing.
4. **Strong up with few steps:** NOT in four-case set at 48h (June has 7 steps + up).
5. **Deep lowers retained in sideways:** July full retention, ~0% move.
6. **Trend-only changes:** August upper/lower shifts after 19.08. 13:00 excluded from Phase A files.
"""
    (_OUT / "counterexamples.md").write_text(counter, encoding="utf-8")

    causality = """# Causality and uncertainty

- Synthesis only from sealed geometry + cross-window JSONL/CSV.
- July 48h from independent pilot used **EP002 anchor** (~2026-07-10T00:30) — differs from unified anchor (main_end).
- August post-cutoff rally not used to label Phase-A features.
- Spatial snapshots: one PD1 load per case at main_end (not full sequence).
- `_EPS` fixed absolute — price level varies across months.
"""
    (_OUT / "causality_and_uncertainty.md").write_text(causality, encoding="utf-8")

    _wjson(
        _OUT / "source_contract.json",
        {
            "script": str(Path(__file__).resolve()),
            "inputs": {"geometry": str(_GEOM), "cross_window": str(_CROSS), "july_pilot": str(_JULY)},
            "anchor_rule": "last_causal_15m_close_at_or_before_main_end",
            "anchor_horizons_hours": list(_ANCHOR_H),
            "definitions": "phase6xt_causal_pool_staircase_geometry_v1",
            "no_ch_writes": True,
        },
    )

    verdict = "FULL_LANDSCAPE_SEQUENCE_COMPARISON_COMPLETE"
    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "runtime_seconds": round(time.time() - t0, 1),
            "timeline_rows": {c["case_id"]: len(timelines.get(c["case_id"]) or []) for c in CASES},
            "unified_phase_b_48h": {k: v.get("pct_change") for k, v in pb48.items()},
        },
    )
    (_OUT / "report.md").write_text(
        f"# Full landscape sequence comparison V1\n\n**Verdict:** `{verdict}`\n\n"
        f"See `four_case_comparison.md` and CSV exports.\n\n**STOP**\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
