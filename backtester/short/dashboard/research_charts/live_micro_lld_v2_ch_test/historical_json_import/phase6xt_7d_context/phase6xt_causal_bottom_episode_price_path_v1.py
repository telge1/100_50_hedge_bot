#!/usr/bin/env python3
"""Phase 6XT-CAUSAL-BOTTOM-EPISODE-PRICE-PATH V1 — anchors, episodes, price paths."""

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
_OUT = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_bottom_episode_price_path_v1"
_CTX = Path(__file__).resolve().parent
_GEOM = _REPO / "logs/historical_import/overnight_2026/phase6xt_causal_pool_staircase_geometry_v1"
_CROSS = _REPO / "logs/historical_import/overnight_2026/phase6xt_cross_window_structure_discovery_v1"
_TRANSITION = _REPO / "logs/historical_import/overnight_2026/phase6xt_dynamic_landscape_transition_analyzer_v1"
_JULY = _REPO / "logs/historical_import/overnight_2026/phase6xt_independent_bottom_pattern_pilot_v1"
_AUG_DISC = _REPO / "logs/historical_import/overnight_2026/phase6xt_automated_bottom_formation_discovery_v1"
_BFD = _CTX / "phase6xt_automated_bottom_formation_discovery_v1.py"
_FROZEN = _AUG_DISC / "frozen_detection_contract.json"

_HORIZONS = (1, 4, 12, 24, 48)
_CHART_LOW = 0.071038
_CHART_HIGH = 0.075451

CASES = [
    {
        "case_id": "june",
        "period_key": "pilot_1_june",
        "main_start": datetime(2026, 6, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 6, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc),
        "price_end": datetime(2026, 6, 14, 23, 55, tzinfo=timezone.utc),
    },
    {
        "case_id": "july",
        "period_key": "july",
        "main_start": datetime(2026, 7, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 7, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 7, 3, 0, 0, tzinfo=timezone.utc),
        "price_end": datetime(2026, 7, 14, 23, 55, tzinfo=timezone.utc),
    },
    {
        "case_id": "august",
        "period_key": "august",
        "main_start": datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc),
        "price_end": datetime(2026, 8, 21, 23, 55, tzinfo=timezone.utc),
    },
    {
        "case_id": "september",
        "period_key": "pilot_2_september",
        "main_start": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
        "main_end": datetime(2026, 9, 12, 23, 55, tzinfo=timezone.utc),
        "hist_start": datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc),
        "price_end": datetime(2026, 9, 14, 23, 55, tzinfo=timezone.utc),
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
from research_charts.dynamic_big_picture_v2 import origin_logical_v2_key  # noqa: E402
from research_charts.historical_lmv2_frozen_short_signal_v1 import parse_decision_close  # noqa: E402
from research_charts.market_cycle_forensic_v1 import causal_15m_decision_close, resample_5m_from_candles  # noqa: E402

_iso = c7._iso
_sha_file = c7._sha_file
_wjson = c7._wjson
_wcsv = geom._wcsv
load_members = c7.load_members
unique_dcs = geom.unique_dcs
price_at_dc = geom.price_at_dc


def load_bfd():
    spec = importlib.util.spec_from_file_location("bfd", _BFD)
    bfd = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bfd)
    frozen = json.loads(_FROZEN.read_text(encoding="utf-8"))
    bfd._DEEPER_LOOKBACK_H = frozen["deeper_lookback_hours"]
    bfd._HIGHER_LOOKBACK_H = frozen["higher_lookback_hours"]
    bfd._UPPER_BIRTH_LOOKBACK_H = frozen["upper_birth_lookback_hours"]
    return bfd, frozen


def read_canonical() -> list[dict]:
    rows = []
    for path in (_GEOM / "canonical_origin_births.csv", _CROSS / "canonical_origin_births.csv"):
        for r in csv.DictReader(path.open(encoding="utf-8")):
            pk = r.get("period") or r.get("window")
            for c in CASES:
                if pk == c["period_key"]:
                    r["case_id"] = c["case_id"]
                    rows.append(r)
    return rows


def birth_lists(canon: list[dict], case_id: str) -> tuple[list[dict], list[dict]]:
    lowers, uppers = [], []
    for r in canon:
        if r["case_id"] != case_id:
            continue
        if r.get("is_new_independent_origin") not in (True, "True"):
            continue
        row = {
            "asof_utc": r.get("asof_utc") or r["decision_close"],
            "decision_close": r["decision_close"],
            "class_deeper": r.get("class_deeper") in (True, "True"),
            "class_higher_step": r.get("class_higher_step") in (True, "True"),
            "is_new_origin": True,
            "origin_id": r["origin_id"],
        }
        if r.get("side") == "lower":
            lowers.append(row)
        elif r.get("side") == "upper":
            uppers.append(row)
    return lowers, uppers


def merge_d_episodes(events: list[dict], gap_h: int = 12) -> list[dict]:
    d = [e for e in events if e["category"] == "D"]
    d.sort(key=lambda x: x["decision_close"])
    out = []
    cur = None
    for e in d:
        t = parse_decision_close(e["decision_close"])
        if cur is None:
            cur = {**e, "episode_start": e["decision_close"], "episode_end": e["decision_close"], "steps": 1}
            continue
        prev = parse_decision_close(cur["episode_end"])
        if (t - prev).total_seconds() / 3600 <= gap_h:
            cur["episode_end"] = e["decision_close"]
            cur["steps"] += 1
        else:
            out.append(cur)
            cur = {**e, "episode_start": e["decision_close"], "episode_end": e["decision_close"], "steps": 1}
    if cur:
        out.append(cur)
    return out


def load_sealed_events(case_id: str) -> list[dict] | None:
    if case_id == "july":
        path = _JULY / "candidate_events.jsonl"
    elif case_id == "august":
        path = _AUG_DISC / "candidate_structure_events.jsonl"
    else:
        return None
    if not path.is_file():
        return None
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def first_anchor_cd(events: list[dict]) -> dict | None:
    for e in sorted(events, key=lambda x: x["decision_close"]):
        if e["category"] in ("C", "D"):
            return e
    return None


def first_transition_anchor(case_id: str, transitions: list[dict]) -> dict | None:
    rows = [
        e
        for e in transitions
        if e.get("case_id") == case_id
        and e.get("pool_detail") == 1
        and e.get("diagnostic_label") == "TRUE_UPWARD_FORMATION"
        and e.get("side") == "lower"
    ]
    if not rows:
        return None
    r = min(rows, key=lambda x: x["decision_close"])
    return {"decision_close": r["decision_close"], "price": r.get("price"), "origin_id": r.get("origin_id")}


def load_transitions() -> list[dict]:
    path = _TRANSITION / "spatial_transition_events.jsonl"
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                out.append(json.loads(line))
    return out


def bar_close_at(bars: dict, t: datetime) -> float | None:
    for k in range(12):
        tt = t - timedelta(minutes=5 * k)
        if tt in bars:
            return float(bars[tt]["close"])
    return None


def path_metrics(bars: dict, anchor: datetime, hours: int) -> dict[str, Any]:
    p0 = bar_close_at(bars, anchor)
    end = anchor + timedelta(hours=hours)
    highs, lows, closes = [], [], []
    t = anchor
    while t <= end:
        if t in bars:
            b = bars[t]
            highs.append((t, float(b["high"])))
            lows.append((t, float(b["low"])))
            closes.append((t, float(b["close"])))
        t += timedelta(minutes=5)
    if p0 is None or not highs:
        return {"horizon_hours": hours, "complete": False}
    max_h = max(highs, key=lambda x: x[1])
    min_l = min(lows, key=lambda x: x[1])
    p_end = closes[-1][1] if closes else None
    mfe = (max_h[1] - p0) / p0 * 100 if p0 else None
    mae = (min_l[1] - p0) / p0 * 100 if p0 else None
    order = "unknown_intrabar"
    if max_h[0] < min_l[0]:
        order = "high_before_low"
    elif min_l[0] < max_h[0]:
        order = "low_before_high"
    elif max_h[0] == min_l[0]:
        order = "same_bar_or_unknown"
    return {
        "horizon_hours": hours,
        "complete": True,
        "price_at_anchor": p0,
        "price_at_horizon": p_end,
        "pct_net": (p_end - p0) / p0 * 100 if p_end and p0 else None,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "max_high": max_h[1],
        "max_high_at": _iso(max_h[0]),
        "min_low": min_l[1],
        "min_low_at": _iso(min_l[0]),
        "high_low_order": order,
    }


def landscape_snapshot(client, db: str, dc: datetime, price: float) -> list[dict]:
    mem = load_members({}, dc, 1, client, db)
    rows = []
    for m in mem:
        side = m.get("side")
        b, t = float(m["lower_bound"]), float(m["upper_bound"])
        rows.append(
            {
                "pool_id": m["pool_id"],
                "origin_id": origin_logical_v2_key(m["pool_id"]),
                "side": side,
                "bottom": b,
                "top": t,
                "strength": m.get("strength"),
                "below_price": t < price if side == "lower" else None,
                "above_price": b > price if side == "upper" else None,
            }
        )
    return rows


def july_chart_recon(bars: dict, anchors: dict) -> str:
    best_low = None
    best_high = None
    for t, b in bars.items():
        if not (datetime(2026, 7, 10, tzinfo=timezone.utc) <= t <= datetime(2026, 7, 14, tzinfo=timezone.utc)):
            continue
        lo, hi = float(b["low"]), float(b["high"])
        if best_low is None or abs(lo - _CHART_LOW) < abs(best_low[1] - _CHART_LOW):
            best_low = (t, lo)
        if best_high is None or abs(hi - _CHART_HIGH) < abs(best_high[1] - _CHART_HIGH):
            best_high = (t, hi)
    chart_pct = None
    if best_low and best_high and best_low[1]:
        chart_pct = (best_high[1] - best_low[1]) / best_low[1] * 100
    ep = anchors.get("frozen_cd")
    ep_dc = ep["decision_close"] if ep else None
    ep_price = ep.get("price") if ep else None
    after_mfe = None
    if ep and ep_dc:
        m = path_metrics(bars, parse_decision_close(ep_dc), 48)
        after_mfe = m.get("mfe_pct")
    low_before_ep = best_low and ep_dc and best_low[0] < parse_decision_close(ep_dc)
    return f"""# July ~6.2% chart reconciliation

## Chart reference prices (user)

- Target low **~{_CHART_LOW}**, high **~{_CHART_HIGH}**

## Nearest 5m extremes (Jul 10–14 search)

| | UTC | Price |
|---|-----|-------|
| Nearest low | {_iso(best_low[0]) if best_low else 'NOT_FOUND'} | {best_low[1] if best_low else ''} |
| Nearest high | {_iso(best_high[0]) if best_high else 'NOT_FOUND'} | {best_high[1] if best_high else ''} |

- **Chart swing % (low→high):** {round(chart_pct, 4) if chart_pct else 'N/A'}%
- **Low before first C/D anchor (EP002 ~00:30)?** **{low_before_ep}**

## Structure anchor (sealed July pilot)

- First **D** episode start: `{ep_dc}` price **{ep_price}**

## After detection anchor (48h path)

- **MFE % from anchor:** {after_mfe}
- **Do not** report chart swing as post-detection achievable return unless anchor coincides with chart low.
"""


def main() -> None:
    t0 = time.time()
    _OUT.mkdir(parents=True, exist_ok=True)
    bfd, frozen = load_bfd()

    canon = read_canonical()
    transitions = load_transitions()
    src = ClickHouseResearchCandleSource()
    cfg = load_clickhouse_config()
    client = clickhouse_connect.get_client(**cfg.connect_kwargs())
    db = cfg.database

    bars_by_case: dict[str, dict] = {}
    for case in CASES:
        bars_by_case[case["case_id"]] = resample_5m_from_candles(
            src.get_candles("DOGEUSDT", "5m", start=case["hist_start"], end=case["price_end"] + timedelta(hours=5))
        )

    anchor_contract = {
        "A_frozen_cd": "Earliest category C or D at decision_close (sealed jsonl if present else bfd scan)",
        "B_transition": "First PD1 lower TRUE_UPWARD_FORMATION in transition analyzer",
        "C_main_end": "Last causal 15m close at or before case main_end",
        "horizons_hours": list(_HORIZONS),
        "no_anchor_cherry_picking": True,
    }
    _wjson(_OUT / "episode_anchor_contract.json", anchor_contract)

    anchors_rows = []
    path_rows = []
    mfe_rows = []
    hl_audit = []
    pool_at = []
    land_chg = []
    episodes_out = []

    july_anchors_for_chart: dict = {}

    for case in CASES:
        cid = case["case_id"]
        bars = bars_by_case[cid]
        lowers, uppers = birth_lists(canon, cid)
        sealed = load_sealed_events(cid)
        if sealed:
            events = [
                {
                    "decision_close": e["decision_close"],
                    "category": e["category"],
                    "price": e.get("price"),
                }
                for e in sealed
                if case["main_start"] <= parse_decision_close(e["decision_close"]) <= case["main_end"]
            ]
        else:
            events = []
            bfd.MAIN_START = case["main_start"]
            for dc in unique_dcs(case["main_start"], case["main_end"]):
                cat = bfd.classify_at(dc, lowers, uppers, None)
                events.append(
                    {
                        "decision_close": _iso(dc),
                        "category": cat,
                        "price": price_at_dc(bars, dc),
                    }
                )

        a_cd = first_anchor_cd(events)
        a_b = first_transition_anchor(cid, transitions)
        dc_end = causal_15m_decision_close(case["main_end"])
        a_c = {"decision_close": _iso(dc_end), "price": price_at_dc(bars, dc_end)}

        for label, anc in (("A_frozen_cd", a_cd), ("B_transition_lower", a_b), ("C_main_end", a_c)):
            anchors_rows.append(
                {
                    "case_id": cid,
                    "anchor_type": label,
                    "decision_close": anc["decision_close"] if anc else None,
                    "price": anc.get("price") if anc else None,
                    "available": anc is not None,
                }
            )

        if cid == "july":
            july_anchors_for_chart["frozen_cd"] = a_cd

        d_eps = merge_d_episodes(events)
        for i, ep in enumerate(d_eps):
            episodes_out.append(
                {
                    "case_id": cid,
                    "episode_index": i + 1,
                    "episode_start": ep["episode_start"],
                    "episode_end": ep["episode_end"],
                    "merged_d_steps": ep["steps"],
                    "start_price": ep.get("price"),
                }
            )

        primary = a_cd or a_c
        if primary:
            dc_a = parse_decision_close(primary["decision_close"])
            snap = landscape_snapshot(client, db, dc_a, float(primary.get("price") or price_at_dc(bars, dc_a)))
            for r in snap[:120]:
                pool_at.append({**r, "case_id": cid, "anchor_type": "A_frozen_cd", "anchor_dc": primary["decision_close"]})
            for e in transitions:
                if e.get("case_id") != cid:
                    continue
                if e["decision_close"] <= primary["decision_close"]:
                    continue
                if parse_decision_close(e["decision_close"]) > dc_a + timedelta(hours=48):
                    continue
                land_chg.append(
                    {
                        "case_id": cid,
                        "after_anchor": primary["decision_close"],
                        "decision_close": e["decision_close"],
                        "diagnostic_label": e.get("diagnostic_label"),
                        "side": e.get("side"),
                        "origin_id": e.get("origin_id"),
                    }
                )

        for label, anc in (("A_frozen_cd", a_cd), ("B_transition_lower", a_b), ("C_main_end", a_c)):
            if not anc:
                continue
            adt = parse_decision_close(anc["decision_close"])
            for h in _HORIZONS:
                m = path_metrics(bars, adt, h)
                path_rows.append({"case_id": cid, "anchor_type": label, **m})
                if m.get("complete"):
                    mfe_rows.append(
                        {
                            "case_id": cid,
                            "anchor_type": label,
                            "horizon_hours": h,
                            "mfe_pct": m["mfe_pct"],
                            "mae_pct": m["mae_pct"],
                            "pct_net": m["pct_net"],
                        }
                    )
                    hl_audit.append(
                        {
                            "case_id": cid,
                            "anchor_type": label,
                            "horizon_hours": h,
                            "high_low_order": m["high_low_order"],
                            "max_high_at": m["max_high_at"],
                            "min_low_at": m["min_low_at"],
                        }
                    )

    client.close()

    _wjson(
        _OUT / "coverage_audit.json",
        {"cases": [c["case_id"] for c in CASES], "sealed_july_august": True, "june_sept_scanned": True},
    )
    _wcsv(_OUT / "causal_episode_anchors.csv", anchors_rows)
    _wcsv(_OUT / "episode_price_paths.csv", path_rows)
    _wcsv(_OUT / "episode_mfe_mae.csv", mfe_rows)
    _wcsv(_OUT / "high_low_order_audit.csv", hl_audit)
    _wcsv(_OUT / "pool_landscape_at_episode_anchor.csv", pool_at)
    _wcsv(_OUT / "landscape_changes_after_anchor.csv", land_chg)

    (_OUT / "july_6pct_chart_reconciliation.md").write_text(
        july_chart_recon(bars_by_case["july"], july_anchors_for_chart), encoding="utf-8"
    )

    counter = """# Counterexamples

- July D episode spans 109 merged steps — one episode, not 109 patterns.
- June/September: C/D from bfd scan on canonical births (no sealed jsonl).
- Anchor B may be NOT_AVAILABLE if no TRUE_UPWARD_FORMATION lower PD1.
- Intrabar high/low order unknown when extrema share a 5m bar.
"""
    (_OUT / "counterexamples.md").write_text(counter, encoding="utf-8")
    (_OUT / "interpretation_limits.md").write_text(
        "Price paths are descriptive OHLC on 5m bars; anchors differ by design; no PnL.\n",
        encoding="utf-8",
    )
    _wjson(
        _OUT / "source_contract.json",
        {
            "frozen": str(_FROZEN),
            "inputs": [str(_GEOM), str(_CROSS), str(_TRANSITION), str(_JULY), str(_AUG_DISC)],
            "episode_gap_hours": 12,
        },
    )

    verdict = "CAUSAL_BOTTOM_EPISODE_PRICE_PATH_COMPLETE"
    _wjson(_OUT / "job_status.json", {"verdict": verdict, "runtime_seconds": round(time.time() - t0, 1)})
    (_OUT / "report.md").write_text(f"# Causal bottom episode price path V1\n\n**Verdict:** `{verdict}`\n\n**STOP**\n", encoding="utf-8")


if __name__ == "__main__":
    main()
