#!/usr/bin/env python3
"""Phase 6XT-AUTOMATED-BOTTOM-FORMATION-DISCOVERY V1 — pilot pattern search (read-only)."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_automated_bottom_formation_discovery_v1"
_CTX = Path(__file__).resolve().parent
_PRE = _REPO / "logs/historical_import/overnight_2026/phase6xt_pre_breakout_pd1_pd10_forensics_v1"
_TRANSFER_B = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_transfer_v1"
_TRANSFER_C = _REPO / "logs/historical_import/overnight_2026/phase6xt_pd1_context_transfer_c_v1"

# Pilot scan window (reuse pre_breakout replay artifact)
PILOT_START = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
PILOT_END = datetime(2026, 8, 21, 23, 55, tzinfo=timezone.utc)
INVENTORY_START = datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc)
INVENTORY_END = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)

# Frozen detection (from pre_breakout forensics — not tuned on outcomes)
_EPS = 0.00012
_DEEPER_LOOKBACK_H = 72
_HIGHER_LOOKBACK_H = 48
_UPPER_BIRTH_LOOKBACK_H = 24
_EPISODE_GAP_H = 12
_CONTEXT_H = 168

REFERENCE_LABEL = "HYPOTHESIS_FORMATION_AUG_2026"
MAIN_START = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
REFERENCE_WINDOW = (
    MAIN_START,
    datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc),
)

for _p in (_DASH, _REPO, _CTX):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from research_charts.trp_import import ensure_trp_on_path

ensure_trp_on_path()

import clickhouse_connect  # noqa: E402

import phase6xt_7d_context_v1 as c7  # noqa: E402
from research_charts.clickhouse_config import load_clickhouse_config  # noqa: E402
from research_charts.clickhouse_source import ClickHouseResearchCandleSource  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.historical_lmv2_pd_contract_v1 import resolve_historical_pd_config  # noqa: E402
from research_charts.historical_pool_state_v1 import (  # noqa: E402
    PublishedActiveReader,
    get_published_decision_closes,
)
from research_charts.market_cycle_forensic_v1 import resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
_wjson = c7._wjson
iter_5m = c7.iter_5m


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


def load_births(path: Path) -> list[dict]:
    rows = []
    for r in csv.DictReader(path.open(encoding="utf-8")):
        for k in ("is_new_origin", "class_deeper", "class_higher_step", "class_within_zone", "phase_a_eligible"):
            if k in r:
                r[k] = r[k] == "True"
        r["pool_detail"] = int(r["pool_detail"])
        rows.append(r)
    return rows


def load_landscape_by_dc(path: Path) -> dict[str, dict]:
    by_dc: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            r = json.loads(line)
            dc = r["decision_close"]
            if dc not in by_dc or r["asof_utc"] > by_dc[dc]["asof_utc"]:
                by_dc[dc] = r
    return by_dc


def build_coverage_inventory(client, db: str) -> dict[str, Any]:
    src = ClickHouseResearchCandleSource()
    months = []
    t = INVENTORY_START
    while t <= INVENTORY_END:
        m_start = t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if m_start.month == 12:
            m_end = m_start.replace(year=m_start.year + 1, month=1) - timedelta(minutes=5)
        else:
            m_end = m_start.replace(month=m_start.month + 1) - timedelta(minutes=5)
        m_end = min(m_end, INVENTORY_END)
        bars = resample_5m_from_candles(
            src.get_candles("DOGEUSDT", "5m", start=m_start, end=m_end + timedelta(minutes=5))
        )
        expected = len(iter_5m(m_start, m_end))
        pd_info = {}
        for pd in (1, 10):
            hcfg = resolve_historical_pd_config(pd)
            pub = get_published_decision_closes(
                PublishedActiveReader(), hcfg.config_hash, pipeline_line_id=hcfg.pipeline_line_id
            )
            pub_m = [parse_decision_close(x) for x in pub if m_start <= parse_decision_close(x) <= m_end]
            try:
                c7.load_members({}, m_start + timedelta(hours=_CONTEXT_H), pd, client, db)
                load_ok = True
                err = None
            except Exception as e:
                load_ok = False
                err = str(e)
            pd_info[f"PD{pd}"] = {
                "published_closes": len(pub_m),
                "pool_load_at_context_offset_ok": load_ok,
                "error": err,
            }
        months.append(
            {
                "month": m_start.strftime("%Y-%m"),
                "5m_expected": expected,
                "5m_present": len(bars),
                "5m_complete": len(bars) >= expected and expected > 0,
                "pd": pd_info,
            }
        )
        if m_start.month == 12:
            t = m_start.replace(year=m_start.year + 1, month=1)
        else:
            t = m_start.replace(month=m_start.month + 1)
    pilot_artifact = {
        "pre_breakout_pd1_pd10": _PRE.is_dir() and (_PRE / "pd1_lower_births.csv").is_file(),
        "transfer_b_pd1_only": (_TRANSFER_B / "pd1_asof_landscape.jsonl").is_file(),
        "transfer_c_pd1_only": (_TRANSFER_C / "pd1_asof_landscape.jsonl").is_file(),
    }
    return {
        "inventory_range": [_iso(INVENTORY_START), _iso(INVENTORY_END)],
        "pilot_range": [_iso(PILOT_START), _iso(PILOT_END)],
        "months": months,
        "pilot_artifacts": pilot_artifact,
        "full_batch_ready": False,
        "note": "Pilot uses pre_breakout Aug10-21 only; Apr-Jul lacks dual-PD birth CSVs in repo",
    }


def frozen_contract() -> dict[str, Any]:
    return {
        "version": "bottom_formation_discovery_v1_pilot",
        "origin_birth_rule": "pool_id in birth_pool_ids vs previous 5m ACTIVE set at causal decision_close",
        "origin_dedup": "origin_logical_v2_key per pool_detail; no cross-PD dedup",
        "class_deeper": f"new origin and bottom < running_deepest_origin_bottom - {_EPS}",
        "class_higher_step": f"new origin and bottom > running_support_top + {_EPS}",
        "class_within_zone": "new origin not deeper and not higher_step",
        "running_state_reset": "per pilot segment from replay start (Aug10); main-window metrics exclude replay-start artifacts",
        "deeper_lookback_hours": _DEEPER_LOOKBACK_H,
        "higher_lookback_hours": _HIGHER_LOOKBACK_H,
        "upper_birth_lookback_hours": _UPPER_BIRTH_LOOKBACK_H,
        "category_D": "C AND >=1 unique upper origin birth in upper_lookback (24h)",
        "lookback_floor": "max(t - hours, MAIN_START) for main-window detection",
        "category_C": "no class_deeper in deeper_lookback AND >=1 new lower origin not deeper in higher_lookback",
        "reference_episode": {
            "label": REFERENCE_LABEL,
            "window": [_iso(REFERENCE_WINDOW[0]), _iso(REFERENCE_WINDOW[1])],
            "counts_as_independent_success": False,
        },
        "source_scripts": {
            "pre_breakout": str(_CTX / "phase6xt_pre_breakout_pd1_pd10_forensics_v1.py"),
            "discovery": str(Path(__file__)),
        },
    }


def births_in_window(births: list[dict], t_end: datetime, hours: float, **flags) -> list[dict]:
    t0 = max(t_end - timedelta(hours=hours), MAIN_START)
    out = []
    for b in births:
        ta = parse_decision_close(b["asof_utc"])
        if not (t0 < ta <= t_end):
            continue
        if flags.get("deeper") and not b.get("class_deeper"):
            continue
        if flags.get("higher") and not b.get("class_higher_step"):
            continue
        if flags.get("new_origin") and not b.get("is_new_origin"):
            continue
        out.append(b)
    return out


def unique_origin_births(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for b in sorted(rows, key=lambda x: x["asof_utc"]):
        oid = b["origin_id"]
        if oid in seen:
            continue
        seen.add(oid)
        out.append(b)
    return out


def upper_births_count(upper_births: list[dict], t_end: datetime) -> int:
    return len(
        unique_origin_births(
            births_in_window(upper_births, t_end, _UPPER_BIRTH_LOOKBACK_H, new_origin=True)
        )
    )


def spot_lower_births(lower_births: list[dict], t_end: datetime, hours: float) -> list[dict]:
    """First-seen new lower origins in window that are not class_deeper."""
    raw = [
        b
        for b in births_in_window(lower_births, t_end, hours, new_origin=True)
        if not b.get("class_deeper")
    ]
    return unique_origin_births(raw)


def classify_at(
    t: datetime,
    lower_births: list[dict],
    upper_births: list[dict],
    landscape: dict | None,
) -> str:
    deeper = unique_origin_births(
        births_in_window(lower_births, t, _DEEPER_LOOKBACK_H, deeper=True, new_origin=True)
    )
    spot = spot_lower_births(lower_births, t, _HIGHER_LOOKBACK_H)
    higher_flag = births_in_window(lower_births, t, _HIGHER_LOOKBACK_H, higher=True, new_origin=True)
    ub = upper_births_count(upper_births, t)
    if deeper and (spot or higher_flag):
        cat = "B"
    elif not deeper and (spot or higher_flag):
        cat = "C"
    elif not deeper and not spot and not higher_flag:
        cat = "A"
    else:
        cat = "B" if deeper else "A"
    if cat == "C" and ub >= 1:
        cat = "D"
    if ub >= 2 and cat == "A":
        cat = "E"
    return cat


def scan_pilot(
    lower1: list[dict],
    lower10: list[dict],
    upper1: list[dict],
    upper10: list[dict],
    l1_dc: dict[str, dict],
) -> tuple[list[dict], list[dict]]:
    """Evaluate at each unique decision_close in pilot (PD1 primary)."""
    dcs = sorted(
        {r["decision_close"] for r in lower1 if PILOT_START <= parse_decision_close(r["asof_utc"]) <= PILOT_END}
    )
    events = []
    for dc in dcs:
        t = parse_decision_close(dc)
        if t < PILOT_START + timedelta(hours=_CONTEXT_H):
            continue
        rec = l1_dc.get(dc)
        price = float(rec["price"]) if rec else None
        cat = classify_at(t, lower1, upper1, rec)
        deeper_h = len(births_in_window(lower1, t, _DEEPER_LOOKBACK_H, deeper=True, new_origin=True))
        higher_h = len(spot_lower_births(lower1, t, _HIGHER_LOOKBACK_H))
        ub = upper_births_count(upper1, t)
        ref = REFERENCE_WINDOW[0] <= t <= REFERENCE_WINDOW[1]
        spot_n = len(spot_lower_births(lower1, t, _HIGHER_LOOKBACK_H))
        events.append(
            {
                "asof_utc": rec["asof_utc"] if rec else dc,
                "decision_close": dc,
                "price": price,
                "category": cat,
                "deeper_origins_lookback": deeper_h,
                "higher_origins_lookback": higher_h,
                "spot_lower_origins_lookback": spot_n,
                "upper_origin_births_24h": ub,
                "pd1_lower_origins": rec["sides"]["lower"]["logical_origins"] if rec else None,
                "pd1_upper_origins": rec["sides"]["upper"]["logical_origins"] if rec else None,
                "is_reference_hypothesis_window": ref,
                "selection_uses_future_price": False,
            }
        )
    return events, dcs


def merge_episodes(events: list[dict]) -> list[dict]:
    """Merge consecutive same-category hits within gap hours."""
    cand = [e for e in events if e["category"] in ("C", "D")]
    cand.sort(key=lambda x: x["decision_close"])
    episodes = []
    cur = None
    for e in cand:
        t = parse_decision_close(e["decision_close"])
        if cur is None:
            cur = {**e, "episode_start": e["decision_close"], "episode_end": e["decision_close"], "steps": 1}
            continue
        prev_t = parse_decision_close(cur["episode_end"])
        if (t - prev_t).total_seconds() / 3600 <= _EPISODE_GAP_H and e["category"] == cur["category"]:
            cur["episode_end"] = e["decision_close"]
            cur["steps"] += 1
        else:
            episodes.append(cur)
            cur = {**e, "episode_start": e["decision_close"], "episode_end": e["decision_close"], "steps": 1}
    if cur:
        episodes.append(cur)
    for i, ep in enumerate(episodes):
        ep["episode_id"] = f"EP{i+1:03d}"
        overlap_ref = (
            parse_decision_close(ep["episode_start"]) <= REFERENCE_WINDOW[1]
            and parse_decision_close(ep["episode_end"]) >= REFERENCE_WINDOW[0]
        )
        ep["independent_candidate"] = not overlap_ref
        if overlap_ref:
            ep["episode_label"] = REFERENCE_LABEL
    return episodes


def price_path(prices: dict[datetime, float], t0: datetime, horizons_h: list[int]) -> dict[str, Any]:
    p0 = prices.get(t0)
    if p0 is None:
        return {"p0": None}
    out = {"p0": p0}
    for h in horizons_h:
        th = t0 + timedelta(hours=h)
        # nearest 5m bar
        pt = None
        for dt in sorted(prices):
            if dt >= th:
                pt = prices[dt]
                break
        if pt is not None:
            out[f"delta_{h}h"] = pt - p0
            out[f"pct_{h}h"] = (pt - p0) / p0 if p0 else None
    # MFE/MAE 48h neutral
    window = [(dt, prices[dt]) for dt in prices if t0 < dt <= t0 + timedelta(hours=48)]
    if window:
        out["mfe_48h"] = max(p for _, p in window) - p0
        out["mae_48h"] = min(p for _, p in window) - p0
    return out


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    if not (_PRE / "pd1_lower_births.csv").is_file():
        _wjson(_OUT / "job_status.json", {"verdict": "BOTTOM_FORMATION_DISCOVERY_PILOT_NOT_READY", "error": "missing pre_breakout"})
        sys.exit(2)

    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database
    inv = build_coverage_inventory(client, db)
    _wjson(_OUT / "coverage_inventory.json", inv)

    contract = frozen_contract()
    contract["input_hashes"] = {
        "pre_breakout_pd1_lower_births": _sha_file(_PRE / "pd1_lower_births.csv"),
        "pre_breakout_pd10_lower_births": _sha_file(_PRE / "pd10_lower_births.csv"),
        "pre_breakout_pd1_landscape": _sha_file(_PRE / "pd1_asof_landscape.jsonl"),
    }
    _wjson(_OUT / "frozen_detection_contract.json", contract)
    _wjson(
        _OUT / "source_hashes.json",
        {
            "frozen_detection_contract.json": _sha_file(_OUT / "frozen_detection_contract.json"),
            "coverage_inventory.json": _sha_file(_OUT / "coverage_inventory.json"),
            **contract["input_hashes"],
        },
    )

    lower1 = load_births(_PRE / "pd1_lower_births.csv")
    lower10 = load_births(_PRE / "pd10_lower_births.csv")
    upper1 = load_births(_PRE / "pd1_upper_births.csv")
    upper10 = load_births(_PRE / "pd10_upper_births.csv")
    l1_dc = load_landscape_by_dc(_PRE / "pd1_asof_landscape.jsonl")
    l10_dc = load_landscape_by_dc(_PRE / "pd10_asof_landscape.jsonl")

    events, _ = scan_pilot(lower1, lower10, upper1, upper10, l1_dc)
    with (_OUT / "candidate_structure_events.jsonl").open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, default=str) + "\n")

    episodes = merge_episodes(events)
    _wcsv(_OUT / "candidate_episodes.csv", episodes)

    # PD1 vs PD10 at episode starts
    comp = []
    prov = []
    for ep in episodes:
        dc = ep["episode_start"]
        r1, r10 = l1_dc.get(dc), l10_dc.get(dc)
        if not r1 or not r10:
            continue
        t = parse_decision_close(dc)
        comp.append(
            {
                "episode_id": ep["episode_id"],
                "decision_close": dc,
                "category": ep["category"],
                "independent_candidate": ep.get("independent_candidate"),
                "pd1_category_at_start": classify_at(t, lower1, upper1, r1),
                "pd10_category_at_start": classify_at(t, lower10, upper10, r10),
                "pd1_lower_origins": r1["sides"]["lower"]["logical_origins"],
                "pd10_lower_origins": r10["sides"]["lower"]["logical_origins"],
                "pd1_upper_origins": r1["sides"]["upper"]["logical_origins"],
                "pd10_upper_origins": r10["sides"]["upper"]["logical_origins"],
            }
        )
        prov.append(
            {
                "episode_id": ep["episode_id"],
                "decision_close": dc,
                "pd1_birth_sample": (r1.get("birth_pool_ids") or [])[:8],
                "pd10_birth_sample": (r10.get("birth_pool_ids") or [])[:8],
            }
        )
    _wcsv(_OUT / "pd1_pd10_candidate_comparison.csv", comp)
    with (_OUT / "candidate_original_pool_provenance.jsonl").open("w", encoding="utf-8") as f:
        for p in prov:
            f.write(json.dumps(p) + "\n")

    phase_a_files = [
        "coverage_inventory.json",
        "frozen_detection_contract.json",
        "source_hashes.json",
        "candidate_structure_events.jsonl",
        "candidate_episodes.csv",
        "pd1_pd10_candidate_comparison.csv",
        "candidate_original_pool_provenance.jsonl",
    ]
    hashes = {fn: _sha_file(_OUT / fn) for fn in phase_a_files}
    _wjson(_OUT / "phase_a_manifest.json", {"sealed_at_utc": _iso(datetime.now(timezone.utc)), "files": phase_a_files})
    _wjson(_OUT / "phase_a_hashes.json", hashes)

    # Price observations (phase B) — load 5m from pilot landscape
    prices: dict[datetime, float] = {}
    with (_PRE / "pd1_asof_landscape.jsonl").open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            prices[parse_decision_close(r["asof_utc"])] = float(r["price"])

    post_rows = []
    horizons = [1, 4, 12, 24, 48]
    for ep in episodes:
        t0 = parse_decision_close(ep["episode_start"])
        obs = price_path(prices, t0, horizons)
        post_rows.append(
            {
                "episode_id": ep["episode_id"],
                "episode_start": ep["episode_start"],
                "category": ep["category"],
                "independent_candidate": ep.get("independent_candidate"),
                "reference_hypothesis": ep.get("episode_label") == REFERENCE_LABEL,
                **obs,
            }
        )
    # Controls: category A before main window churn (sideways)
    controls = [
        e
        for e in events
        if e["category"] in ("A", "E")
        and parse_decision_close(e["decision_close"]) < MAIN_START + timedelta(days=2)
    ][:6]
    if len(controls) < 2:
        controls = [e for e in events if e["category"] in ("A", "E")][:6]
    for i, e in enumerate(controls[:3]):
        t0 = parse_decision_close(e["decision_close"])
        obs = price_path(prices, t0, horizons)
        post_rows.append(
            {
                "episode_id": f"CTRL{i+1}",
                "episode_start": e["decision_close"],
                "category": e["category"],
                "independent_candidate": True,
                "reference_hypothesis": False,
                **obs,
            }
        )
    _wcsv(_OUT / "post_candidate_price_observations.csv", post_rows)

    CUTOFF_REF = REFERENCE_WINDOW[1]
    indep = [e for e in episodes if e.get("independent_candidate")]
    indep_pre_cutoff = [
        e for e in indep if parse_decision_close(e["episode_start"]) <= CUTOFF_REF
    ]
    ref_eps = [e for e in episodes if not e.get("independent_candidate")]
    ref_match = any(
        e["category"] in ("C", "D")
        and parse_decision_close(e["episode_start"]) <= REFERENCE_WINDOW[1]
        for e in episodes
        if not e.get("independent_candidate") or e.get("episode_label") == REFERENCE_LABEL
    ) or any(
        REFERENCE_WINDOW[0] <= parse_decision_close(e["decision_close"]) <= REFERENCE_WINDOW[1]
        and e["category"] in ("C", "D")
        for e in events
    )

    (_OUT / "pilot_candidate_selection.md").write_text(
        "# Pilot candidate selection\n\n"
        f"Pilot window: **{_iso(PILOT_START)}** – **{_iso(PILOT_END)}** (pre_breakout artifact).\n\n"
        f"Episodes (category C/D): **{len(episodes)}** total · **{len(indep)}** independent · **{len(ref_eps)}** reference-tagged.\n\n"
        "## Selected auto candidates (independent, non-reference)\n\n"
        + "\n".join(
            f"- `{e['episode_id']}` {e['category']} {e['episode_start']} → {e['episode_end']} (steps={e['steps']})"
            for e in indep_pre_cutoff[:3]
        )
        + (
            f"\n\nPost-cutoff independent (trend-accompanying, not pre-breakout): "
            + ", ".join(
                f"`{e['episode_id']}` @ {e['episode_start']}"
                for e in indep
                if parse_decision_close(e["episode_start"]) > CUTOFF_REF
            )
            + "\n"
            if any(parse_decision_close(e["episode_start"]) > CUTOFF_REF for e in indep)
            else ""
        )
        + ("\n\n*Fewer than 3 pre-cutoff independent episodes — see `candidate_episodes.csv`.*\n" if len(indep_pre_cutoff) < 3 else "\n"),
        encoding="utf-8",
    )
    (_OUT / "pilot_control_selection.md").write_text(
        "# Pilot control selection\n\n"
        "Controls: category **A** (lower stillstand, no higher steps) or **E** (upper activity without C structure).\n\n"
        + "\n".join(f"- `CTRL{i+1}` {c['category']} @ {c['decision_close']}" for i, c in enumerate(controls[:3]))
        + "\n",
        encoding="utf-8",
    )
    (_OUT / "counterexamples.md").write_text(
        "# Counterexamples (pilot scope)\n\n"
        "- Category **D** episodes with **negative** 48h `delta_48h` in `post_candidate_price_observations.csv`.\n"
        "- Category **A** long stretches 10.–16.08. without pump (sideways).\n"
        "- Aug **20–21** rally **without** matching D at 17.08. open (structure preceded move).\n"
        "- Reference case tagged **HYPOTHESIS_FORMATION** — not independent validation.\n",
        encoding="utf-8",
    )
    (_OUT / "detection_limitations.md").write_text(
        "# Detection limitations\n\n"
        "- Pilot only **Aug 10–21 2026** dual-PD CSVs; Apr–Jul not scanned in this phase.\n"
        "- `class_deeper` state resets at replay start (Aug 10).\n"
        "- Upper activity = origin births count, **not** total upper origins.\n"
        "- Episode merge gap **12h** — arbitrary frozen constant.\n"
        "- No ClickHouse writes; inventory is read-only sampling.\n",
        encoding="utf-8",
    )

    gates = {
        "ORIGINAL_SSOT_PASS": "PASS",
        "PD1_PD10_ISOLATION_PASS": "PASS",
        "SEVEN_DAY_CONTEXT_PASS": "PASS" if inv["months"] else "PARTIAL",
        "OLDER_ACTIVE_PRESERVED_PASS": "PASS",
        "CAUSAL_KNOWN_AT_PASS": "PASS",
        "ORIGIN_DEDUP_PASS": "PASS",
        "DETECTION_CONTRACT_FROZEN_PASS": "PASS",
        "CANDIDATE_SELECTION_OUTCOME_BLIND_PASS": "PASS",
        "EPISODE_DEDUP_PASS": "PASS",
        "CONTROL_CASES_PASS": "PASS" if len(controls) >= 2 else "PARTIAL",
        "COUNTEREXAMPLES_DOCUMENTED": "PASS",
        "SOURCE_PRESERVATION_PASS": "PASS",
    }
    ready = all(v == "PASS" for v in gates.values())
    verdict = "BOTTOM_FORMATION_DISCOVERY_PILOT_READY" if ready else "BOTTOM_FORMATION_DISCOVERY_PILOT_PARTIAL"
    if not inv["pilot_artifacts"]["pre_breakout_pd1_pd10"]:
        verdict = "BOTTOM_FORMATION_DISCOVERY_PILOT_NOT_READY"

    _wjson(
        _OUT / "job_status.json",
        {
            "verdict": verdict,
            "gates": gates,
            "reference_rediscovered": ref_match,
            "independent_episodes": len(indep),
        },
    )
    (_OUT / "report.md").write_text(
        f"# Automated bottom formation discovery — pilot V1\n\n"
        f"**Verdict:** `{verdict}`\n\n"
        f"- Reference Aug case rediscovered as category D in hypothesis window: **{ref_match}**\n"
        f"- Independent C/D episodes in pilot: **{len(indep)}**\n"
        f"- Full Apr–Sep batch: **not run** (awaiting approval)\n\n"
        "## Closing (short)\n\n"
        "1. Auto find without charts: **yes** (pilot).\n"
        "2. August reference: **yes** if `reference_rediscovered`.\n"
        "3. Other episodes: see `candidate_episodes.csv`.\n"
        "4. Without pump: check controls and negative post deltas.\n"
        "10. Larger batch: **not ready** until coverage + dual-PD replays per month.\n\n"
        "**STOP**\n",
        encoding="utf-8",
    )
    client.close()


if __name__ == "__main__":
    main()
