#!/usr/bin/env python3
"""Phase 6XT-DYNAMIC-LANDSCAPE-TRANSITION-ANALYZER V1 — spatial transition read-only."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_dynamic_landscape_transition_analyzer_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_FULL = _REPO / "logs/historical_import/overnight_2026/phase6xt_full_landscape_sequence_comparison_v1"

_EPS = 0.00012
_STEP_EPS = 0.00012
_PRICE_ONLY_MIN_MOVE = 0.00015
_CONTEXT_H = 168

CASES = [
    {
        "case_id": "june",
        "main_start": datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 6, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc),
    },
    {
        "case_id": "july",
        "main_start": datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 7, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc),
    },
    {
        "case_id": "august",
        "main_start": datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
    },
    {
        "case_id": "september",
        "main_start": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 9, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
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
from phase6xt_pd1_density_parity_audit_v1 import exact_max_origin_overlap  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.dynamic_liquidity_landscape_v1 import analyze_side  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
_wjson = c7._wjson
_wcsv = geom._wcsv
load_members = c7.load_members
side_pool_rows = c7.side_pool_rows
unique_dcs = geom.unique_dcs
price_at_dc = geom.price_at_dc


def origins_by_side(members: list[dict], side: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for m in members:
        if m.get("side") != side:
            continue
        oid = origin_logical_v2_key(m["pool_id"])
        rec = out.setdefault(
            oid,
            {
                "origin_id": oid,
                "pool_ids": [],
                "bottom": float("inf"),
                "top": float("-inf"),
                "strength_max": 0.0,
                "known_at": m.get("known_at"),
            },
        )
        rec["pool_ids"].append(m["pool_id"])
        b, t = float(m["lower_bound"]), float(m["upper_bound"])
        rec["bottom"] = min(rec["bottom"], b)
        rec["top"] = max(rec["top"], t)
        rec["strength_max"] = max(rec["strength_max"], float(m.get("strength") or 0))
    return out


def fingerprint_origins(omap: dict[str, dict]) -> str:
    parts = []
    for oid in sorted(omap):
        r = omap[oid]
        parts.append(f"{oid}:{r['bottom']:.8f}:{r['top']:.8f}:{len(r['pool_ids'])}")
    h = hashlib.sha256()
    h.update("|".join(parts).encode())
    return h.hexdigest()[:16]


def interval_covered(lo: float, hi: float, intervals: list[tuple[float, float]]) -> bool:
    for a, b in intervals:
        if lo >= a - _EPS and hi <= b + _EPS:
            return True
        if lo <= b + _EPS and hi >= a - _EPS:
            if hi - lo < (min(b, hi) - max(a, lo)) + _EPS:
                continue
            return True
    return False


def merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not intervals:
        return []
    ints = sorted(intervals)
    merged = [ints[0]]
    for lo, hi in ints[1:]:
        plo, phi = merged[-1]
        if lo <= phi + _EPS:
            merged[-1] = (plo, max(phi, hi))
        else:
            merged.append((lo, hi))
    return merged


def support_top_below(omap: dict[str, dict], price: float) -> float | None:
    best = None
    for r in omap.values():
        if r["top"] < price and (best is None or r["top"] > best):
            best = r["top"]
    return best


def nearest_upper_bottom(omap: dict[str, dict], price: float) -> float | None:
    best = None
    for r in omap.values():
        if r["bottom"] > price and (best is None or r["bottom"] < best):
            best = r["bottom"]
    return best


def classify_new_origin(
    side: str,
    rec: dict,
    prev_omap: dict[str, dict],
    price: float,
    prev_price: float,
    last_step_bottom: float | None,
) -> str:
    bot, top = rec["bottom"], rec["top"]
    prev_intervals = [(r["bottom"], r["top"]) for r in prev_omap.values()]
    if interval_covered(bot, top, prev_intervals):
        return "EXISTING_ZONE_REINFORCEMENT"
    for r in prev_omap.values():
        if abs(bot - r["bottom"]) <= _STEP_EPS or abs(top - r["top"]) <= _STEP_EPS:
            return "EXISTING_ZONE_REINFORCEMENT"
    if side == "lower":
        ref = last_step_bottom if last_step_bottom is not None else support_top_below(prev_omap, prev_price)
        if ref is not None and bot > ref + _EPS:
            return "TRUE_UPWARD_FORMATION"
    if side == "upper":
        ref = nearest_upper_bottom(prev_omap, prev_price)
        if ref is not None and bot > ref + _EPS:
            return "TRUE_UPWARD_FORMATION"
    return "TRUE_UPWARD_FORMATION" if side == "upper" else "EXISTING_ZONE_REINFORCEMENT"


def analyze_case(
    case: dict,
    client,
    db: str,
    bars: dict,
    pd: int,
) -> dict[str, list]:
    cache: dict = {}
    dcs = unique_dcs(case["main_start"], case["main_end"])
    events: list[dict] = []
    new_cov: list[dict] = []
    lost_cov: list[dict] = []
    density_chg: list[dict] = []
    gap_evo: list[dict] = []
    price_only: list[dict] = []
    last_step_bottom: float | None = None

    prev_om: dict[str, dict] = {"lower": {}, "upper": {}}
    prev_price: float | None = None
    prev_fp: dict[str, str] = {"lower": "", "upper": ""}
    prev_gap: float | None = None
    prev_ex_max: dict[str, int] = {"lower": 0, "upper": 0}

    # bootstrap previous close at main_start - 15m
    dc0 = dcs[0] - timedelta(minutes=15)
    mem0 = load_members(cache, dc0, pd, client, db)
    for side in ("lower", "upper"):
        prev_om[side] = origins_by_side(mem0, side)
        prev_fp[side] = fingerprint_origins(prev_om[side])

    for dc in dcs:
        dc_s = _iso(dc)
        mem = load_members(cache, dc, pd, client, db)
        price = price_at_dc(bars, dc)
        row_base = {"case_id": case["case_id"], "pool_detail": pd, "decision_close": dc_s, "price": price}

        cur_om = {s: origins_by_side(mem, s) for s in ("lower", "upper")}
        cur_fp = {s: fingerprint_origins(cur_om[s]) for s in ("lower", "upper")}

        for side in ("lower", "upper"):
            ex = exact_max_origin_overlap(mem, side)
            rows = side_pool_rows(mem, side)
            an = analyze_side(rows, price, dedupe_micro=False) if rows else {}
            gaps = an.get("gaps") or []
            lg = max((float(g["gap_width"]) for g in gaps), default=0.0)

            density_chg.append(
                {
                    **row_base,
                    "side": side,
                    "exact_origin_overlap_max": ex["max"],
                    "delta_exact_max": ex["max"] - prev_ex_max[side],
                    "logical_origins": len(cur_om[side]),
                }
            )
            prev_ex_max[side] = ex["max"]

            if side == "lower":
                sup = support_top_below(cur_om["lower"], price)
                upb = nearest_upper_bottom(cur_om["upper"], price)
                gul = (upb - sup) if sup is not None and upb is not None else None
                gap_evo.append(
                    {
                        **row_base,
                        "support_top_below_price": sup,
                        "nearest_upper_bottom": upb,
                        "upper_lower_gap": gul,
                        "delta_gap": (gul - prev_gap) if prev_gap is not None and gul is not None else None,
                        "lower_largest_gap_width": lg,
                    }
                )
                prev_gap = gul

            # removals
            for oid, rec in prev_om[side].items():
                if oid not in cur_om[side]:
                    lost_cov.append(
                        {
                            **row_base,
                            "side": side,
                            "origin_id": oid,
                            "old_bottom": rec["bottom"],
                            "old_top": rec["top"],
                            "pool_ids": ",".join(rec["pool_ids"][:6]),
                            "label": "POOL_REMOVAL",
                        }
                    )
                    events.append(
                        {
                            **row_base,
                            "side": side,
                            "category": "C" if side == "lower" else "G",
                            "diagnostic_label": "POOL_REMOVAL",
                            "origin_id": oid,
                            "old_bounds": [rec["bottom"], rec["top"]],
                            "new_bounds": None,
                            "pool_ids": rec["pool_ids"],
                        }
                    )

            prev_intervals = merge_intervals([(r["bottom"], r["top"]) for r in prev_om[side].values()])
            for oid, rec in cur_om[side].items():
                if oid in prev_om[side]:
                    continue
                lab = classify_new_origin(side, rec, prev_om[side], price, prev_price or price, last_step_bottom)
                if lab == "TRUE_UPWARD_FORMATION" and side == "lower":
                    last_step_bottom = rec["bottom"]
                cat = "A" if lab == "TRUE_UPWARD_FORMATION" and side == "lower" else "E" if side == "upper" else "B"
                if lab == "EXISTING_ZONE_REINFORCEMENT":
                    cat = "B" if side == "lower" else "F"
                events.append(
                    {
                        **row_base,
                        "side": side,
                        "category": cat,
                        "diagnostic_label": lab,
                        "origin_id": oid,
                        "old_bounds": None,
                        "new_bounds": [rec["bottom"], rec["top"]],
                        "pool_ids": rec["pool_ids"],
                        "strength_max": rec["strength_max"],
                    }
                )
                if not interval_covered(rec["bottom"], rec["top"], prev_intervals):
                    new_cov.append(
                        {
                            **row_base,
                            "side": side,
                            "origin_id": oid,
                            "bottom": rec["bottom"],
                            "top": rec["top"],
                            "diagnostic_label": lab,
                        }
                    )

            if prev_fp[side] == cur_fp[side] and prev_price is not None:
                if abs(price - prev_price) >= _PRICE_ONLY_MIN_MOVE:
                    price_only.append(
                        {
                            **row_base,
                            "side": side,
                            "diagnostic_label": "PRICE_ONLY_SHIFT",
                            "price_delta": price - prev_price,
                            "fingerprint_unchanged": True,
                        }
                    )
                    events.append(
                        {
                            **row_base,
                            "side": side,
                            "category": "I",
                            "diagnostic_label": "PRICE_ONLY_SHIFT",
                            "origin_id": None,
                            "price_delta": price - prev_price,
                        }
                    )

        prev_om = cur_om
        prev_fp = cur_fp
        prev_price = price

    return {
        "events": events,
        "new_cov": new_cov,
        "lost_cov": lost_cov,
        "density_chg": density_chg,
        "gap_evo": gap_evo,
        "price_only": price_only,
    }


def build_sequences(events: list[dict], side: str) -> list[dict]:
    seq: list[dict] = []
    cur_id = 0
    open_seq: dict | None = None
    for e in sorted(events, key=lambda x: x["decision_close"]):
        if e.get("side") != side:
            continue
        if e.get("diagnostic_label") not in ("TRUE_UPWARD_FORMATION", "EXISTING_ZONE_REINFORCEMENT"):
            if open_seq:
                seq.append(open_seq)
                open_seq = None
            continue
        if e.get("diagnostic_label") == "TRUE_UPWARD_FORMATION":
            if open_seq:
                seq.append(open_seq)
            cur_id += 1
            open_seq = {
                "sequence_id": cur_id,
                "case_id": e["case_id"],
                "pool_detail": e["pool_detail"],
                "side": side,
                "first_decision_close": e["decision_close"],
                "last_decision_close": e["decision_close"],
                "formation_events": 1,
                "reinforcement_events": 0,
                "origin_ids": [e.get("origin_id")],
            }
        elif open_seq:
            open_seq["last_decision_close"] = e["decision_close"]
            open_seq["reinforcement_events"] += 1
            open_seq["origin_ids"].append(e.get("origin_id"))
        else:
            cur_id += 1
            open_seq = {
                "sequence_id": cur_id,
                "case_id": e["case_id"],
                "pool_detail": e["pool_detail"],
                "side": side,
                "first_decision_close": e["decision_close"],
                "last_decision_close": e["decision_close"],
                "formation_events": 0,
                "reinforcement_events": 1,
                "origin_ids": [e.get("origin_id")],
            }
    if open_seq:
        seq.append(open_seq)
    for s in seq:
        s["origin_ids"] = ",".join([x for x in s["origin_ids"] if x][:20])
    return seq


def pd_comparison(all_events: list[dict]) -> str:
    lines = ["# PD1 vs PD10 transition counts\n\n", "| Case | PD | Upward lower | Reinforce lower | Pool removal lower | Price-only |\n", "|------|----|--------------|-----------------|----------------------|------------|\n"]
    for c in CASES:
        for pd in (1, 10):
            sub = [e for e in all_events if e["case_id"] == c["case_id"] and e["pool_detail"] == pd]
            lines.append(
                f"| {c['case_id']} | {pd} | "
                f"{sum(1 for e in sub if e.get('diagnostic_label')=='TRUE_UPWARD_FORMATION' and e.get('side')=='lower')} | "
                f"{sum(1 for e in sub if e.get('diagnostic_label')=='EXISTING_ZONE_REINFORCEMENT' and e.get('side')=='lower')} | "
                f"{sum(1 for e in sub if e.get('diagnostic_label')=='POOL_REMOVAL' and e.get('side')=='lower')} | "
                f"{sum(1 for e in sub if e.get('diagnostic_label')=='PRICE_ONLY_SHIFT')} |\n"
            )
    return "".join(lines)


def four_case_analysis(all_events: list[dict], gap_evo: list[dict]) -> str:
    def cnt(case, label, side=None):
        return sum(
            1
            for e in all_events
            if e["case_id"] == case
            and e.get("diagnostic_label") == label
            and (side is None or e.get("side") == side)
        )

    aug_up = cnt("august", "TRUE_UPWARD_FORMATION", "lower")
    aug_up_u = cnt("august", "TRUE_UPWARD_FORMATION", "upper")
    return f"""# Four-case transition analysis

## 1–2 August before 13:00 UTC

- Lower **TRUE_UPWARD_FORMATION** events (PD1+PD10): **{aug_up}** lower / **{aug_up_u}** upper (diagnostic labels).
- Reinforcement and churn coexist; pump after cutoff **not** in this Phase-A pass.

## 3 July

- Same label families present: lower upward formations **{cnt('july', 'TRUE_UPWARD_FORMATION', 'lower')}** (all PDs in export).

## 4 June / September

- June lower formations: **{cnt('june', 'TRUE_UPWARD_FORMATION', 'lower')}**; September: **{cnt('september', 'TRUE_UPWARD_FORMATION', 'lower')}**.

## 5–7 Coverage vs reinforcement

- `new_price_coverage.csv` vs `EXISTING_ZONE_REINFORCEMENT` in `spatial_transition_events.jsonl`.

## 8–9 Post-cutoff / price-only

- August rally after Phase A: **not** in event files.
- **PRICE_ONLY_SHIFT** rows: `price_only_vs_real_change.csv` (fingerprint-stable landscape).

## 10 User observation vs IDs

- TRUE_UPWARD_FORMATION backed by **origin_id** + bounds in events.
- Cluster merge/split lineage: **UNCERTAIN** without full cluster_lineage replay.

## Core question

**Can code separate real spatial uplift from churn?** **Partially:** new intervals + TRUE_UPWARD_FORMATION vs REINFORCEMENT; not fully — overlapping bounds and `_EPS` ambiguity remain.
"""


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()

    cov = {"cases": [], "context_hours_required": _CONTEXT_H, "pass": True}
    for case in CASES:
        h = (case["main_start"] - case["hist_start"]).total_seconds() / 3600
        try:
            load_members({}, causal_15m_decision_close(case["hist_start"]), 1, client, db)
            ok = True
        except Exception as e:
            ok = False
            err = str(e)
        else:
            err = None
        cov["cases"].append({"case_id": case["case_id"], "context_hours": h, "ssot_ok": ok, "error": err})
        if h < _CONTEXT_H or not ok:
            cov["pass"] = False
    _wjson(_OUT / "coverage_audit.json", cov)
    if not cov["pass"]:
        _wjson(_OUT / "job_status.json", {"verdict": "DYNAMIC_LANDSCAPE_TRANSITION_NOT_READY"})
        client.close()
        sys.exit(2)

    all_events: list[dict] = []
    new_cov, lost_cov, density, gap_evo, price_only = [], [], [], [], []
    lower_seq, upper_seq = [], []

    for case in CASES:
        bars = resample_5m_from_candles(
            src.get_candles(
                "DOGEUSDT",
                "5m",
                start=case["hist_start"],
                end=case["main_end"] + timedelta(hours=1),
            )
        )
        for pd in (1, 10):
            res = analyze_case(case, client, db, bars, pd)
            all_events.extend(res["events"])
            new_cov.extend(res["new_cov"])
            lost_cov.extend(res["lost_cov"])
            density.extend(res["density_chg"])
            gap_evo.extend(res["gap_evo"])
            price_only.extend(res["price_only"])
            lower_seq.extend(build_sequences(res["events"], "lower"))
            upper_seq.extend(build_sequences(res["events"], "upper"))

    client.close()

    with (_OUT / "spatial_transition_events.jsonl").open("w", encoding="utf-8") as f:
        for e in all_events:
            f.write(json.dumps(e, default=str) + "\n")

    _wcsv(_OUT / "new_price_coverage.csv", new_cov)
    _wcsv(_OUT / "lost_price_coverage.csv", lost_cov)
    _wcsv(_OUT / "origin_density_changes.csv", density)
    _wcsv(_OUT / "lower_spatial_sequences.csv", lower_seq)
    _wcsv(_OUT / "upper_spatial_sequences.csv", upper_seq)
    _wcsv(_OUT / "upper_lower_gap_evolution.csv", gap_evo)
    _wcsv(_OUT / "price_only_vs_real_change.csv", price_only)

    (_OUT / "pd1_pd10_comparison.md").write_text(pd_comparison(all_events), encoding="utf-8")
    (_OUT / "four_case_transition_analysis.md").write_text(four_case_analysis(all_events, gap_evo), encoding="utf-8")

    script = Path(__file__).resolve()
    hashes = {
        "analyzer_script_sha256": _sha_file(script),
        "geometry_script_sha256": _sha_file(_CTX / "phase6xt_causal_pool_staircase_geometry_v1.py"),
        "spatial_events_sha256": hashlib.sha256((_OUT / "spatial_transition_events.jsonl").read_bytes()).hexdigest(),
    }
    _wjson(_OUT / "source_hashes.json", hashes)
    _wjson(
        _OUT / "source_contract.json",
        {
            "script": str(script),
            "eps": _EPS,
            "step_eps": _STEP_EPS,
            "price_only_min_move": _PRICE_ONLY_MIN_MOVE,
            "definitions_from": "phase6xt_causal_pool_staircase_geometry_v1",
            "active_only": True,
            "no_ch_writes": True,
        },
    )
    _wjson(
        _OUT / "causality_audit.json",
        {
            "causal_closes_only": True,
            "no_outcome_fitting": True,
            "cluster_lineage_uncertain": True,
            "event_count": len(all_events),
        },
    )

    verdict = "DYNAMIC_LANDSCAPE_TRANSITION_COMPLETE"
    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "runtime_seconds": round(time.time() - t0, 1),
            "spatial_events": len(all_events),
            "true_upward_lower_pd1": sum(
                1
                for e in all_events
                if e.get("diagnostic_label") == "TRUE_UPWARD_FORMATION"
                and e.get("side") == "lower"
                and e.get("pool_detail") == 1
            ),
        },
    )
    (_OUT / "report.md").write_text(
        f"# Dynamic landscape transition analyzer V1\n\n**Verdict:** `{verdict}`\n\n"
        f"Events: **{len(all_events)}**. See `four_case_transition_analysis.md`.\n\n**STOP**\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
