#!/usr/bin/env python3
"""Phase 6XT-PD1-UPWARD-STAIRCASE-FORMATION V1 — descriptive forensics (no signals)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

_DASH = Path(__file__).resolve().parents[4]
_REPO = _DASH.parents[2]
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_upward_staircase_formation_v1"
_CTX = Path(__file__).resolve().parent
_SIG = _DASH / "research_charts/live_micro_lld_v2_ch_test/historical_json_import/phase6xt_local_strength_signature"
_TRANSFER_C = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_transfer_c_v1"
_COMP = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_comparison_v1"
_TRANSFER_B = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_transfer_v1"
_A7 = _REPO / "logs/historical_import/overnight_2026/phase6xt_7d_pd1_context_v1"

WINDOW_START = datetime(2026, 8, 13, 0, 0, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 21, 23, 55, tzinfo=timezone.utc)
EVAL_START = datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)
_PD = 1
STEP_EPS = 0.0002
MIN_SUPPORT_STRENGTH = 2.0
PERSIST_CLOSES = 3

for _p in (_DASH, _REPO, _CTX, _SIG):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from local_strength_signature_core import build_cluster_signatures  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
_wjson = c7._wjson
load_members = c7.load_members


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


def _strength_max(sig: dict) -> float:
    s = sig.get("strength") or {}
    return float(s.get("max") or 0)


def _below_support_clusters(sigs: list[dict], price: float) -> list[dict]:
    out = []
    for s in sigs:
        top = float(s["bounds"]["top"])
        if top < price and _strength_max(s) >= MIN_SUPPORT_STRENGTH:
            out.append(s)
    return out


def _above_upper_clusters(sigs: list[dict], price: float) -> list[dict]:
    out = []
    for s in sigs:
        bot = float(s["bounds"]["bottom"])
        if bot > price and _strength_max(s) >= MIN_SUPPORT_STRENGTH:
            out.append(s)
    return out


def pick_primary_support(below: list[dict]) -> dict | None:
    if not below:
        return None
    return max(below, key=lambda s: float(s["bounds"]["top"]))


def pick_deep_anchor(below: list[dict]) -> dict | None:
    if not below:
        return None
    return min(below, key=lambda s: float(s["bounds"]["bottom"]))


def pick_nearest_upper_above(above: list[dict]) -> dict | None:
    if not above:
        return None
    return min(above, key=lambda s: float(s["bounds"]["bottom"]))


def load_landscape(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def build_tracking_and_events(
    landscape: list[dict], client, db: str
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    mem_cache: dict = {}
    support_rows: list[dict] = []
    upper_rows: list[dict] = []
    track_rows: list[dict] = []
    events: list[dict] = []

    prev_support_top: float | None = None
    prev_support_id: str | None = None
    prev_upper_bot: float | None = None
    prev_upper_id: str | None = None
    last_dc: str | None = None
    persist_count = 0
    deep_id_at_first: str | None = None
    deep_left_fired: set[str] = set()
    persist_announced: set[str] = set()

    prices_by_asof = {r["asof_utc"]: float(r["price"]) for r in landscape}

    for rec in landscape:
        asof = rec["asof_utc"]
        dc = rec["decision_close"]
        price = float(rec["price"])
        members = load_members(mem_cache, parse_decision_close(dc), _PD, client, db)
        lowers = build_cluster_signatures(
            pool_detail=_PD, side="lower", members=members, price=price, asof_utc=asof, decision_close=dc
        )
        uppers = build_cluster_signatures(
            pool_detail=_PD, side="upper", members=members, price=price, asof_utc=asof, decision_close=dc
        )
        below = _below_support_clusters(lowers, price)
        above = _above_upper_clusters(uppers, price)
        prim = pick_primary_support(below)
        deep = pick_deep_anchor(below)
        n_up = pick_nearest_upper_above(above)

        if deep and deep_id_at_first is None:
            deep_id_at_first = deep["cluster_id"]
        lo_side = rec["sides"]["lower"]
        support_row = {
            "asof_utc": asof,
            "decision_close": dc,
            "price": price,
            "support_cluster_id": prim["cluster_id"] if prim else None,
            "support_bottom": prim["bounds"]["bottom"] if prim else None,
            "support_top": prim["bounds"]["top"] if prim else None,
            "support_strength_max": _strength_max(prim) if prim else None,
            "support_origin_overlap_max": (prim.get("geometry") or {}).get("max_origin_overlap_depth") if prim else None,
            "deep_cluster_id": deep["cluster_id"] if deep else None,
            "deep_bottom": deep["bounds"]["bottom"] if deep else None,
            "deep_top": deep["bounds"]["top"] if deep else None,
            "deep_strength_max": _strength_max(deep) if deep else None,
            "price_above_deep_top": (price > float(deep["bounds"]["top"])) if deep else None,
            "lower_origins": lo_side.get("logical_origins"),
            "exact_origin_overlap_max": lo_side.get("exact_origin_overlap_max"),
            "pool_overlap_max": lo_side.get("pool_overlap_max"),
        }
        support_rows.append(support_row)

        up_side = rec["sides"]["upper"]
        upper_row = {
            "asof_utc": asof,
            "decision_close": dc,
            "price": price,
            "nearest_upper_bridge_id": n_up["cluster_id"] if n_up else None,
            "bridge_bottom": n_up["bounds"]["bottom"] if n_up else None,
            "bridge_top": n_up["bounds"]["top"] if n_up else None,
            "bridge_strength_max": _strength_max(n_up) if n_up else None,
            "upper_origins": up_side.get("logical_origins"),
            "exact_origin_overlap_max": up_side.get("exact_origin_overlap_max"),
            "new_upper_births_step": len(rec.get("birth_pool_ids") or []),
        }
        upper_rows.append(upper_row)

        track_rows.append(
            {
                "asof_utc": asof,
                "decision_close": dc,
                "price": price,
                "primary_support": support_row,
                "deep_anchor": {
                    "cluster_id": support_row["deep_cluster_id"],
                    "bottom": support_row["deep_bottom"],
                    "top": support_row["deep_top"],
                },
                "nearest_upper_bridge": {
                    "cluster_id": upper_row["nearest_upper_bridge_id"],
                    "bottom": upper_row["bridge_bottom"],
                    "top": upper_row["bridge_top"],
                },
                "below_cluster_count": len(below),
                "above_upper_count": len(above),
            }
        )

        if dc != last_dc:
            last_dc = dc
            if prim:
                st = float(prim["bounds"]["top"])
                sid = prim["cluster_id"]
                if prev_support_top is not None and st > prev_support_top + STEP_EPS and sid != prev_support_id:
                    events.append(
                        {
                            "event": "NEW_HIGHER_LOWER_STEP",
                            "asof_utc": asof,
                            "decision_close": dc,
                            "price": price,
                            "prev_support_top": prev_support_top,
                            "new_support_top": st,
                            "cluster_id": sid,
                            "deep_cluster_id": deep["cluster_id"] if deep else None,
                            "phase": "A",
                        }
                    )
                    persist_count = 0
                prev_support_top = st
                prev_support_id = sid

            if n_up:
                ub = float(n_up["bounds"]["bottom"])
                uid = n_up["cluster_id"]
                if prev_upper_bot is not None and ub > prev_upper_bot + STEP_EPS:
                    events.append(
                        {
                            "event": "NEW_HIGHER_UPPER_BRIDGE",
                            "asof_utc": asof,
                            "decision_close": dc,
                            "price": price,
                            "prev_bridge_bottom": prev_upper_bot,
                            "new_bridge_bottom": ub,
                            "cluster_id": uid,
                            "phase": "A",
                        }
                    )
                prev_upper_bot = ub
                prev_upper_id = uid

            if prim and prev_support_id == prim["cluster_id"]:
                persist_count += 1
                if persist_count >= PERSIST_CLOSES and prim["cluster_id"] not in persist_announced:
                    persist_announced.add(prim["cluster_id"])
                    events.append(
                        {
                            "event": "LOWER_STEP_PERSISTS",
                            "asof_utc": asof,
                            "decision_close": dc,
                            "price": price,
                            "cluster_id": prim["cluster_id"],
                            "support_top": float(prim["bounds"]["top"]),
                            "persist_closes": persist_count,
                            "phase": "A",
                        }
                    )
                    persist_count = 0

            if deep and deep_id_at_first and deep["cluster_id"] == deep_id_at_first:
                dtop = float(deep["bounds"]["top"])
                did = deep["cluster_id"]
                if price > dtop + 0.001 and did not in deep_left_fired:
                    deep_left_fired.add(did)
                    events.append(
                        {
                            "event": "DEEP_LOWER_LEFT_BEHIND",
                            "asof_utc": asof,
                            "decision_close": dc,
                            "price": price,
                            "deep_bottom": float(deep["bounds"]["bottom"]),
                            "deep_top": dtop,
                            "cluster_id": did,
                            "phase": "A",
                        }
                    )

    # Phase B: staircase advance / failed (descriptive, post-hoc on sealed track)
    step_events = [e for e in events if e["event"] == "NEW_HIGHER_LOWER_STEP"]
    for e in step_events:
        t0 = parse_decision_close(e["asof_utc"])
        future = [r for r in support_rows if parse_decision_close(r["asof_utc"]) > t0]
        if not future:
            continue
        p0 = e["price"]
        pmax = max(float(r["price"]) for r in future[:288])
        pmin = min(float(r["price"]) for r in future[:288])
        new_top = float(e["new_support_top"])
        adv = pmax > p0 + 0.001 and pmin >= new_top - 0.0005
        fail = pmin < new_top - 0.0015
        if adv:
            events.append(
                {
                    "event": "STAIRCASE_ADVANCE",
                    "ref_asof": e["asof_utc"],
                    "horizon_5m_bars": min(288, len(future)),
                    "price_at_event": p0,
                    "price_max_after": pmax,
                    "price_min_after": pmin,
                    "phase": "B",
                }
            )
        elif fail:
            events.append(
                {
                    "event": "FAILED_STAIRCASE",
                    "ref_asof": e["asof_utc"],
                    "horizon_5m_bars": min(288, len(future)),
                    "price_at_event": p0,
                    "price_min_after": pmin,
                    "support_top_at_event": new_top,
                    "phase": "B",
                }
            )

    return support_rows, upper_rows, track_rows, events, list(prices_by_asof.items())


def counterexamples_md(step_events: list[dict], events: list[dict]) -> str:
    failed = [e for e in events if e["event"] == "FAILED_STAIRCASE"]
    adv = [e for e in events if e["event"] == "STAIRCASE_ADVANCE"]
    lines = [
        "# Counterexamples\n\n",
        "## Within window 2026-08-13 — 2026-08-21\n\n",
        f"- NEW_HIGHER_LOWER_STEP count: **{len(step_events)}**\n",
        f"- STAIRCASE_ADVANCE (24h descriptive): **{len(adv)}**\n",
        f"- FAILED_STAIRCASE (24h descriptive): **{len(failed)}**\n\n",
        "## Prior research windows (qualitative)\n\n",
        "- **Period A (2026-08-01):** mass lower ACTIVE loss — opposite of staircase build (`phase6xt_pd1_context_comparison_v1`).\n",
        "- **Period B (2026-08-10–11):** lower origins rose during rally — partial structural similarity without bridge ladder claim.\n",
        "- **Period C eval:** upper origin count **fell** while price rallied — contradicts naive “more upper bridges ⇒ count up”.\n\n",
        "## Pattern failures to watch\n\n",
        "- Higher lower steps with **FAILED_STAIRCASE** entries in `pd1_staircase_events.jsonl`.\n",
        "- Deep lowers persist without rally (sideways Aug 13–16 context week).\n",
    ]
    if failed[:5]:
        lines.append("\n### Sample FAILED_STAIRCASE (phase B)\n\n")
        for e in failed[:5]:
            lines.append(f"- ref {e['ref_asof']} min_after={e['price_min_after']:.6f}\n")
    return "".join(lines)


def assessment_md(
    support_rows: list[dict], events: list[dict], eval_rows: list[dict]
) -> str:
    steps = [e for e in events if e["event"] == "NEW_HIGHER_LOWER_STEP"]
    bridges = [e for e in events if e["event"] == "NEW_HIGHER_UPPER_BRIDGE"]
    deep_left = [e for e in events if e["event"] == "DEEP_LOWER_LEFT_BEHIND"]
    adv = [e for e in events if e["event"] == "STAIRCASE_ADVANCE"]
    if eval_rows:
        p0 = float(eval_rows[0]["price"])
        p1 = float(eval_rows[-1]["price"])
        s0 = eval_rows[0].get("support_top")
        s1 = eval_rows[-1].get("support_top")
    else:
        p0 = p1 = s0 = s1 = None
    return f"""# Staircase hypothesis assessment (descriptive only)

**Hypothesis not confirmed** — forensic metrics only.

## Closing questions

1. **Higher lower support steps in 13.–21.08.?**
   **{len(steps)}** `NEW_HIGHER_LOWER_STEP` events at 15m `decision_close` granularity (support_top rise > {STEP_EPS}).

2. **Deep lowers left behind?**
   **{len(deep_left)}** `DEEP_LOWER_LEFT_BEHIND` markers when price traded >1m above deep top while deep anchor unchanged.

3. **Higher upper bridges?**
   **{len(bridges)}** `NEW_HIGHER_UPPER_BRIDGE` events (nearest-above cluster bottom stepped up).

4. **Price moved with structure?**
   Eval 48h price **{p0} → {p1}**; primary support_top **{s0} → {s1}**. **{len(adv)}** `STAIRCASE_ADVANCE` vs failed cases in events file — **correlation only**.

5. **Visual vs metric?**
   Metric: cluster bounds, strength ≥{MIN_SUPPORT_STRENGTH}, exact origin overlap in timelines. Visual: “ladder” metaphor — use `pd1_relevant_cluster_tracking.jsonl`.

6. **Counterexamples?**
   See `counterexamples.md` (Aug 1 collapse, upper-count drop in C, FAILED_STAIRCASE rows).

7. **Clearer in PD1 alone?**
   **Yes** vs multi-PD — single ACTIVE SSOT + cluster signatures; still no automatic trade rule.

8. **Open points?**
   Choice of “primary support” heuristic; 5m vs 15m publication; catalog break_at vs price revisit; micro/normal dedupe in clusters.

## Phase A / B

- **Phase A:** events with `"phase": "A"` in `pd1_staircase_events.jsonl` + sealed hashes.
- **Phase B:** `STAIRCASE_ADVANCE` / `FAILED_STAIRCASE` use only prices **after** reference step (post-seal descriptive).
"""


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    landscape_path = _TRANSFER_C / "pd1_asof_landscape.jsonl"
    cov_src = _TRANSFER_C / "coverage_audit.json"
    if not landscape_path.is_file() or not cov_src.is_file():
        _wjson(_OUT / "job_status.json", {"verdict": "PD1_UPWARD_STAIRCASE_NOT_READY", "error": "missing transfer_c landscape"})
        sys.exit(2)

    cov = json.loads(cov_src.read_text(encoding="utf-8"))
    _wjson(_OUT / "coverage_audit.json", {**cov, "staircase_window": {"start": _iso(WINDOW_START), "end": _iso(WINDOW_END)}})
    if not cov.get("pass"):
        _wjson(_OUT / "job_status.json", {"verdict": "PD1_UPWARD_STAIRCASE_NOT_READY", "coverage": cov})
        sys.exit(2)

    landscape = [r for r in load_landscape(landscape_path) if WINDOW_START <= parse_decision_close(r["asof_utc"]) <= WINDOW_END]
    if len(landscape) < 2000:
        _wjson(_OUT / "job_status.json", {"verdict": "PD1_UPWARD_STAIRCASE_NOT_READY", "landscape_rows": len(landscape)})
        sys.exit(2)

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database

    print("Building support/bridge tracking...", flush=True)
    support_rows, upper_rows, track_rows, events, _ = build_tracking_and_events(landscape, client, db)
    client.close()

    _wcsv(_OUT / "pd1_support_step_timeline.csv", support_rows)
    _wcsv(_OUT / "pd1_upper_bridge_timeline.csv", upper_rows)
    with (_OUT / "pd1_relevant_cluster_tracking.jsonl").open("w", encoding="utf-8") as f:
        for row in track_rows:
            f.write(json.dumps(row, default=str) + "\n")
    with (_OUT / "pd1_staircase_events.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, default=str) + "\n")

    phase_a_files = [
        "coverage_audit.json",
        "source_contract.json",
        "pd1_support_step_timeline.csv",
        "pd1_upper_bridge_timeline.csv",
        "pd1_staircase_events.jsonl",
        "pd1_relevant_cluster_tracking.jsonl",
    ]
    _wjson(
        _OUT / "source_contract.json",
        {
            "symbol": "DOGEUSDT",
            "pd": "15m Original LMV2 PD1",
            "window": [_iso(WINDOW_START), _iso(WINDOW_END)],
            "eval_start": _iso(EVAL_START),
            "inputs": {"landscape": str(landscape_path), "transfer_c": str(_TRANSFER_C)},
            "heuristic": {
                "primary_support": "max top among below-price lowers strength>=" + str(MIN_SUPPORT_STRENGTH),
                "step_eps": STEP_EPS,
            },
            "events_descriptive": [
                "NEW_HIGHER_LOWER_STEP",
                "LOWER_STEP_PERSISTS",
                "DEEP_LOWER_LEFT_BEHIND",
                "NEW_HIGHER_UPPER_BRIDGE",
                "STAIRCASE_ADVANCE",
                "FAILED_STAIRCASE",
            ],
        },
    )
    hashes = {fn: _sha_file(_OUT / fn) for fn in phase_a_files if (_OUT / fn).is_file()}
    _wjson(_OUT / "phase_a_manifest.json", {"sealed_at_utc": _iso(datetime.now(timezone.utc)), "files": list(hashes.keys()), "event_count": len(events)})
    _wjson(_OUT / "phase_a_hashes.json", hashes)

    eval_support = [r for r in support_rows if parse_decision_close(r["asof_utc"]) >= EVAL_START]
    step_e = [e for e in events if e["event"] == "NEW_HIGHER_LOWER_STEP"]
    (_OUT / "staircase_hypothesis_assessment.md").write_text(assessment_md(support_rows, events, eval_support), encoding="utf-8")
    (_OUT / "counterexamples.md").write_text(counterexamples_md(step_e, events), encoding="utf-8")

    partial = len(landscape) < 2590 or len(step_e) == 0
    verdict = (
        "PD1_UPWARD_STAIRCASE_PARTIAL"
        if partial and len(step_e) > 0
        else ("PD1_UPWARD_STAIRCASE_NOT_READY" if len(step_e) == 0 else "PD1_UPWARD_STAIRCASE_COMPLETE")
    )
    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "new_higher_lower_steps": len(step_e),
            "new_higher_upper_bridges": sum(1 for e in events if e["event"] == "NEW_HIGHER_UPPER_BRIDGE"),
            "phase_a_sealed": bool(all(hashes.values())),
        },
    )
    (_OUT / "report.md").write_text(
        f"# PD1 Upward Staircase Formation V1\n\n"
        f"Window **{_iso(WINDOW_START)}** – **{_iso(WINDOW_END)}** · landscape rows **{len(landscape)}**.\n\n"
        f"Lower steps: **{len(step_e)}** · events total: **{len(events)}**.\n\n"
        f"**Verdict:** `{verdict}`\n\n"
        "Hypothesis **not** promoted to signal logic. See `staircase_hypothesis_assessment.md`.\n\n**STOP**\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
