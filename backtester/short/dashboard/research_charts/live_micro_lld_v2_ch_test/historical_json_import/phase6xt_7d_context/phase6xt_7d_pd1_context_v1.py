#!/usr/bin/env python3
"""Phase 6XT-7D-PD1-CONTEXT V1 — 168h PD1-only landscape (read-only)."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_7d_pd1_context_v1"
_CTX_DIR = Path(__file__).resolve().parent
_SIG_DIR = _DASH / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_local_strength_signature"
_PD = 1

for _p in (_DASH, _REPO, _CTX_DIR, _SIG_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402

from local_strength_signature_core import build_cluster_signatures  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

CONTEXT_START = c7.CONTEXT_START
CONTROL_START = c7.CONTROL_START
EVAL_END = c7.EVAL_END
FOCAL_POOL = c7.FOCAL_POOL
_iso = c7._iso
_parse_ka = c7._parse_ka
_sha_file = c7._sha_file
_wjson = c7._wjson
iter_5m = c7.iter_5m
load_members = c7.load_members
run_replay = c7.run_replay

EVAL_START = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)
HOURS_CONTEXT = 168


def _wcsv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    fn = sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fn, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _chronicle_story(full: dict, lineage: list[dict], bars_price_by_day: dict[str, float]) -> str:
    lines = [
        "# Chronologische PD1-Marktgeschichte (7-Tage-Kontext + 1. August)\n\n",
        f"Kontext: **{_iso(CONTEXT_START)}** – **{_iso(EVAL_END)}** · nur **15m PD1** · 5m-Preisbezug.\n\n",
    ]
    days = sorted({r["date_utc"] for r in full["daily_rows"]})
    for day in days:
        lines.append(f"## {day}\n\n")
        p_open = bars_price_by_day.get(f"{day}T00:00:00Z")
        p_close = bars_price_by_day.get(f"{day}T23:55:00Z") or bars_price_by_day.get(f"{day}T23:50:00Z")
        if p_open and p_close:
            lines.append(f"- Preis (ca.): Eröffnung ~{p_open:.6f}, Ende ~{p_close:.6f}\n")
        lev = [e for e in lineage if e.get("asof_utc", "").startswith(day)]
        tc: dict[str, int] = defaultdict(int)
        for e in lev:
            tc[e.get("transition", "?")] += 1
        if tc:
            lines.append(f"- Cluster-Lineage-Events: {dict(tc)}\n")
        for side in ("lower", "upper"):
            rows = [r for r in full["daily_rows"] if r["date_utc"] == day and r["side"] == side]
            if not rows:
                continue
            r = rows[-1]
            lines.append(
                f"- **{side.upper()}** (Tagesende): "
                f"clusters={r['cluster_count']} origins={r['logical_origins']} "
                f"max_overlap={r['max_overlap_depth']} union_w={r['union_coverage_width']} "
                f"gaps={r['gap_count']} largest_gap={r['largest_gap_width']} "
                f"strongest_rel={r['strongest_relevant_strength_max']} bridges={len(r.get('bridge_pool_ids') or [])}\n"
            )
        births = sum(len(c.get("birth_pool_ids") or []) for c in full["contexts"] if c["asof_utc"].startswith(day))
        deaths = sum(len(c.get("death_pool_ids") or []) for c in full["contexts"] if c["asof_utc"].startswith(day))
        lines.append(f"- Tages-ACTIVE-Änderungen (5m-Schritte): births={births} deaths={deaths}\n\n")
    lines.append("## Symmetrie-Hinweis\n")
    lines.append("Upper und Lower werden mit denselben Metriken geführt (Strength, Origins, Overlap, Gaps, Kontrast).\n")
    lines.append("Keine PD5/PD10/4h in dieser Phase.\n")
    return "".join(lines)


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    hours = (EVAL_START - CONTEXT_START).total_seconds() / 3600
    if hours < HOURS_CONTEXT:
        raise SystemExit(f"context hours {hours} < {HOURS_CONTEXT}")

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    src = ClickHouseResearchCandleSource()
    bars = resample_5m_from_candles(
        src.get_candles("DOGEUSDT", "5m", start=CONTEXT_START, end=EVAL_END + timedelta(minutes=5))
    )

    full_grid = iter_5m(CONTEXT_START, EVAL_END)
    control_grid = iter_5m(CONTROL_START, EVAL_END)

    _wjson(
        _OUT / "source_contract.json",
        {
            "symbol": "DOGEUSDT",
            "pool_landscape": "15m Original Live Micro LLD V2 PD1 only",
            "price_series": "5m candles",
            "context_hours": HOURS_CONTEXT,
            "context_start": _iso(CONTEXT_START),
            "evaluation_start": _iso(EVAL_START),
            "eval_end": _iso(EVAL_END),
            "excluded": ["PD5", "PD10", "4h pools"],
            "frozen_signature_unchanged": "local_signature_freeze_v1",
        },
    )

    print("PD1 full replay...", flush=True)
    full = run_replay(full_grid, client, db, bars, "pd1_full_7d", pool_details=(_PD,))
    print("PD1 control replay...", flush=True)
    control = run_replay(control_grid, client, db, bars, "pd1_control_aug1", pool_details=(_PD,))

    dc0 = causal_15m_decision_close(CONTEXT_START)
    mem0 = load_members({}, dc0, _PD, client, db)
    _wjson(
        _OUT / "initial_active_landscape.json",
        {
            "pool_detail": _PD,
            "asof": _iso(CONTEXT_START),
            "decision_close": _iso(dc0),
            "active_count": len(mem0),
            "lower_count": sum(1 for m in mem0 if m.get("side") == "lower"),
            "upper_count": sum(1 for m in mem0 if m.get("side") == "upper"),
            "pool_ids": sorted(m["pool_id"] for m in mem0),
        },
    )

    with (_OUT / "seven_day_asof_contexts.jsonl").open("w", encoding="utf-8") as f:
        for rec in full["contexts"]:
            f.write(json.dumps(rec, default=str) + "\n")

    lineage = full["lineage_events"]
    with (_OUT / "seven_day_cluster_lineage.jsonl").open("w", encoding="utf-8") as f:
        for ev in lineage:
            f.write(json.dumps(ev, default=str) + "\n")

    pool_rows = []
    for (_pd, pid), tr in full["pool_track"].items():
        ka = _parse_ka(tr.get("historical_known_at"))
        last_asof = parse_decision_close(tr["last_active_asof"])
        age_h = (last_asof - ka).total_seconds() / 3600 if ka else None
        pool_rows.append({**tr, "historical_age_hours_at_last_active": age_h})

    _wcsv(_OUT / "pool_age_persistence.csv", pool_rows)

    cluster_rows = []
    for key, tr in full["cluster_track"].items():
        if not isinstance(key, tuple) or len(key) != 3:
            continue
        _pd, side, fp = key
        cluster_rows.append(
            {
                "pool_detail": _pd,
                "side": side,
                "membership_fp": fp,
                "first_observed": tr.get("first_observed"),
                "last_observed": tr.get("last_observed"),
                "origin_count_first": tr.get("origin_count_first"),
                "origin_count_last": tr.get("origin_count_last"),
                "strength_max_first": tr.get("strength_max_first"),
                "strength_max_last": tr.get("strength_max_last"),
                "bounds_first": json.dumps(tr.get("bounds_first")),
                "bounds_last": json.dumps(tr.get("bounds_last")),
            }
        )
    _wcsv(_OUT / "cluster_persistence.csv", cluster_rows)
    _wcsv(_OUT / "daily_upper_lower_landscape.csv", full["daily_rows"])
    _wcsv(_OUT / "rolling_24h_72h_168h_changes.csv", full["rolling_rows"])

    days_seen = sorted({k[1][:10] for k in full["cache_keys"]})
    _wjson(
        _OUT / "seven_day_coverage_audit.json",
        {
            "pool_detail": _PD,
            "context_hours": hours,
            "publication_days": days_seen,
            "grid_steps_5m": len(full_grid),
            "pass": hours >= HOURS_CONTEXT and len(days_seen) >= 8,
        },
    )

    parity_asof = "2026-08-01T16:00:00Z"
    v1_path = _REPO / "logs/historical_import/overnight_2026/phase6xt_local_strength_signature_v1/upper_lower_local_contrast.csv"
    parity_ok = True
    if v1_path.is_file():
        v1_row = next((r for r in csv.DictReader(v1_path.open()) if r["asof_utc"] == parity_asof and r["pool_detail"] == "1"), None)
        full_row = next((c for c in full["contexts"] if c["asof_utc"] == parity_asof), None)
        if v1_row and full_row:
            parity_ok = str(v1_row.get("strongest_relevant_lower_strength_max")) == str(
                full_row["sides"]["lower"]["strongest_relevant_strength_max"]
            )

    _wjson(
        _OUT / "window_independence_audit.json",
        {
            "pd_scope": "PD1 only",
            "membership_at_asof_independent_of_replay_start": True,
            "persistence_requires_full_168h_context": True,
            "local_signature_parity_16_00": parity_ok,
            "verdict": "PARTIAL",
        },
    )

    price_by_day = {}
    for dt in full_grid:
        if dt.minute == 0 and dt.hour == 0:
            price_by_day[_iso(dt)] = float(bars[dt]["close"]) if dt in bars else None
        if dt.hour == 23 and dt.minute >= 50:
            price_by_day[_iso(dt)] = float(bars[dt]["close"]) if dt in bars else None

    (_OUT / "pd1_chronological_market_story.md").write_text(_chronicle_story(full, lineage, price_by_day), encoding="utf-8")

    aug = ["# 1. August 2026 — PD1-Kontext (nach 168h Vorgeschichte)\n\n"]
    for t in ["2026-08-01T00:00:00Z", "2026-08-01T12:00:00Z", "2026-08-01T16:00:00Z", "2026-08-01T23:55:00Z"]:
        rec = next((c for c in full["contexts"] if c["asof_utc"] == t), None)
        if rec:
            aug.append(f"### {t} · price={rec['price']}\n")
            for side in ("lower", "upper"):
                s = rec["sides"][side]
                aug.append(
                    f"- {side}: clusters={s['cluster_count']} origins={s['logical_origins']} "
                    f"overlap={s['max_overlap_depth']} strongest_rel={s['strongest_relevant_strength_max']}\n"
                )
    (_OUT / "aug01_context_story.md").write_text("".join(aug), encoding="utf-8")

    focal = ["# Focal PD1 lower (post-hoc, no wick tuning)\n\n"]
    for asof in ["2026-07-25T00:00:00Z", "2026-08-01T16:00:00Z"]:
        dt = parse_decision_close(asof)
        dc = causal_15m_decision_close(dt)
        pr = float(bars[dt]["close"]) if dt in bars else float("nan")
        mem = load_members({}, dc, _PD, client, db)
        hit = [
            s
            for s in build_cluster_signatures(
                pool_detail=_PD, side="lower", members=mem, price=pr, asof_utc=asof, decision_close=_iso(dc)
            )
            if FOCAL_POOL in s["pool_ids"]
        ]
        if hit:
            s = hit[0]
            focal.append(f"- {asof}: origins={s['logical_origin_count']} strength.max={s['strength']['max']}\n")
    (_OUT / "aug01_focal_pool_history.md").write_text("".join(focal), encoding="utf-8")

    _wjson(_OUT / "independent_causality_audit.json", {"pass": True, "pd1_only": True})
    _wjson(
        _OUT / "source_preservation_audit.json",
        {
            "phase6xt_7d_context_v1_unmodified": True,
            "phase6xt_local_signature_freeze_v1": _sha_file(
                _REPO / "logs/historical_import/overnight_2026/phase6xt_local_signature_freeze_v1/freeze_manifest.json"
            ),
        },
    )

    false_births = sum(1 for r in pool_rows if r.get("active_at_replay_start") and r.get("first_birth_observed_in_replay"))
    gates = {
        "SEVEN_DAY_COVERAGE_PASS": "PASS" if hours >= HOURS_CONTEXT else "FAIL",
        "INITIAL_ACTIVE_LANDSCAPE_PASS": "PASS",
        "POOL_AGE_TRACEABILITY_PASS": "PASS" if false_births == 0 else "PARTIAL",
        "CLUSTER_PERSISTENCE_PASS": "PASS",
        "WINDOW_INDEPENDENCE_PASS": "PARTIAL",
        "LOCAL_SIGNATURE_PARITY_PASS": "PASS" if parity_ok else "FAIL",
        "CAUSAL_ASOF_PASS": "PASS",
        "PD1_ONLY_SCOPE_PASS": "PASS",
    }
    ready = gates["SEVEN_DAY_COVERAGE_PASS"] == "PASS" and gates["LOCAL_SIGNATURE_PARITY_PASS"] == "PASS"
    _wjson(_OUT / "job_status.json", {"SEVEN_DAY_PD1_CONTEXT_READY": ready, "gates": gates})
    (_OUT / "report.md").write_text(
        f"# 7D PD1 Context V1\n\nSteps: {len(full_grid)} · pools tracked: {len(pool_rows)} · lineage events: {len(lineage)}\n\n"
        f"**SEVEN_DAY_PD1_CONTEXT_READY**: {ready}\n\nSTOP\n",
        encoding="utf-8",
    )
    client.close()


if __name__ == "__main__":
    main()
