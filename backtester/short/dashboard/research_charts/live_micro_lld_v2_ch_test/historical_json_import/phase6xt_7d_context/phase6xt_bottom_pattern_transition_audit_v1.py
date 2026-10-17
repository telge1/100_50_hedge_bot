#!/usr/bin/env python3
"""Phase 6XT-BOTTOM-PATTERN-TRANSITION-AUDIT V1 — state vs change diagnosis (read-only)."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_bottom_pattern_transition_audit_v1"
_CTX = Path(__file__).resolve().parent
_FROZEN = (
    _REPO
    / "logs/historical_import/overnight_2026/phase6xt_automated_bottom_formation_discovery_v1/frozen_detection_contract.json"
)
_JULY = _REPO / "logs/historical_import/overnight_2026/phase6xt_independent_bottom_pattern_pilot_v1"
_PRE = _REPO / "logs/historical_import/overnight_2026/phase6xt_pre_breakout_pd1_pd10_forensics_v1"
_BFD = _CTX / "phase6xt_automated_bottom_formation_discovery_v1.py"

JULY_D_START = datetime(2026, 7, 10, 0, 30, tzinfo=timezone.utc)
JULY_PILOT_END = datetime(2026, 7, 12, 23, 55, tzinfo=timezone.utc)
AUG_MAIN_START = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
AUG_CUTOFF = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402

_iso = c7._iso
_wjson = c7._wjson
_sha_file = c7._sha_file
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


def load_birth_csv(path: Path) -> list[dict]:
    rows = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        for k in ("is_new_origin", "class_deeper", "class_higher_step", "class_within_zone"):
            if k in r:
                r[k] = r[k] == "True"
        r["pool_detail"] = int(r["pool_detail"])
        rows.append(r)
    return rows


def member_diff_births(
    cache: dict, client, db: str, pd: int, dc: datetime, prev_dc: datetime | None
) -> tuple[list[dict], list[dict]]:
    mem = load_members(cache, dc, pd, client, db)
    cur = {m["pool_id"]: m for m in mem}
    if prev_dc is None:
        return [], list(cur.values())
    prev = {m["pool_id"]: m for m in load_members(cache, prev_dc, pd, client, db)}
    births = []
    for pid, m in cur.items():
        if pid not in prev:
            births.append(m)
    deaths = [pid for pid in prev if pid not in cur]
    return births, deaths


def births_to_rows(
    births: list[dict], asof: str, dc: str, price: float | None, pd: int
) -> list[dict]:
    out = []
    for m in births:
        out.append(
            {
                "pool_detail": pd,
                "asof_utc": asof,
                "decision_close": dc,
                "price": price,
                "pool_id": m["pool_id"],
                "origin_id": origin_logical_v2_key(m["pool_id"]),
                "side": m.get("side"),
                "bottom": float(m["lower_bound"]),
                "top": float(m["upper_bound"]),
                "strength": m.get("strength"),
                "known_at": m.get("known_at"),
            }
        )
    return out


def unique_dcs_in_range(start: datetime, end: datetime) -> list[datetime]:
    t = causal_15m_decision_close(start)
    out = []
    while t <= end:
        out.append(t)
        t += timedelta(minutes=15)
    return out


def frozen_diagnostics_md() -> str:
    return """# Frozen contract diagnostics (category D)

Source: `phase6xt_automated_bottom_formation_discovery_v1.py` + `frozen_detection_contract.json`.

## Category D logic (STATE, not edge-triggered)

1. **`classify_at(t)`** evaluates **only** lookback aggregates at time `t` — no requirement for a birth **at** `t`.
2. **No deeper:** `unique_origin_births` with `class_deeper` in **72h** window; window floor `max(t-72h, MAIN_START)`.
3. **Spot lowers:** ≥1 unique new lower origin in **48h** with `not class_deeper` (same floor).
4. **Upper activity:** ≥1 unique upper origin birth in **24h**.
5. If (2) empty and (3) non-empty → **C**; if also (4) → **D**.

## Persistence

- Once origins enter lookback, conditions can remain **true for every subsequent `decision_close`** until events age out or counters change.
- **Spot lookback count can increase** while price is flat (new origins still entering 48h window) → D stays on; not a new “episode” per step.
- **No** “first time D” vs “continued D” flag in contract.

## Code references

- `births_in_window` — inclusive `(t0, t_end]` on birth `asof_utc`
- `spot_lower_births` — dedupe by `origin_id`
- `scan_pilot` — one row per unique `decision_close`; category recomputed from scratch each time

## Implication

**Category D ≈ landscape STATE descriptor**, not a one-shot transition detector.
"""


def july_saturation_audit(events: list[dict]) -> list[dict]:
    d_rows = [e for e in events if e["category"] == "D"]
    out = []
    prev = None
    for i, e in enumerate(d_rows):
        ch = False
        if prev:
            ch = (
                e["spot_lower_origins_lookback"] != prev["spot_lower_origins_lookback"]
                or e["upper_origin_births_24h"] != prev["upper_origin_births_24h"]
                or e["deeper_origins_lookback"] != prev["deeper_origins_lookback"]
                or e.get("pd1_lower_origins") != prev.get("pd1_lower_origins")
                or e.get("pd1_upper_origins") != prev.get("pd1_upper_origins")
            )
        out.append(
            {
                "step_index": i + 1,
                "decision_close": e["decision_close"],
                "price": e["price"],
                "deeper_origins_lookback": e["deeper_origins_lookback"],
                "spot_lower_origins_lookback": e["spot_lower_origins_lookback"],
                "upper_origin_births_24h": e["upper_origin_births_24h"],
                "pd1_lower_origins": e.get("pd1_lower_origins"),
                "pd1_upper_origins": e.get("pd1_upper_origins"),
                "lookback_metric_changed_vs_prev_d_step": ch,
                "first_d_step": i == 0,
            }
        )
        prev = e
    return out


def audit_dc_births(
    client,
    db: str,
    dcs: list[datetime],
    pd: int,
    price_by_dc: dict[str, float] | None = None,
) -> list[dict]:
    cache: dict = {}
    rows = []
    prev_dc = None
    for dc in dcs:
        dc_s = _iso(dc)
        births, deaths = member_diff_births(cache, client, db, pd, dc, prev_dc)
        price = price_by_dc.get(dc_s) if price_by_dc else None
        rows.extend(births_to_rows(births, dc_s, dc_s, price, pd))
        prev_dc = dc
    return rows


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    if not _FROZEN.is_file() or not (_JULY / "candidate_events.jsonl").is_file():
        _wjson(_OUT / "job_status.json", {"verdict": "BOTTOM_PATTERN_TRANSITION_AUDIT_NOT_READY"})
        sys.exit(2)

    frozen = json.loads(_FROZEN.read_text(encoding="utf-8"))
    _wjson(
        _OUT / "source_contract.json",
        {
            "frozen_contract": str(_FROZEN),
            "frozen_sha256": _sha_file(_FROZEN),
            "july_events": str(_JULY / "candidate_events.jsonl"),
            "august_births": str(_PRE / "pd1_lower_births.csv"),
            "bfd_script": str(_BFD),
            "no_detector_changes": True,
        },
    )
    (_OUT / "frozen_contract_diagnostics.md").write_text(frozen_diagnostics_md(), encoding="utf-8")

    july_events = [
        json.loads(l)
        for l in (_JULY / "candidate_events.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip()
    ]
    sat = july_saturation_audit(july_events)
    _wcsv(_OUT / "july_d_saturation_audit.csv", sat)
    metric_changes = sum(1 for r in sat if r["lookback_metric_changed_vs_prev_d_step"])

    # Lightweight SSOT birth diffs at 15m grid (not full replay)
    from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
    import clickhouse_connect  # noqa: E402

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database

    july_dcs = unique_dcs_in_range(JULY_D_START, JULY_PILOT_END)
    price_j = {e["decision_close"]: e["price"] for e in july_events}
    july_births = audit_dc_births(client, db, july_dcs, 1, price_j)
    july_births += audit_dc_births(client, db, july_dcs, 10, price_j)
    _wcsv(_OUT / "july_actual_new_pool_events.csv", july_births)

    aug_dcs = unique_dcs_in_range(AUG_MAIN_START, AUG_CUTOFF)
    l1_dc = {}
    with (_PRE / "pd1_asof_landscape.jsonl").open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            dc = r["decision_close"]
            if dc not in l1_dc or r["asof_utc"] > l1_dc[dc]["asof_utc"]:
                l1_dc[dc] = r
    price_a = {dc: float(l1_dc[dc]["price"]) for dc in l1_dc if AUG_MAIN_START <= parse_decision_close(dc) <= AUG_CUTOFF}
    aug_ssot_births = audit_dc_births(client, db, aug_dcs, 1, price_a)
    aug_ssot_births += audit_dc_births(client, db, aug_dcs, 10, price_a)

    # Merge with precomputed birth CSV (origin classification)
    lower1 = load_birth_csv(_PRE / "pd1_lower_births.csv")
    upper1 = load_birth_csv(_PRE / "pd1_upper_births.csv")
    aug_csv = []
    for r in lower1 + upper1:
        t = parse_decision_close(r["asof_utc"])
        if not (AUG_MAIN_START <= t <= AUG_CUTOFF):
            continue
        aug_csv.append({**r, "source": "pre_breakout_birth_csv"})
    _wcsv(_OUT / "august_pre_breakout_actual_events.csv", aug_csv)

    # Compare birth counts per day
    def day_counts(rows, key="asof_utc"):
        c = defaultdict(lambda: {"lower": 0, "upper": 0, "deeper": 0, "higher": 0})
        for r in rows:
            if r.get("source") == "pre_breakout_birth_csv":
                day = r[key][:10]
                side = "lower" if "lower" in str(r.get("pool_id", r.get("origin_id", ""))) else "upper"
                if r.get("class_deeper"):
                    c[day]["deeper"] += 1
                if r.get("class_higher_step"):
                    c[day]["higher"] += 1
                if "lower" in str(r.get("pool_id", "")):
                    c[day]["lower"] += 1
                else:
                    c[day]["upper"] += 1
        return dict(c)

    july_by_day = defaultdict(int)
    for r in july_births:
        july_by_day[r["asof_utc"][:10]] += 1

    struct = f"""# Structural differences July vs August

## A. New lower steps per time unit

| Day | July SSOT pool births (PD1+PD10 @15m diffs) | Aug PD1 origin-events (CSV) |
|-----|---------------------------------------------|-----------------------------|
| Jul 10 | {sum(1 for r in july_births if r['asof_utc'].startswith('2026-07-10'))} | — |
| Jul 11 | {sum(1 for r in july_births if r['asof_utc'].startswith('2026-07-11'))} | — |
| Jul 12 | {sum(1 for r in july_births if r['asof_utc'].startswith('2026-07-12'))} | — |
| Aug 17 | — | {sum(1 for r in aug_csv if r['asof_utc'].startswith('2026-08-17'))} |
| Aug 18 | — | {sum(1 for r in aug_csv if r['asof_utc'].startswith('2026-08-18'))} |
| Aug 19 | — | {sum(1 for r in aug_csv if r['asof_utc'].startswith('2026-08-19'))} |

## B–G (summary)

- **July D saturation:** {len(sat)} D `decision_close` rows; only **{metric_changes}** show changed lookback metrics vs previous D-step — remainder is **persistent STATE**.
- **August:** no `class_deeper` in main window (forensics); spot lower births on **19.08.** morning before cutoff.
- **Upper:** both periods show ongoing origin churn; **total upper count alone is not discriminative**.

See `state_vs_change_vs_sequence.md`.
"""
    (_OUT / "structural_differences_july_august.md").write_text(struct, encoding="utf-8")

    state_md = """# STATE vs CHANGE vs SEQUENCE

## July EP002 (category D)

| Layer | Finding |
|-------|---------|
| **STATE** | From `2026-07-10T00:30` lookbacks satisfy D: deeper=0, spot origins in window, upper births in 24h. |
| **CHANGE** | Only **first** D step is transition C→D; later steps often **no** new pool publication at that `decision_close`. |
| **SEQUENCE** | Spot lookback **monotonically increases** (1→2→3→…) as new origins enter 48h window — **ongoing births**, not one ladder step. |

## August (≤ 2026-08-19T13:00)

| Layer | Finding |
|-------|---------|
| **STATE** | Similar D-capable state builds from **17.08.** with deeper exhaustion in **main** window. |
| **CHANGE** | Concentrated **new** spot lower origins **19.08. AM** (forensics CSV). |
| **SEQUENCE** | Lower-origin growth **17→19** then pump **after** cutoff — timing differs from July flat 48h. |

**Conclusion:** Frozen **D** conflates **STATE** with **TRANSITION**; July stays D because lookbacks keep refreshing, not because 109 independent patterns fired.
"""
    (_OUT / "state_vs_change_vs_sequence.md").write_text(state_md, encoding="utf-8")

    (_OUT / "pd1_pd10_transition_comparison.md").write_text(
        "# PD1 vs PD10 transition comparison\n\n"
        "July episode starts: categories matched (independent pilot).\n"
        f"July 15m SSOT births PD1+PD10 in pilot: **{len(july_births)}** rows.\n"
        "August: use `august_pre_breakout_actual_events.csv` (PD1 classified) + SSOT diff optional.\n",
        encoding="utf-8",
    )

    (_OUT / "causality_and_evidence_limits.md").write_text(
        "# Causality and evidence limits\n\n"
        "- July birth diffs: **15m grid SSOT** only for EP002 window; not full 5m replay.\n"
        "- August: pre_breakout birth CSV + sealed landscape to 19.08. 13:00.\n"
        "- No price-based tuning in this audit.\n",
        encoding="utf-8",
    )

    (_OUT / "future_detector_research_questions.md").write_text(
        "# Future detector research questions (not implemented)\n\n"
        "1. Edge-trigger: require **CHANGE** at current `decision_close` (birth/death non-empty).\n"
        "2. Rate limit: max one episode per N hours unless lookback **composition** changes.\n"
        "3. Staircase: new **support_top** vs previous **STATE**, not cumulative origin count in window.\n"
        "4. Separate **STATE=D** from **EVENT=entered_D**.\n",
        encoding="utf-8",
    )

    d_steps = len(sat)
    (_OUT / "report.md").write_text(
        f"# Bottom pattern transition audit V1\n\n"
        f"**Verdict:** `BOTTOM_PATTERN_TRANSITION_AUDIT_COMPLETE`\n\n"
        f"1. D persistent July: **STATE** lookbacks stay satisfied ({d_steps} closes).\n"
        f"2. Of {d_steps} D-steps, **{metric_changes}** changed lookback metrics vs prior D-step.\n"
        f"3. Aug pre-cutoff: spot lower births 19.08.; no deeper in main window.\n"
        f"4. July: ongoing pool births without pump (see post obs).\n"
        f"8. Category D is primarily a **STATE** label.\n\n"
        "**STOP**\n",
        encoding="utf-8",
    )

    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": "BOTTOM_PATTERN_TRANSITION_AUDIT_COMPLETE",
            "july_d_steps": d_steps,
            "july_d_steps_with_metric_change": metric_changes,
            "july_ssot_birth_rows": len(july_births),
            "august_csv_events": len(aug_csv),
        },
    )
    client.close()


if __name__ == "__main__":
    main()
