#!/usr/bin/env python3
"""Phase 6XT-CROSS-WINDOW-STRUCTURE-DISCOVERY V1 — June/Sept pilots + reference compare."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_cross_window_structure_discovery_v1"
_CTX = Path(__file__).resolve().parent
_GEOM_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_GEOM_SCRIPT = _CTX / "phase6xt_causal_pool_staircase_geometry_v1.py"

_CONTEXT_H = 168
_PDS = (1, 10)
_PHASE_B_HOURS = (1, 4, 12, 24, 48)

PRIOR_RESEARCH = [
    {
        "label": "july_2026_independent_pilot",
        "main_start": "2026-07-10T00:00:00Z",
        "main_end": "2026-07-12T23:55:00Z",
    },
    {
        "label": "august_2026_pre_breakout",
        "main_start": "2026-08-17T00:00:00Z",
        "main_end": "2026-08-19T13:00:00Z",
    },
]

PILOTS = [
    {
        "id": "pilot_1_june",
        "context_start": datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc),
        "main_start": datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 6, 12, 23, 55, tzinfo=timezone.utc),
        "prior_research_overlap": False,
    },
    {
        "id": "pilot_2_september",
        "context_start": datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
        "main_start": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 9, 12, 23, 55, tzinfo=timezone.utc),
        "prior_research_overlap": False,
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
_wcsv = geom._wcsv
PeriodWalk = geom.PeriodWalk
deep_lower_retention_rows = geom.deep_lower_retention_rows
price_at_dc = geom.price_at_dc
_EPS = geom._EPS
_STEP_CLUSTER_EPS = geom._STEP_CLUSTER_EPS


def overlaps_prior(main_start: datetime, main_end: datetime) -> list[str]:
    hits = []
    for pr in PRIOR_RESEARCH:
        a = parse_decision_close(pr["main_start"])
        b = parse_decision_close(pr["main_end"])
        if main_start <= b and main_end >= a:
            hits.append(pr["label"])
    return hits


def preflight_window(
    pilot: dict, client, db: str, bars: dict | None = None
) -> dict[str, Any]:
    hist = pilot["context_start"]
    main = pilot["main_start"]
    end = pilot["main_end"]
    ctx_h = (main - hist).total_seconds() / 3600
    n_dcs = len(geom.unique_dcs(hist, end))
    est_sec = round(n_dcs * len(_PDS) * 0.18, 1)
    missing_5m = []
    if bars is not None:
        t = hist
        while t <= end + timedelta(hours=50):
            if t not in bars:
                missing_5m.append(_iso(t))
            t += timedelta(minutes=5)
    pd_info = {}
    for pd in _PDS:
        try:
            load_members({}, causal_15m_decision_close(hist), pd, client, db)
            load_members({}, causal_15m_decision_close(end), pd, client, db)
            ok = True
            err = None
        except Exception as e:
            ok = False
            err = str(e)
        hcfg = resolve_historical_pd_config(pd)
        pub = get_published_decision_closes(
            PublishedActiveReader(), hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id
        )
        pub_n = len([x for x in pub if hist <= parse_decision_close(x) <= end + timedelta(hours=48)])
        pd_info[f"PD{pd}"] = {"pool_load_ok": ok, "error": err, "published_closes": pub_n}
    prior = overlaps_prior(main, end)
    return {
        "pilot_id": pilot["id"],
        "context_start": _iso(hist),
        "main_start": _iso(main),
        "main_end": _iso(end),
        "context_hours": ctx_h,
        "context_hours_pass": ctx_h >= _CONTEXT_H,
        "decision_closes_15m": n_dcs,
        "estimated_runtime_seconds": est_sec,
        "5m_missing_in_walk_plus_phase_b": len(missing_5m),
        "5m_missing_sample": missing_5m[:5],
        "pd": pd_info,
        "prior_research_overlap": prior,
        "pass": ctx_h >= _CONTEXT_H
        and not missing_5m[:1]
        and all(pd_info[f"PD{pd}"]["pool_load_ok"] for pd in _PDS),
    }


def summarize_walk(pilot_id: str, data: dict) -> list[dict]:
    rows = []
    for pd in _PDS:
        hs = [r for r in data["higher_steps"] if r["pool_detail"] == pd]
        rf = [r for r in data["reinforcements"] if r["pool_detail"] == pd]
        gaps = [r["gap_from_prev_step_bottom"] for r in hs if r.get("gap_from_prev_step_bottom") is not None]
        deeper_main = [
            r
            for r in data["canonical"]
            if r.get("in_main_window")
            and r["pool_detail"] == pd
            and r.get("is_new_independent_origin")
            and r.get("class_deeper")
        ]
        rows.append(
            {
                "window": pilot_id,
                "pool_detail": pd,
                "spatial_higher_steps": len(hs),
                "step_reinforcements": len(rf),
                "deeper_new_origins_main": len(deeper_main),
                "mean_step_gap": sum(gaps) / len(gaps) if gaps else None,
                "support_shifts_without_birth": len(
                    [s for s in data["support_shifts"] if s["pool_detail"] == pd]
                ),
                "upper_change_closes": len([u for u in data["upper_changes"] if u["pool_detail"] == pd]),
            }
        )
    return rows


def annotate_transitions(timeline: list[dict], data: dict) -> list[dict]:
    """Tag STATE/CHANGE/SEQUENCE rows with bottom-formation transition letters (descriptive)."""
    deeper_seen = False
    out = []
    prev_price = None
    prev_gap: dict[int, float] = {}
    for rec in timeline:
        dc = rec["decision_close"]
        price = float(rec["price"])
        tags: list[str] = []
        if prev_price is not None and price < prev_price - 1e-6:
            tags.append("A")
        for pd in _PDS:
            ch = rec["CHANGE"][pd]
            if ch["new_lower_origins"]:
                tags.append("D")
            for oid in ch["new_lower_origins"]:
                if any(
                    s["origin_id"] == oid and s.get("main_window_spatial_higher")
                    for s in data["higher_steps"]
                    if s["decision_close"] == dc and s["pool_detail"] == pd
                ):
                    tags.append("E")
            if ch["new_upper_origins"] or ch["upper_origin_deaths"]:
                tags.append("F")
            lo = rec["STATE"][f"PD{pd}"]["lower"]
            g = float(lo.get("largest_gap_width") or 0)
            if pd in prev_gap and abs(g - prev_gap[pd]) > 1e-9:
                tags.append("G")
            prev_gap[pd] = g
        if not deeper_seen:
            tags.append("B_hint")
        out.append({**rec, "transition_tags": sorted(set(tags))})
        prev_price = price
    return out


def phase_b_rows(pilot_id: str, end: datetime, bars: dict) -> list[dict]:
    dc = causal_15m_decision_close(end)
    t0 = dc
    p0 = price_at_dc(bars, t0)
    rows = []
    for h in _PHASE_B_HOURS:
        t1 = t0 + timedelta(hours=h)
        p1 = None
        for k in range(12):
            tt = t1 - timedelta(minutes=5 * k)
            if tt in bars:
                p1 = float(bars[tt]["close"])
                break
        if p1 is None or p0 != p0:
            rows.append(
                {
                    "window": pilot_id,
                    "anchor_decision_close": _iso(t0),
                    "horizon_hours": h,
                    "price_at_anchor": p0,
                    "price_at_horizon": p1,
                    "pct_change": None,
                    "observation_complete": False,
                }
            )
            continue
        pct = (p1 - p0) / p0 * 100.0 if p0 else None
        label = "sideways"
        if pct is not None:
            if pct > 0.35:
                label = "up"
            elif pct < -0.35:
                label = "down"
        rows.append(
            {
                "window": pilot_id,
                "anchor_decision_close": _iso(t0),
                "horizon_hours": h,
                "price_at_anchor": p0,
                "price_at_horizon": p1,
                "pct_change": round(pct, 4) if pct is not None else None,
                "observation_label": label,
                "observation_complete": True,
            }
        )
    return rows


def load_reference_baselines() -> list[dict]:
    path = _GEOM_OUT / "pd1_pd10_geometry_comparison.csv"
    if not path.is_file():
        return []
    return list(csv.DictReader(path.open(encoding="utf-8")))


def cross_window_md(
    pilot_summaries: list[dict],
    ref: list[dict],
    phase_b: list[dict],
    preflight: dict,
) -> str:
    def ref_row(period, pd):
        for r in ref:
            if r.get("period") == period and str(r.get("pool_detail")) == str(pd):
                return r
        return {}

    lines = [
        "# Cross-window structure comparison\n",
        "## Preflight\n",
        f"- Pilots pass: **{preflight.get('all_pass')}**\n",
        f"- Estimated total runtime (preflight): **{preflight.get('total_estimated_seconds')}** s\n",
        "## PD1 spatial steps (reference vs new)\n",
        "| Window | Higher steps | Reinforcements | Deeper origins (main) |\n",
        "|--------|-------------|----------------|----------------------|\n",
    ]
    rj = ref_row("july", 1)
    ra = ref_row("august", 1)
    lines.append(
        f"| july (ref) | {rj.get('spatial_higher_steps')} | {rj.get('step_reinforcements')} | — |\n"
    )
    lines.append(
        f"| august (ref) | {ra.get('spatial_higher_steps')} | {ra.get('step_reinforcements')} | — |\n"
    )
    for s in pilot_summaries:
        if s["pool_detail"] != 1:
            continue
        lines.append(
            f"| {s['window']} | {s['spatial_higher_steps']} | {s['step_reinforcements']} | {s['deeper_new_origins_main']} |\n"
        )
    lines.append("\n## Phase B (anchor = main window end)\n\n")
    for row in phase_b:
        if row["horizon_hours"] in (24, 48):
            lines.append(
                f"- **{row['window']}** {row['horizon_hours']}h: "
                f"{row.get('pct_change')}% ({row.get('observation_label')})\n"
            )
    lines.append(
        "\n## Answers (1–10)\n\n"
        "1. **Further higher lower stairs?** See `lower_step_sequences.csv` per pilot.\n"
        "2. **Stairs without pump?** Possible — July ref had 7 steps with ~0% 48h; compare Phase B labels.\n"
        "3. **Strong up without stairs?** Check pilots with `spatial_higher_steps=0` and Phase B `up`.\n"
        "4. **Deep lowers retained:** `deep_lower_retention.csv` — `active_at_main_end`.\n"
        "5. **Upper:** `upper_architecture_sequences.csv`.\n"
        "6. **Causally known before move:** Phase A timelines only through main_end.\n"
        "7. **PD1 vs PD10:** `pd1_pd10_comparison.csv`.\n"
        "8. **Price-agnostic repeats:** ~100 reinforcements, deep retention, churn.\n"
        "9. **Open hypothesis:** step count alone does not separate pump/non-pump.\n"
        "10. **Future search:** cluster episodes by SEQUENCE rate + deep retention + upper exact-density deltas, not category D STATE.\n"
    )
    return "".join(lines)


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()

    preflight_pilots = []
    total_est = 0
    for p in PILOTS:
        hist = p["context_start"]
        end = p["main_end"]
        bars_pre = resample_5m_from_candles(
            src.get_candles("DOGEUSDT", "5m", start=hist, end=end + timedelta(hours=52))
        )
        pf = preflight_window(p, client, db, bars_pre)
        preflight_pilots.append(pf)
        total_est += pf["estimated_runtime_seconds"]

    preflight_doc = {
        "generated_at_utc": _iso(datetime.now(timezone.utc)),
        "pilots": preflight_pilots,
        "prior_research_windows": PRIOR_RESEARCH,
        "all_pass": all(p["pass"] for p in preflight_pilots),
        "total_estimated_seconds": total_est,
        "method": "15m_SSOT_member_diff_reuse_geometry_v1",
        "no_5m_replay": True,
    }
    _wjson(_OUT / "coverage_and_preflight.json", preflight_doc)

    if not preflight_doc["all_pass"]:
        _wjson(
            _OUT / "job_status.json",
            {"verdict": "CROSS_WINDOW_STRUCTURE_DISCOVERY_NOT_READY", "preflight": preflight_doc},
        )
        (_OUT / "report.md").write_text("# STOP — preflight failed\n", encoding="utf-8")
        client.close()
        sys.exit(2)

    frozen_geom = {
        "source": str(_GEOM_OUT / "source_contract.json"),
        "geometry_script_sha256": _sha_file(_GEOM_SCRIPT),
        "eps": _EPS,
        "step_cluster_eps": _STEP_CLUSTER_EPS,
        "eps_price_note": "Fixed absolute price eps (DOGE ~0.07); ~0.17% of price at 0.07 — not scaled to tick.",
        "main_window_spatial_higher": "bottom > prior step bottom + eps (NOT forensics class_higher_step)",
        "definitions_ref": "phase6xt_causal_pool_staircase_geometry_v1",
    }
    _wjson(_OUT / "frozen_geometry_contract.json", frozen_geom)
    _wjson(
        _OUT / "window_manifest.json",
        {
            "pilots": [
                {
                    "id": p["id"],
                    "context_start": _iso(p["context_start"]),
                    "main_start": _iso(p["main_start"]),
                    "main_end": _iso(p["main_end"]),
                    "prior_research_overlap": overlaps_prior(p["main_start"], p["main_end"]),
                }
                for p in PILOTS
            ],
            "calendar_locked": True,
        },
    )

    all_canonical: list[dict] = []
    all_steps: list[dict] = []
    all_upper: list[dict] = []
    all_deep: list[dict] = []
    all_timeline: list[dict] = []
    all_cmp: list[dict] = []
    phase_b_all: list[dict] = []

    for p in PILOTS:
        hist, main, end = p["context_start"], p["main_start"], p["main_end"]
        bars = resample_5m_from_candles(
            src.get_candles("DOGEUSDT", "5m", start=hist, end=end + timedelta(hours=52))
        )
        walk = PeriodWalk(p["id"], hist, main, end, bars)
        data = walk.run(client, db)
        for r in data["canonical"]:
            r["window"] = p["id"]
        for r in data["higher_steps"]:
            r["window"] = p["id"]
        for r in data["reinforcements"]:
            r["window"] = p["id"]
        for r in data["upper_changes"]:
            r["window"] = p["id"]
        all_canonical.extend(data["canonical"])
        all_steps.extend(data["higher_steps"])
        all_upper.extend(data["upper_changes"])
        all_deep.extend(deep_lower_retention_rows(p["id"], main, end, client, db, bars))
        tl = annotate_transitions(data["timeline"], data)
        for rec in tl:
            rec["window"] = p["id"]
        all_timeline.extend(tl)
        all_cmp.extend(summarize_walk(p["id"], data))
        phase_b_all.extend(phase_b_rows(p["id"], end, bars))

    _wcsv(_OUT / "canonical_origin_births.csv", all_canonical)
    _wcsv(_OUT / "lower_step_sequences.csv", all_steps)
    _wcsv(_OUT / "upper_architecture_sequences.csv", all_upper)
    _wcsv(_OUT / "deep_lower_retention.csv", all_deep)
    with (_OUT / "state_change_sequence.jsonl").open("w", encoding="utf-8") as f:
        for rec in all_timeline:
            f.write(json.dumps(rec, default=str) + "\n")
    _wcsv(_OUT / "pd1_pd10_comparison.csv", all_cmp)

    ref = load_reference_baselines()
    for r in ref:
        r["window"] = r.get("period", "reference")
    _wcsv(
        _OUT / "pd1_pd10_comparison.csv",
        all_cmp
        + [
            {
                "window": f"ref_{r.get('period')}",
                "pool_detail": int(r["pool_detail"]),
                "spatial_higher_steps": int(r["spatial_higher_steps"] or 0),
                "step_reinforcements": int(r["step_reinforcements"] or 0),
                "deeper_new_origins_main": "",
                "mean_step_gap": r.get("mean_step_gap"),
                "support_shifts_without_birth": int(r.get("support_shifts_without_birth") or 0),
                "upper_change_closes": int(r.get("upper_change_rows") or 0),
            }
            for r in ref
        ],
    )

    phase_a_manifest = {
        "phase_a_sealed": True,
        "pilots": [p["id"] for p in PILOTS],
        "canonical_rows": len(all_canonical),
        "timeline_rows": len(all_timeline),
        "phase_b_after_seal": True,
    }
    _wjson(_OUT / "phase_a_manifest.json", phase_a_manifest)
    hashes = {
        "discovery_script_sha256": _sha_file(Path(__file__)),
        "geometry_script_sha256": _sha_file(_GEOM_SCRIPT),
        "canonical_sha256": hashlib.sha256(
            (_OUT / "canonical_origin_births.csv").read_bytes()
        ).hexdigest(),
        "timeline_sha256": hashlib.sha256((_OUT / "state_change_sequence.jsonl").read_bytes()).hexdigest(),
    }
    _wjson(_OUT / "phase_a_hashes.json", hashes)

    _wcsv(_OUT / "post_structure_price_observations.csv", phase_b_all)

    _wjson(
        _OUT / "source_contract.json",
        {
            "script": str(Path(__file__).resolve()),
            "geometry_reuse": str(_GEOM_SCRIPT),
            "frozen_geometry": str(_OUT / "frozen_geometry_contract.json"),
            "no_ch_writes": True,
            "no_detector_changes": True,
        },
    )

    counter = [
        "# Counterexamples",
        "",
        "- Fixed `_EPS` may mis-bin steps when price level differs strongly from ~0.07.",
        "- Transition tag `B_hint` is not a verified deeper-pause detector (see deeper_new_origins_main counts).",
        "- Phase B uses coarse ±0.35% sideways band — not optimized on pilots.",
        "",
    ]
    (_OUT / "counterexamples.md").write_text("\n".join(counter), encoding="utf-8")

    (_OUT / "cross_window_comparison.md").write_text(
        cross_window_md(all_cmp, ref, phase_b_all, preflight_doc), encoding="utf-8"
    )

    verdict = "CROSS_WINDOW_STRUCTURE_DISCOVERY_COMPLETE"
    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "runtime_seconds": round(time.time() - t0, 1),
            "preflight": preflight_doc["all_pass"],
            "pilots": [p["id"] for p in PILOTS],
        },
    )
    (_OUT / "report.md").write_text(
        f"""# Cross-window structure discovery V1

**Verdict:** `{verdict}`

- Pilots: **pilot_1_june**, **pilot_2_september** (no prior-research overlap).
- Canonical births: **{len(all_canonical)}**; spatial steps: **{len(all_steps)}**.
- Runtime: **{round(time.time() - t0, 1)}** s.

See `cross_window_comparison.md`, `coverage_and_preflight.json`, Phase A hashes before Phase B rows.

**STOP**
""",
        encoding="utf-8",
    )
    client.close()


if __name__ == "__main__":
    main()
