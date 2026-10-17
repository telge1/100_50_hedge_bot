#!/usr/bin/env python3
"""Phase 6XT-INDEPENDENT-BOTTOM-PATTERN-PILOT V1 — July 2026 independent window."""

from __future__ import annotations

import csv
import importlib.util
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_independent_bottom_pattern_pilot_v1"
_CTX = Path(__file__).resolve().parent
_FROZEN_PATH = (
    _REPO
    / "logs/historical_import/overnight_2026/phase6xt_automated_bottom_formation_discovery_v1/frozen_detection_contract.json"
)
_BFD_SCRIPT = _CTX / "phase6xt_automated_bottom_formation_discovery_v1.py"

HIST_START = datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc)
PILOT_MAIN = datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc)
PHASE_A_END = datetime(2026, 7, 12, 23, 55, tzinfo=timezone.utc)
POST_END = datetime(2026, 7, 14, 23, 55, tzinfo=timezone.utc)
_CONTEXT_H = 168
_PDS = (1, 10)
_EPS = 0.00012

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


def load_frozen_contract() -> dict[str, Any]:
    return json.loads(_FROZEN_PATH.read_text(encoding="utf-8"))


def load_bfd_module(frozen: dict[str, Any]):
    spec = importlib.util.spec_from_file_location("bfd", _BFD_SCRIPT)
    bfd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bfd)
    bfd.PILOT_START = HIST_START
    bfd.PILOT_END = PHASE_A_END
    bfd.MAIN_START = PILOT_MAIN
    bfd._CONTEXT_H = _CONTEXT_H
    bfd._EPS = _EPS
    bfd._DEEPER_LOOKBACK_H = frozen["deeper_lookback_hours"]
    bfd._HIGHER_LOOKBACK_H = frozen["higher_lookback_hours"]
    bfd._UPPER_BIRTH_LOOKBACK_H = frozen["upper_birth_lookback_hours"]
    bfd._EPISODE_GAP_H = 12
    bfd.REFERENCE_WINDOW = (
        datetime(2099, 1, 1, tzinfo=timezone.utc),
        datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    bfd.REFERENCE_LABEL = "NONE_JULY_PILOT"
    return bfd


def coverage_audit(client, db: str, bars: dict) -> dict[str, Any]:
    missing = []
    t = HIST_START
    while t <= POST_END:
        if t not in bars:
            missing.append(_iso(t))
        t += timedelta(minutes=5)
    hours = (PILOT_MAIN - HIST_START).total_seconds() / 3600
    pd_info = {}
    for pd in _PDS:
        hcfg = resolve_historical_pd_config(pd)
        pub = get_published_decision_closes(
            PublishedActiveReader(), hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id
        )
        pub_n = len([x for x in pub if HIST_START <= parse_decision_close(x) <= POST_END])
        try:
            load_members({}, causal_15m_decision_close(HIST_START), pd, client, db)
            load_members({}, causal_15m_decision_close(POST_END), pd, client, db)
            ok = True
            err = None
        except Exception as e:
            ok = False
            err = str(e)
        pd_info[f"PD{pd}"] = {"published_closes": pub_n, "pool_load_ok": ok, "error": err}
    return {
        "hist_start": _iso(HIST_START),
        "pilot_main": _iso(PILOT_MAIN),
        "phase_a_end": _iso(PHASE_A_END),
        "post_end": _iso(POST_END),
        "context_hours": hours,
        "5m_expected": len(iter_5m(HIST_START, POST_END)),
        "5m_present": len(bars),
        "5m_missing": len(missing),
        "missing_sample": missing[:5],
        "pd": pd_info,
        "pass": hours >= _CONTEXT_H and not missing and all(pd_info[f"PD{p}"]["pool_load_ok"] for p in _PDS),
    }


def build_landscapes(contexts: list[dict], client, db: str) -> tuple[list[dict], list[dict]]:
    cache: dict = {}
    pd1, pd10 = [], []
    for c in contexts:
        pd = c["pool_detail"]
        dc = parse_decision_close(c["decision_close"])
        mem = load_members(cache, dc, pd, client, db)
        enc = enrich_context(c, mem)
        (pd1 if pd == 1 else pd10).append(enc)
    return pd1, pd10


def extract_births(rows: list[dict], client, db: str, side: str) -> list[dict]:
    cache: dict = {}
    seen: set[tuple[int, str]] = set()
    running_deepest = {pd: float("inf") for pd in _PDS}
    running_support_top = {pd: 0.0 for pd in _PDS}
    out = []
    for rec in sorted(rows, key=lambda x: x["asof_utc"]):
        pd = rec["pool_detail"]
        asof = rec["asof_utc"]
        dc = parse_decision_close(rec["decision_close"])
        price = float(rec["price"])
        mem = load_members(cache, dc, pd, client, db)
        by_id = {m["pool_id"]: m for m in mem}
        for pid in rec.get("birth_pool_ids") or []:
            m = by_id.get(pid)
            if not m or m.get("side") != side:
                continue
            bot = float(m["lower_bound"])
            top = float(m["upper_bound"])
            oid = origin_logical_v2_key(pid)
            okey = (pd, oid)
            is_new_origin = okey not in seen
            if is_new_origin:
                seen.add(okey)
            deeper = is_new_origin and bot < running_deepest[pd] - _EPS
            higher_step = is_new_origin and bot > running_support_top[pd] + _EPS
            within = is_new_origin and not deeper and not higher_step
            if is_new_origin and bot < running_deepest[pd]:
                running_deepest[pd] = bot
            if top > running_support_top[pd]:
                running_support_top[pd] = top
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
                    "phase_a_eligible": parse_decision_close(asof) <= PHASE_A_END,
                }
            )
    return out


def deep_retention(pd1: list[dict], pd10: list[dict], client, db: str) -> list[dict]:
    cache: dict = {}
    rows = []
    for pd, data in ((1, pd1), (10, pd10)):
        rec = next((r for r in data if r["asof_utc"] == _iso(PILOT_MAIN)), None)
        if not rec:
            continue
        dc = parse_decision_close(rec["decision_close"])
        price = float(rec["price"])
        for m in load_members(cache, dc, pd, client, db):
            if m.get("side") != "lower":
                continue
            bot = float(m["lower_bound"])
            if bot < price - 0.0015:
                rows.append(
                    {
                        "pool_detail": pd,
                        "pool_id": m["pool_id"],
                        "origin_id": origin_logical_v2_key(m["pool_id"]),
                        "bottom": bot,
                        "top": float(m["upper_bound"]),
                        "strength": m.get("strength"),
                        "observed_at": rec["asof_utc"],
                    }
                )
    end_rec = max((r for r in pd1 if parse_decision_close(r["asof_utc"]) <= PHASE_A_END), key=lambda x: x["asof_utc"])
    dc_end = parse_decision_close(end_rec["decision_close"])
    active_end = {m["pool_id"] for m in load_members(cache, dc_end, 1, client, db) if m.get("side") == "lower"}
    for r in rows:
        if r["pool_detail"] == 1:
            r["active_at_phase_a_end_pd1"] = r["pool_id"] in active_end
    return rows


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    if not _FROZEN_PATH.is_file():
        _wjson(_OUT / "job_status.json", {"verdict": "INDEPENDENT_BOTTOM_PATTERN_PILOT_NOT_READY"})
        sys.exit(2)

    frozen = load_frozen_contract()
    frozen_hash = _sha_file(_FROZEN_PATH)
    _wjson(
        _OUT / "frozen_contract_verification.json",
        {
            "path": str(_FROZEN_PATH),
            "sha256": frozen_hash,
            "version": frozen.get("version"),
            "parameters_match_code": {
                "deeper_lookback_hours": frozen["deeper_lookback_hours"],
                "higher_lookback_hours": frozen["higher_lookback_hours"],
                "upper_birth_lookback_hours": frozen["upper_birth_lookback_hours"],
                "eps": _EPS,
            },
            "modified": False,
        },
    )

    bfd = load_bfd_module(frozen)
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
        _wjson(_OUT / "job_status.json", {"verdict": "INDEPENDENT_BOTTOM_PATTERN_PILOT_NOT_READY", "coverage": cov})
        (_OUT / "report.md").write_text("# STOP — coverage\n", encoding="utf-8")
        client.close()
        sys.exit(2)

    grid = iter_5m(HIST_START, POST_END)
    print(f"July pilot replay {len(grid)} steps...", flush=True)
    t_replay = time.time()
    full = run_replay(grid, client, db, bars, "july_independent_pilot", pool_details=_PDS)
    replay_sec = time.time() - t_replay

    pd1_all, pd10_all = build_landscapes(full["contexts"], client, db)
    lower1 = extract_births(pd1_all, client, db, "lower")
    lower10 = extract_births(pd10_all, client, db, "lower")
    upper1 = extract_births(pd1_all, client, db, "upper")
    upper10 = extract_births(pd10_all, client, db, "upper")

    l1_dc = {}
    for r in pd1_all:
        dc = r["decision_close"]
        if dc not in l1_dc or r["asof_utc"] > l1_dc[dc]["asof_utc"]:
            l1_dc[dc] = r
    l10_dc = {}
    for r in pd10_all:
        dc = r["decision_close"]
        if dc not in l10_dc or r["asof_utc"] > l10_dc[dc]["asof_utc"]:
            l10_dc[dc] = r

    events, _ = bfd.scan_pilot(lower1, lower10, upper1, upper10, l1_dc)
    # restrict phase-A candidate events to pilot main window
    events_pa = [
        e
        for e in events
        if PILOT_MAIN <= parse_decision_close(e["decision_close"]) <= PHASE_A_END
    ]
    with (_OUT / "candidate_events.jsonl").open("w", encoding="utf-8") as f:
        for e in events_pa:
            f.write(json.dumps(e, default=str) + "\n")

    episodes = bfd.merge_episodes(events_pa)
    for ep in episodes:
        ep["independent_candidate"] = True
        ep.pop("episode_label", None)
    _wcsv(_OUT / "candidate_episodes.csv", episodes)

    comp = []
    for ep in episodes:
        dc = ep["episode_start"]
        r1, r10 = l1_dc.get(dc), l10_dc.get(dc)
        if not r1 or not r10:
            continue
        t = parse_decision_close(dc)
        comp.append(
            {
                "episode_id": ep["episode_id"],
                "category": ep["category"],
                "decision_close": dc,
                "pd1_category": bfd.classify_at(t, lower1, upper1, r1),
                "pd10_category": bfd.classify_at(t, lower10, upper10, r10),
                "pd1_lower_origins": r1["sides"]["lower"]["logical_origins"],
                "pd10_lower_origins": r10["sides"]["lower"]["logical_origins"],
                "pd1_upper_origins": r1["sides"]["upper"]["logical_origins"],
                "pd10_upper_origins": r10["sides"]["upper"]["logical_origins"],
                "pd1_lower_exact": r1["sides"]["lower"].get("exact_origin_overlap_max"),
                "pd10_lower_exact": r10["sides"]["lower"].get("exact_origin_overlap_max"),
            }
        )
    _wcsv(_OUT / "pd1_pd10_comparison.csv", comp)

    _wcsv(_OUT / "deep_lower_retention.csv", deep_retention(pd1_all, pd10_all, client, db))

    ul_rows = []
    for rec in pd1_all:
        t = parse_decision_close(rec["asof_utc"])
        if not (PILOT_MAIN <= t <= PHASE_A_END):
            continue
        if rec["asof_utc"] != l1_dc[rec["decision_close"]]["asof_utc"]:
            continue
        up = rec["sides"]["upper"]
        ul_rows.append(
            {
                "decision_close": rec["decision_close"],
                "price": rec["price"],
                "upper_origins": up["logical_origins"],
                "upper_exact_max": up.get("exact_origin_overlap_max"),
                "upper_gap": up["largest_gap_width"],
                "upper_births_24h": bfd.upper_births_count(upper1, t),
            }
        )
    _wcsv(_OUT / "upper_ladder_observations.csv", ul_rows)

    phase_a_files = [
        "coverage_audit.json",
        "frozen_contract_verification.json",
        "candidate_events.jsonl",
        "candidate_episodes.csv",
        "pd1_pd10_comparison.csv",
        "deep_lower_retention.csv",
        "upper_ladder_observations.csv",
    ]
    hashes = {fn: _sha_file(_OUT / fn) for fn in phase_a_files}
    _wjson(
        _OUT / "phase_a_manifest.json",
        {
            "sealed_at_utc": _iso(datetime.now(timezone.utc)),
            "frozen_contract_sha256": frozen_hash,
            "pilot_window": [_iso(PILOT_MAIN), _iso(PHASE_A_END)],
        },
    )
    _wjson(_OUT / "phase_a_hashes.json", hashes)

    prices = {parse_decision_close(r["asof_utc"]): float(r["price"]) for r in pd1_all}
    post_rows = []
    horizons = [1, 4, 12, 24, 48]
    for ep in episodes:
        t_start = parse_decision_close(ep["episode_start"])
        obs = bfd.price_path(prices, t_start, horizons)
        post_rows.append({"episode_id": ep["episode_id"], "category": ep["category"], "kind": "candidate", **obs})
    controls = [e for e in events_pa if e["category"] in ("A", "E")][:3]
    for i, e in enumerate(controls):
        t_start = parse_decision_close(e["decision_close"])
        obs = bfd.price_path(prices, t_start, horizons)
        post_rows.append(
            {"episode_id": f"CTRL{i+1}", "category": e["category"], "kind": "control", **obs}
        )
    _wcsv(_OUT / "post_candidate_price_observations.csv", post_rows)

    cat_counts = {}
    for e in events_pa:
        cat_counts[e["category"]] = cat_counts.get(e["category"], 0) + 1

    _wjson(
        _OUT / "source_manifest.json",
        {
            "frozen_contract": str(_FROZEN_PATH),
            "replay_label": "july_independent_pilot",
            "grid_steps": len(grid),
            "pd1_context_rows": len(pd1_all),
        },
    )
    _wjson(
        _OUT / "performance_report.json",
        {
            "grid_steps": len(grid),
            "replay_seconds": round(replay_sec, 1),
            "total_seconds": round(time.time() - t0, 1),
            "lower_birth_rows_pd1": len(lower1),
        },
    )

    cd_episodes = [e for e in episodes if e["category"] in ("C", "D")]
    (_OUT / "counterexamples.md").write_text(
        "# Counterexamples (July pilot)\n\n"
        f"- Category counts in pilot window: {cat_counts}\n"
        f"- C/D episodes: **{len(cd_episodes)}**\n"
        "- Sideways Jul 10–12 with mixed A/E if no full D.\n"
        "- Aug reference **excluded** from this run.\n",
        encoding="utf-8",
    )

    partial = len(cd_episodes) == 0
    verdict = (
        "INDEPENDENT_BOTTOM_PATTERN_PILOT_COMPLETE"
        if not partial
        else "INDEPENDENT_BOTTOM_PATTERN_PILOT_PARTIAL"
    )
    if not partial and len(cd_episodes) < 1:
        verdict = "INDEPENDENT_BOTTOM_PATTERN_PILOT_PARTIAL"

    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "c_d_episodes": len(cd_episodes),
            "category_counts": cat_counts,
            "independent": True,
        },
    )
    (_OUT / "report.md").write_text(
        f"# Independent bottom pattern pilot (July 2026)\n\n"
        f"Window **{_iso(PILOT_MAIN)}** – **{_iso(PHASE_A_END)}** · post to **{_iso(POST_END)}**.\n\n"
        f"Frozen contract verified: `{frozen_hash[:16]}...`\n\n"
        f"**C/D episodes:** {len(cd_episodes)} · categories: {cat_counts}\n\n"
        f"**Verdict:** `{verdict}`\n\n"
        "## Closing\n\n"
        "1. Unchanged search outside Aug: **executed** on July pilot.\n"
        f"2. Independent episodes: **{len(cd_episodes)}** C/D.\n"
        "3–4. See `candidate_episodes.csv`.\n"
        "9. Full months: replay ~17min/12d dual-PD — batch feasible with approval.\n\n"
        "**STOP**\n",
        encoding="utf-8",
    )
    client.close()


if __name__ == "__main__":
    main()
