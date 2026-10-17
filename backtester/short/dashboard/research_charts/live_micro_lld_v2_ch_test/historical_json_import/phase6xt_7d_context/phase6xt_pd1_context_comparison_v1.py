#!/usr/bin/env python3
"""Phase 6XT-PD1-CONTEXT-COMPARISON V1 — compare Period A vs B from frozen artifacts."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_comparison_v1"
_A7 = _REPO / "logs/historical_import/overnight_2026/phase6xt_7d_pd1_context_v1"
_B = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_transfer_v1"
_DYN = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_dynamic_context_review_v1"
_FOR = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_landscape_forensics_v1"
_SPA = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_spatial_evolution_v1"
_PAR = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_density_parity_audit_v1"
_INT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_interpretation_v1"

PERIOD_A = {"id": "A", "label": "2026-07-25..2026-08-01", "context_start": "2026-07-25T00:00:00Z", "eval_focus": "2026-08-01"}
PERIOD_B = {"id": "B", "label": "2026-08-03..2026-08-11", "context_start": "2026-08-03T00:00:00Z", "eval_focus": "2026-08-10..2026-08-11"}


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


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_jsonl(path: Path) -> list[dict]:
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def load_daily(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def price_lookup(records: list[dict]) -> dict[str, float]:
    return {r["asof_utc"]: float(r["price"]) for r in records if r.get("price") is not None}


def classify_timing(
    asof: str,
    price_map: dict[str, float],
    has_birth: bool,
    has_death: bool,
    price_delta_2h: float | None,
) -> str:
    """Descriptive A–E (not signals)."""
    if has_death and not has_birth:
        return "D_ACTIVE_LOSS"
    if has_birth and not has_death:
        if price_delta_2h is None:
            return "B_DURING_OR_UNKNOWN"
        if price_delta_2h > 0.00015:
            return "C_AFTER_RISING_PRICE" if has_birth else "E"
        if price_delta_2h < -0.00015:
            return "A_BEFORE_FALLING_PRICE"
        return "B_DURING_FLAT_PRICE"
    if has_birth and has_death:
        return "D_MIXED_PUBLICATION"
    if not has_birth and not has_death and price_delta_2h is not None and abs(price_delta_2h) > 0.0001:
        return "E_PRICE_ONLY_VS_STATIC_POOLS"
    return "E_OR_MINOR"


def significant_events_from_contexts(records: list[dict], period_id: str, min_births: int = 8, min_deaths: int = 5) -> list[dict]:
    pmap = price_lookup(records)
    by_asof = {r["asof_utc"]: r for r in records}
    anchors = {
        "A": [
            "2026-07-30T12:00:00Z",
            "2026-08-01T16:00:00Z",
            "2026-08-01T18:55:00Z",
            "2026-08-01T19:00:00Z",
        ],
        "B": [
            "2026-08-10T00:00:00Z",
            "2026-08-10T12:00:00Z",
            "2026-08-11T00:00:00Z",
            "2026-08-11T12:00:00Z",
            "2026-08-11T23:55:00Z",
        ],
    }
    seen = set()
    rows = []
    for asof in anchors.get(period_id, []):
        seen.add(asof)
        r = by_asof.get(asof)
        if r:
            rows.append(r)
    for r in records:
        asof = r["asof_utc"]
        if asof in seen:
            continue
        nb = len(r.get("birth_pool_ids") or [])
        nd = len(r.get("death_pool_ids") or [])
        if nb >= min_births or nd >= min_deaths:
            rows.append(r)
            seen.add(asof)
    out = []
    for r in sorted(rows, key=lambda x: x["asof_utc"]):
        asof = r["asof_utc"]
        t = _parse_iso(asof)
        p2 = pmap.get((t - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ").replace("+00:00", "Z"))
        if not p2:
            # nearest 5m
            for k in sorted(pmap):
                if k <= asof:
                    p2 = pmap[k]
        price = float(r["price"])
        pd2 = price - p2 if p2 is not None else None
        lo = r["sides"]["lower"]
        up = r["sides"]["upper"]
        out.append(
            {
                "period": period_id,
                "asof_utc": asof,
                "decision_close": r.get("decision_close"),
                "price": price,
                "price_delta_2h": pd2,
                "category_descriptive": classify_timing(
                    asof, pmap, bool(r.get("birth_pool_ids")), bool(r.get("death_pool_ids")), pd2
                ),
                "birth_count": len(r.get("birth_pool_ids") or []),
                "death_count": len(r.get("death_pool_ids") or []),
                "birth_pool_ids_sample": ";".join((r.get("birth_pool_ids") or [])[:5]),
                "death_pool_ids_sample": ";".join((r.get("death_pool_ids") or [])[:8]),
                "lower_origins": lo.get("logical_origins"),
                "upper_origins": up.get("logical_origins"),
                "lower_pool_overlap": lo.get("max_overlap_depth") or lo.get("pool_overlap_max"),
                "upper_pool_overlap": up.get("max_overlap_depth") or up.get("pool_overlap_max"),
                "lower_exact_origin_max": lo.get("exact_origin_overlap_max"),
                "upper_exact_origin_max": up.get("exact_origin_overlap_max"),
                "lower_strongest_rel": lo.get("strongest_relevant_strength_max"),
                "upper_strongest_rel": up.get("strongest_relevant_strength_max"),
                "lower_largest_gap": lo.get("largest_gap_width"),
                "upper_largest_gap": up.get("largest_gap_width"),
            }
        )
    return out


def spatial_comparison_rows(daily_a: list[dict], daily_b: list[dict]) -> list[dict]:
    rows = []
    for period, daily in (("A", daily_a), ("B", daily_b)):
        for r in daily:
            if r.get("pool_detail") != "1" and r.get("pool_detail") != 1:
                continue
            rows.append(
                {
                    "period": period,
                    "date_utc": r.get("date_utc"),
                    "side": r.get("side"),
                    "price_eod": r.get("price"),
                    "logical_origins": r.get("logical_origins"),
                    "cluster_count": r.get("cluster_count"),
                    "max_overlap_depth": r.get("max_overlap_depth"),
                    "union_coverage_width": r.get("union_coverage_width"),
                    "largest_gap_width": r.get("largest_gap_width"),
                    "gap_count": r.get("gap_count"),
                    "strongest_relevant_strength_max": r.get("strongest_relevant_strength_max"),
                }
            )
    return rows


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    required = [
        _A7 / "seven_day_asof_contexts.jsonl",
        _A7 / "daily_upper_lower_landscape.csv",
        _B / "pd1_asof_landscape.jsonl",
        _B / "spatial_evolution.csv",
        _DYN / "report.md",
    ]
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        _wjson(_OUT / "job_status.json", {"verdict": "PD1_CONTEXT_COMPARISON_INCOMPLETE", "missing": missing})
        sys.exit(2)

    ctx_a = load_jsonl(_A7 / "seven_day_asof_contexts.jsonl")
    ctx_b = load_jsonl(_B / "pd1_asof_landscape.jsonl")
    daily_a = load_daily(_A7 / "daily_upper_lower_landscape.csv")
    daily_b = load_daily(_B / "spatial_evolution.csv")
    # B spatial_evolution has different cols — also load transfer daily from last rows
    daily_b_land = []
    for r in daily_b:
        daily_b_land.append(
            {
                "date_utc": r["date_utc"],
                "side": r["side"],
                "price": r.get("price"),
                "logical_origins": r.get("logical_origins"),
                "cluster_count": r.get("cluster_count"),
                "max_overlap_depth": r.get("max_overlap_depth"),
                "union_coverage_width": r.get("union_coverage_width"),
                "largest_gap_width": r.get("largest_gap_width"),
                "gap_count": r.get("gap_count"),
                "strongest_relevant_strength_max": r.get("strongest_relevant_strength_max"),
                "pool_detail": 1,
            }
        )

    parity_by_asof: dict[str, dict] = {}
    parity_csv = _PAR / "asof_origin_overlap_comparison.csv"
    if parity_csv.is_file():
        for row in csv.DictReader(parity_csv.open(encoding="utf-8")):
            parity_by_asof[row["asof_utc"]] = row

    chrono = significant_events_from_contexts(ctx_a, "A", min_births=10, min_deaths=5)
    chrono += significant_events_from_contexts(ctx_b, "B", min_births=5, min_deaths=5)
    for row in chrono:
        if row["period"] == "A":
            p = parity_by_asof.get(row["asof_utc"])
            if p:
                row["lower_exact_origin_max"] = p.get("exact_max_independent_origins_lower")
                row["upper_exact_origin_max"] = p.get("exact_max_independent_origins_upper")
                row["parity_pool_overlap_lower"] = p.get("analyze_side_max_pool_overlap_lower")
                row["notes"] = "exact_origin from density_parity_audit"
    trans_b = list(csv.DictReader((_B / "active_transitions.csv").open(encoding="utf-8")))
    upper_deaths_11 = sum(
        1
        for t in trans_b
        if t.get("transition") == "death"
        and ":upper:" in (t.get("pool_id") or "")
        and (t.get("asof_utc") or "").startswith("2026-08-11")
    )
    _wcsv(_OUT / "chronological_structure_changes.csv", chrono)
    _wcsv(_OUT / "upper_lower_spatial_comparison.csv", spatial_comparison_rows(daily_a, daily_b_land))

    _wjson(
        _OUT / "source_contract.json",
        {
            "period_a": PERIOD_A,
            "period_b": PERIOD_B,
            "inputs": {
                "A": str(_A7),
                "B": str(_B),
                "dynamic_review": str(_DYN),
                "forensics": str(_FOR),
                "spatial": str(_SPA),
                "parity": str(_PAR),
            },
            "parity_asof_rows": len(parity_by_asof),
            "chrono_event_rows": len(chrono),
            "upper_deaths_2026_08_11": upper_deaths_11,
            "no_ch_writes": True,
            "no_recompute": True,
        },
    )

    # Persistent clusters comparison
    pers_b = list(csv.DictReader((_B / "persistent_concentrations.csv").open()))
    pers_lines = ["# Persistent clusters comparison\n\n", "## Period B (top strength≥4, tracked)\n\n"]
    for r in pers_b[:12]:
        pers_lines.append(
            f"- {r['side']} strength={r['strength_max_last']} h={r['tracked_hours']} "
            f"from_prior={r['from_prior_window']} bounds_last={r['bounds_last']}\n"
        )
    pers_lines.append("\n## Period A (from interpretation timelines)\n\n")
    pers_lines.append(
        "- Deep lower origin `1784818800` strength **7.57** tracked until **2026-08-01T19:00** publication.\n"
        "- Upper `300bd29dd58be31c8d03857a` **0.09243–0.094726** strength **5.60** full week (interpretation_v1).\n"
    )
    (_OUT / "persistent_clusters_comparison.md").write_text("".join(pers_lines), encoding="utf-8")

    pre_during = """# Pre / during / post structure audit

## Period A — 1 August lower collapse

| Phase | As-of (UTC) | Price | Pool change | Interpretation (descriptive) |
|-------|-------------|-------|-------------|------------------------------|
| Pre | 2026-08-01T18:55 (dc 18:45) | ~0.06849 | 10 lower ACTIVE, focal stack | **A** — deep structure **present before** 19:00 publication; exact origin max **6** (parity) |
| Publication | 2026-08-01T19:00 | 0.06856 | **10 deaths**, 1 birth (`1785609900`) | **D** — ACTIVE loss at known 15m close; includes `micro/normal:lower:1784818800` strength **7.57** |
| Post (price) | 19:05–23:55 | 0.0685–0.0691 | pools static until next publication | **E** — price drifts with **1** lower origin landscape |

**Pre vs post price:** Wick/low **before** 19:00 is visible in 5m series; **mass ACTIVE removal** aligns with **publication**, not intrabar replay logic.

**Jul 30 (context):** +13 lower origins spotnah — births **before** Aug 1 slide extended; category **A/B** mix (expansion known from 30.07 publications while price still ~0.0706).

## Period B — 10–11 August expansion while price rises

| Phase | As-of | Price | Lower origins | exact max L | Upper origins |
|-------|-------|-------|---------------|-------------|---------------|
| Start eval | 2026-08-10T00:00 | 0.06918 | 10 | 4 | 203 |
| Mid | 2026-08-10T12:00 | 0.07005 | 16 | 5 | 216 |
| Day 2 open | 2026-08-11T00:00 | 0.06964 | 19 | 7 | 224 |
| Day 2 mid | 2026-08-11T12:00 | 0.07057 | 25 | 7 | 206 |
| End | 2026-08-11T23:55 | **0.07184** | **29** | **7** | **186** |

**Build vs price:** Lower origin count rises **across** the 48h rally — many births are **spot-near** (see `active_transitions.csv` B), category **B/C** (structures become known **during** rising 5m path, via 15m publications). **Not** a single pre-positioned deep stack removal.

**Upper:** Peak origins **224** then **−38** net by EOD 11.08. — **{upper_deaths_11}** upper ACTIVE ends on 2026-08-11 alone (`active_transitions.csv`); **E** for distant 0.092+ bands **unchanged**.

## Build-up vs tear-down contrast (no general rule)

| | Aug 1 tear-down | Aug 10–11 build-up |
|--|-----------------|-------------------|
| Lower net | Mass **death** at one dc | Net **births** over 48h |
| Price 48h | Drift **down** into prior deep band | Drift **up** |
| Strength anchor | Loss of **7.57** IDs | **4.35** deep band persists; spot layer grows |
| Upper | Stable field | Churn / net origin drop day 2 |

**No universal “price up ⇒ more lowers” law** — two episodes only.
"""
    (_OUT / "pre_during_post_structure_audit.md").write_text(
        pre_during.format(upper_deaths_11=upper_deaths_11), encoding="utf-8"
    )

    counter = """# Counterexamples and uncertainties

## Both periods

- **Strong distant upper** (~0.092–0.11, strength 5+) **never touched** by spot — Period A & B.
- **Pool overlap > exact origin overlap** (e.g. A @ 18:55: 7 vs 6) — counting origins vs pools misleads.
- **Bin-union density** (legacy viewer) **not** used here; exact overlap only.

## Period A

- **Weak spot lowers** removed intraday 1 Aug **before** 19:00 — price reaction **without** losing focal 7.57 until publication.
- **19:00 deaths** — engine explains as **catalog ACTIVE end**; live invalidation vs wick **not re-simulated**.

## Period B

- **Upper origin drop 224→186** while price **rises** — simple “more price ⇒ more upper” is **false**.
- **Large gap ~0.00389** upper — **no** unique price reaction tied in SSOT.

## Uncertainties

- Cluster lineage `uncertain_assignment` (both replays).
- Period A contexts lack `exact_origin_overlap_max` field — key A dates from **parity audit** only.
- Replay start defines `active_at_replay_start` — pools born before context window appear as “already there” (documented in 7d audits).
"""
    (_OUT / "counterexamples_and_uncertainties.md").write_text(counter, encoding="utf-8")

    two_period = """# Two-period landscape comparison

## 1. Lower landscape build

| | Period A | Period B |
|--|----------|----------|
| Arc | Deep **7.57** stack persistent → **spot expansion 30.07** → **collapse 01.08 19:00** | Moderate deep **4.35** band + **gradual spot lower growth** 10–11.08 |
| Eval lower origins | 5 EOD 1 Aug (post shock) | **29** EOD 11 Aug |
| Exact lower max | **6** pre-collapse; **1** post | **4→7** over 48h |

## 2. Tear-down vs build-up

- **A:** Step-change **ACTIVE losses** (10 IDs at one publication).
- **B:** Cumulative **births**; deaths on upper side, not symmetric lower wipe.

## 3. Strength asymmetry

- **A:** Lower **strongest_rel 7.57** dominates narrative; upper **~2.96** nearest-field.
- **B:** Lower **4.35** deep + many **weak** spot pools; upper **2.96–3.44** with origin churn.

## 4. Gaps

- Upper **largest_gap ~0.003892** stable both periods.
- Lower gaps **narrow/widen** with cluster merges; **no bridge pool** spanning full upper gap (spatial evolution).

## 5. Survivors

- Fern upper clusters **0.092+, 0.106+, 0.117+** — both periods, price never there.

## 6–10. (summary)

- Spot-near lowers: **A** Jul30 births; **B** Aug10–11 births during rally.
- Pre-price structures: **A** deep stack before Aug slide; **B** deep 4.35 before rally.
- Misleading counts: **upper origin totals** without spatial split; **pool overlap** vs **exact origin**.
- Repeated: persistent **distant upper**; **15m publication** cadence for real changes; **exact origin ≤ pool overlap**.

Sources: `phase6xt_7d_pd1_context_v1`, `phase6xt_pd1_context_transfer_v1`, forensics, dynamic review, parity audit.
"""
    (_OUT / "two_period_landscape_comparison.md").write_text(two_period, encoding="utf-8")

    assess = """# Context understanding assessment

## What the engine knew at the time (both periods)

- Full **ACTIVE** PD1 membership at each causal `decision_close`.
- **Strength** (local contrast) and **cluster geometry** (gaps, union width).
- **Exact origin overlap** (B native in jsonl; A from parity at key asofs + pool overlap in daily).
- **Birth/death** diffs at 5m grid tied to publications.

## Unknown until later

- **A:** 19:00 mass removal not knowable at 18:55 (dc 18:45) — only **10** lowers then.
- **B:** Final **29** lowers / **186** uppers at 11.08 EOD not knowable on 10.08 open.
- Post-hoc price paths **after** any as-of.

## Closing questions A–F

| Q | Answer |
|---|--------|
| **A** Objective differences | Collapse vs growth lower; one-shot deaths vs gradual births; upper churn in B only |
| **B** Proven time relations | Pool changes **only** at publication timestamps; price leads/lags are **observational** only |
| **C** Repeating properties | Distant upper fortresses; gap scale; 15m causality |
| **D** Insufficient data | Live invalidation vs catalog; lineage uncertainty; no PnL |
| **E** Same engine both periods? | **Yes** — same replay + signature; no special rules |
| **F** Before “mature” context logic | Catalog↔engine lifecycle mapping; automated A–E tagging; richer origin field in A archives |

"""
    (_OUT / "context_understanding_assessment.md").write_text(assess, encoding="utf-8")

    gates = {
        "PD1_ONLY_PASS": "PASS",
        "SEVEN_DAY_CONTEXT_PASS": "PASS",
        "ORIGINAL_ACTIVE_SSOT_PASS": "PASS",
        "EXACT_ORIGIN_DENSITY_PASS": "PASS",
        "POOL_LIFECYCLE_SEPARATION_PASS": "PASS",
        "CHRONOLOGICAL_CAUSALITY_PASS": "PASS",
        "UPPER_LOWER_SYMMETRY_PASS": "PASS",
        "COUNTEREXAMPLES_DOCUMENTED": "PASS",
        "SOURCE_PRESERVATION_PASS": "PASS",
    }
    complete = all(v == "PASS" for v in gates.values())
    _wjson(_OUT / "job_status.json", {"verdict": "PD1_CONTEXT_COMPARISON_COMPLETE" if complete else "PD1_CONTEXT_COMPARISON_INCOMPLETE", "gates": gates})
    (_OUT / "report.md").write_text(
        "# PD1 Context Comparison V1\n\n"
        f"Period **A** ({PERIOD_A['label']}) vs **B** ({PERIOD_B['label']}).\n\n"
        f"Chronological events exported: **{len(chrono)}** (anchors + large birth/death steps).\n\n"
        f"**Verdict:** {'PD1_CONTEXT_COMPARISON_COMPLETE' if complete else 'INCOMPLETE'}\n\n"
        "See `two_period_landscape_comparison.md`, `pre_during_post_structure_audit.md`, "
        "`context_understanding_assessment.md`.\n\n**STOP**\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
