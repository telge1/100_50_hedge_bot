#!/usr/bin/env python3
"""BT-D39 read-only dynamic upper-pool forensics (no new signals)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"
OUT_DIR = ROOT / "results/phase_bt_d39_dynamic_upper_pool_forensics_v1"

BT_D26 = ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1"
BT_D7 = ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"
BT_D6 = ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1"
BT_D23 = ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1"
BT_D8 = ROOT / "tools/phase_bt_d8_canonical_density_cache_v1"
PHASE4D = ROOT / "tools/phase4d_pool_landscape_backtester_v2"
for p in (BT_D26, BT_D7, BT_D6, BT_D8, BT_D23, PHASE4D):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from pool_first_signal_v1 import _structural_eligible_upper  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range, measure_upper_gap_pct  # noqa: E402
from signal_types_v1 import pool_key  # noqa: E402


def _origin_count(ids: str | None) -> int:
    if not ids:
        return 0
    return len([x for x in str(ids).split("|") if x.strip()])


def _bounds_tuple(lo: float | None, hi: float | None) -> tuple[float, float] | None:
    if lo is None or hi is None:
        return None
    return (round(float(lo), 12), round(float(hi), 12))


def _same_price_zone(a: dict | None, b: dict | None) -> bool:
    if not a or not b:
        return False
    return pool_key(float(a["pool_lower"]), float(a["pool_upper"])) == pool_key(
        float(b["pool_lower"]), float(b["pool_upper"])
    )


def classify_transition(
    prev: dict | None,
    cur: dict | None,
    selection_reason: str | None,
    invalidation_reason: str | None,
) -> str:
    if prev is None or cur is None:
        if selection_reason and "FIRST" in selection_reason:
            return "1_FIRST_SELECT"
        if selection_reason and "INVALIDATION" in (selection_reason or ""):
            return "4_DEACTIVATION_RESELECT"
        return "6_UNCLEAR"
    same_key = prev.get("pool_key") == cur.get("pool_key")
    same_lo = abs(float(prev["pool_lower"]) - float(cur["pool_lower"])) < 1e-9
    same_hi = abs(float(prev["pool_upper"]) - float(cur["pool_upper"])) < 1e-9
    same_origin = prev.get("origin_set_key") == cur.get("origin_set_key")
    if same_key and same_lo and same_hi and same_origin:
        return "6_UNCLEAR_EMIT_WITH_IDENTICAL_IDENTITY"
    if same_lo and not same_hi:
        return "3_POOL_BOUND_DRIFT_OR_SPLIT_MERGE"
    if same_key and not same_origin:
        return "2_ORIGIN_COMPOSITION_CHANGE_SAME_POOL_KEY"
    if not same_lo:
        if selection_reason == "LOWER_ELIGIBLE_UPPER_POOL_CHANGED":
            return "5_SWITCH_TO_LOWER_ELIGIBLE_ZONE"
        if selection_reason == "RESELECT_AFTER_INVALIDATION_BT_D21_LOWEST":
            return "4_DEACTIVATION_RESELECT"
        return "1_DIFFERENT_PRICE_ZONE"
    return "6_UNCLEAR"


def list_eligible_uppers(
    config: dict,
    *,
    bar: str,
    snap: dict,
    candle: dict,
    pool_age: dict[str, int],
) -> list[dict[str, Any]]:
    uppers = snap.get("upper_bands") or []
    rows: list[dict[str, Any]] = []
    min_gap = float(config["short_entry"]["upper_gap_min_pct"])
    for b in uppers:
        ok, fail = _structural_eligible_upper(
            config,
            band=b,
            decision_close_utc=bar,
            candle=candle,
            snapshot=snap,
            pool_age_bars=pool_age,
            require_large_upper_gap=True,
        )
        if not ok:
            continue
        lo = float(b["lower_price"])
        hi = float(b["upper_price"])
        pk = pool_key(lo, hi)
        rows.append(
            {
                "pool_lower": lo,
                "pool_upper": hi,
                "pool_key": pk,
                "origin_set_key": b.get("origin_set_key") or b.get("origin_ids"),
                "upper_origin_ids": b.get("origin_ids"),
                "active_origin_count": _origin_count(b.get("origin_ids")),
                "pool_age_bars": pool_age.get(pk, 0),
                "upper_gap_pct": measure_upper_gap_pct(uppers, hi, min_gap),
            }
        )
    rows.sort(key=lambda r: (r["pool_lower"], r["pool_upper"]))
    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    setups_doc = json.loads((RUN_DIR / "setups.json").read_text(encoding="utf-8"))
    setups = setups_doc["setups"]
    assert len(setups) == 102

    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)

    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_excl)
    density = create_density_session(config)

    pool_age: dict[str, int] = {}
    snap_by_bar: dict[str, dict] = {}
    eligible_by_bar: dict[str, list] = {}

    for bar in bars:
        try:
            snap = density.collect_at_close(bar)
        except KeyError:
            continue
        snap_by_bar[bar] = snap
        for b in snap.get("upper_bands") or []:
            pk = pool_key(float(b["lower_price"]), float(b["upper_price"]))
            pool_age[pk] = pool_age.get(pk, 0) + 1
        candle = prices.get(bar)
        if candle:
            eligible_by_bar[bar] = list_eligible_uppers(
                config, bar=bar, snap=snap, candle=candle, pool_age=pool_age
            )

    episodes: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []

    for i, s in enumerate(setups):
        bar = s["decision_utc"]
        prev = s.get("previous_candidate")
        nxt = setups[i + 1] if i + 1 < len(setups) else None
        candle = prices.get(bar) or {}
        close_px = candle.get("c")
        pk = pool_key(float(s["pool_lower"]), float(s["pool_upper"]))
        elig = eligible_by_bar.get(bar, [])
        alts = [e for e in elig if e["pool_key"] != pk]

        ep = {
            "episode_index": i + 1,
            "setup_id": s["setup_id"],
            "decision_utc": bar,
            "close_price": close_px,
            "pool_lower": s["pool_lower"],
            "pool_upper": s["pool_upper"],
            "pool_key": pk,
            "upper_origin_ids": s["upper_origin_ids"],
            "origin_set_key": s["origin_set_key"],
            "active_origin_count": _origin_count(s["upper_origin_ids"]),
            "pool_age_bars_at_select": pool_age.get(pk, 0),
            "upper_gap_pct": s.get("upper_gap_pct"),
            "planned_entry": s["planned_entry"],
            "planned_sl": s["planned_sl"],
            "planned_tp": s["planned_tp"],
            "selection_reason": s.get("selection_reason"),
            "invalidation_reason": s.get("invalidation_reason"),
            "dynamic_state": s.get("dynamic_state"),
            "prev_pool_lower": (prev or {}).get("pool_lower"),
            "prev_pool_upper": (prev or {}).get("pool_upper"),
            "prev_origin_set_key": (prev or {}).get("origin_set_key"),
            "next_decision_utc": nxt["decision_utc"] if nxt else None,
            "next_pool_lower": nxt["pool_lower"] if nxt else None,
            "next_pool_upper": nxt["pool_upper"] if nxt else None,
            "eligible_upper_count": len(elig),
            "eligible_alternatives_json": json.dumps(alts[:12], separators=(",", ":")),
        }
        episodes.append(ep)

        tr = {
            "episode_index": i + 1,
            "decision_utc": bar,
            "selection_reason": s.get("selection_reason"),
            "invalidation_reason": s.get("invalidation_reason"),
            "forensic_class": classify_transition(
                prev,
                {
                    "pool_key": pk,
                    "pool_lower": s["pool_lower"],
                    "pool_upper": s["pool_upper"],
                    "origin_set_key": s["origin_set_key"],
                },
                s.get("selection_reason"),
                s.get("invalidation_reason"),
            ),
            "prev_pool_key": (prev or {}).get("pool_key"),
            "cur_pool_key": pk,
            "same_price_zone_as_prev": _same_price_zone(
                prev,
                {
                    "pool_lower": s["pool_lower"],
                    "pool_upper": s["pool_upper"],
                },
            ),
            "origin_set_changed": (prev or {}).get("origin_set_key") != s.get("origin_set_key")
            if prev
            else None,
        }
        transitions.append(tr)

    # episodes.csv
    if episodes:
        with (OUT_DIR / "episodes.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(episodes[0].keys()))
            w.writeheader()
            w.writerows(episodes)

    with (OUT_DIR / "transitions.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(transitions[0].keys()))
        w.writeheader()
        w.writerows(transitions)

    # reference 15:35
    ref_bar = "2026-09-11T15:35:00Z"
    ref_setup = next(s for s in setups if s["decision_utc"] == ref_bar)
    ref_elig = eligible_by_bar.get(ref_bar, [])
    ref_snap = snap_by_bar.get(ref_bar, {})
    ref = {
        "decision_utc": ref_bar,
        "close_price": (prices.get(ref_bar) or {}).get("c"),
        "selected": ref_setup,
        "previous_candidate": ref_setup.get("previous_candidate"),
        "eligible_uppers_ordered_by_pool_lower": ref_elig,
        "unresolved_at_decision": [
            u
            for u in setups_doc.get("unresolved_events") or []
            if u.get("decision_utc") == ref_bar
        ],
        "why_selected": (
            "BT-D21 pool_first: lowest pool_lower among structurally eligible uppers at this 5m close; "
            f"selection_reason={ref_setup.get('selection_reason')}; "
            f"invalidation_reason={ref_setup.get('invalidation_reason')}"
        ),
        "zone_change_vs_previous": not _same_price_zone(
            ref_setup.get("previous_candidate"),
            {"pool_lower": ref_setup["pool_lower"], "pool_upper": ref_setup["pool_upper"]},
        ),
    }
    (OUT_DIR / "reference_1535.json").write_text(json.dumps(ref, indent=2), encoding="utf-8")

    # 10 manual cases — stratified indices
    picks = [
        1,  # FIRST
        2,  # invalidation reselect
        4,  # invalidation
        11,  # pre-15:35 zone step
        12,  # reference 15:35 UTC
        13,  # 15:40 bound drift same lower
        20,  # bound drift class
        35,  # lower-eligible zone switch
        50,
        65,
    ]
    manual = []
    for idx in picks:
        s = setups[idx - 1]
        bar = s["decision_utc"]
        pk = pool_key(float(s["pool_lower"]), float(s["pool_upper"]))
        elig = eligible_by_bar.get(bar, [])
        manual.append(
            {
                "episode_index": idx,
                "decision_utc": bar,
                "pool_lower": s["pool_lower"],
                "pool_upper": s["pool_upper"],
                "upper_origin_ids": s["upper_origin_ids"],
                "planned_entry": s["planned_entry"],
                "planned_sl": s["planned_sl"],
                "planned_tp": s["planned_tp"],
                "selection_reason": s.get("selection_reason"),
                "invalidation_reason": s.get("invalidation_reason"),
                "forensic_class": transitions[idx - 1]["forensic_class"],
                "eligible_upper_count": len(elig),
                "top_alternative_lowers": "|".join(
                    f"{e['pool_lower']:.6f}-{e['pool_upper']:.6f}" for e in elig if e["pool_key"] != pk
                )[:500],
                "chart_check_question": (
                    f"At {bar} GO TO viewport: is upper band [{s['pool_lower']}, {s['pool_upper']}] "
                    f"the lowest eligible upper (Depth2 min origins 2) with gap above?"
                ),
            }
        )
    with (OUT_DIR / "manual_review_10.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(manual[0].keys()))
        w.writeheader()
        w.writerows(manual)

    from collections import Counter

    sr = Counter(s.get("selection_reason") for s in setups)
    fc = Counter(t["forensic_class"] for t in transitions)
    summary = {
        "run_id": setups_doc.get("run_id"),
        "setup_count": len(setups),
        "selection_reason_counts": dict(sr),
        "forensic_class_counts": dict(fc),
        "density": setups_doc.get("density_warmup_note"),
        "reference_1535_selection_reason": ref_setup.get("selection_reason"),
    }
    (OUT_DIR / "summary.md").write_text(
        "# BT-D39 forensics summary\n\n```json\n"
        + json.dumps(summary, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
