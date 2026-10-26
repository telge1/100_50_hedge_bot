#!/usr/bin/env python3
"""BT-D53 touch-connected price level SL forensic audit."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_bt_d53_touch_connected_sl_v1"
D47 = ROOT / "results/phase_bt_d47_trade_forensics_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"

USER_REF_UPPER = 0.08438
USER_REF_SL = USER_REF_UPPER * 1.002
MANUAL_LO = 0.08410
MANUAL_HI = 0.08438

REF_DECISIONS = ["2026-09-11T12:05:00Z", "2026-09-11T12:30:00Z"]

for p in (
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d53_touch_connected_sl_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range  # noqa: E402
from strategy_rules import short_sl  # noqa: E402

from connectivity_component_v1 import (  # noqa: E402
    component_from_entry,
    edge_kind,
    intervals_strict_overlap,
    intervals_touch_or_overlap,
    sl_from_component_upper,
)


def _neighbor_audit(snap: dict, lo: float, hi: float) -> dict[str, Any]:
    pk = f"{lo}|{hi}"
    rows = []
    for b in snap.get("upper_bands") or []:
        blo = float(b["lower_price"])
        bhi = float(b["upper_price"])
        rows.append(
            {
                "pool_lower": blo,
                "pool_upper": bhi,
                "edge_vs_entry": edge_kind(lo, hi, blo, bhi),
                "strict_overlap": intervals_strict_overlap(lo, hi, blo, bhi),
                "touch_or_overlap": intervals_touch_or_overlap(lo, hi, blo, bhi),
            }
        )
    next_band = next(
        (r for r in rows if r["pool_lower"] == hi or r["pool_upper"] == lo),
        None,
    )
    return {"entry_bounds": {"lower": lo, "upper": hi}, "next_touch_neighbor_sample": next_band}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg_path = Path((RUN_DIR / "config_path.txt").read_text().strip())
    config = load_yaml_and_validate(cfg_path)
    sl_pct = float(config["risk"]["sl_above_upper_top_pct"])
    start = config["backtest"]["start_utc"]
    end = config["backtest"]["end_utc"]
    end_excl = bool(config["backtest"].get("end_utc_exclusive"))
    prices = load_prices_from_ssot_window(start, end, end_utc_exclusive=end_excl)
    bars = _bars_in_range(prices, start, end, end_exclusive=end_excl)
    density = create_density_session(config)
    snap_at: dict[str, dict] = {}
    for bar in bars:
        try:
            snap_at[bar] = density.collect_at_close(bar)
        except KeyError:
            pass

    positions = list(csv.DictReader((D47 / "positions_audit.csv").open(encoding="utf-8")))
    pid_by_decision = {r["decision_close_utc"]: r for r in positions}

    # Reference 12:05 / 12:30
    ref_doc: dict[str, Any] = {"contract_version": "bt_d53_reference_1205_1230_v1", "cases": []}
    path_rows: list[dict[str, Any]] = []
    edge_cmp_rows: list[dict[str, Any]] = []

    for bar in REF_DECISIONS:
        aud = pid_by_decision[bar]
        lo, hi = float(aud["pool_lower"]), float(aud["pool_upper"])
        snap = snap_at[bar]
        strict = component_from_entry(snap, entry_pool_lower=lo, entry_pool_upper=hi, mode="strict_overlap")
        touch = component_from_entry(snap, entry_pool_lower=lo, entry_pool_upper=hi, mode="touch_or_overlap")
        atomic_sl = short_sl(hi, sl_pct)
        strict_sl = sl_from_component_upper(strict, sl_pct=sl_pct, short_sl_fn=short_sl)
        touch_sl = sl_from_component_upper(touch, sl_pct=sl_pct, short_sl_fn=short_sl)

        bands_above_manual = [
            b
            for b in snap.get("upper_bands") or []
            if float(b["lower_price"]) >= MANUAL_LO - 1e-9
            and float(b["upper_price"]) <= MANUAL_HI + 1e-9
        ]

        ref_doc["cases"].append(
            {
                "decision_close_utc": bar,
                "linked_fill_bar_utc": aud["entry_bar_utc"],
                "position_id": aud["position_id"],
                "selected_atomic": {"lower": lo, "upper": hi, "origins": aud["origin_set_key"]},
                "neighbor_audit": _neighbor_audit(snap, lo, hi),
                "strict_overlap_component": strict,
                "touch_or_overlap_component": touch,
                "sl_comparison": {
                    "atomic_sl": atomic_sl,
                    "strict_overlap_sl": strict_sl,
                    "touch_connected_sl": touch_sl,
                    "user_reference_sl": USER_REF_SL,
                },
                "manual_zone_note": {
                    "approx_lower": MANUAL_LO,
                    "approx_upper": MANUAL_HI,
                    "canonical_touch_component_upper": touch.get("component_upper"),
                    "delta_touch_upper_vs_user_ref": (
                        (touch.get("component_upper") - USER_REF_UPPER)
                        if touch.get("component_upper") is not None
                        else None
                    ),
                    "bands_with_bounds_inside_manual_approx": len(bands_above_manual),
                },
            }
        )

        for step_i, pk in enumerate(touch.get("path_entry_to_uppermost_pool_keys") or []):
            path_rows.append(
                {
                    "decision_close_utc": bar,
                    "connectivity_mode": "touch_or_overlap",
                    "step_index": step_i,
                    "pool_key": pk,
                }
            )
        for step_i, pk in enumerate(strict.get("path_entry_to_uppermost_pool_keys") or []):
            path_rows.append(
                {
                    "decision_close_utc": bar,
                    "connectivity_mode": "strict_overlap",
                    "step_index": step_i,
                    "pool_key": pk,
                }
            )

        edge_cmp_rows.append(
            {
                "decision_close_utc": bar,
                "atomic_upper": hi,
                "strict_component_upper": strict.get("component_upper"),
                "touch_component_upper": touch.get("component_upper"),
                "strict_member_count": strict.get("member_count"),
                "touch_member_count": touch.get("member_count"),
                "touch_edge_touch_edges": touch.get("edge_counts", {}).get("edge_touch_only_edges"),
                "touch_genuine_overlap_edges": touch.get("edge_counts", {}).get("genuine_overlap_edges"),
                "touch_status": touch.get("status"),
                "touch_unresolved_reason": touch.get("unresolved_reason") or "",
                "touch_articulation_count": len(touch.get("articulation_pool_keys") or []),
            }
        )

    (OUT / "reference_1205_1230.json").write_text(json.dumps(ref_doc, indent=2), encoding="utf-8")

    # All 19 positions
    sl19: list[dict[str, Any]] = []
    unchanged = diff = unresolved = 0
    for row in positions:
        bar = row["decision_close_utc"]
        snap = snap_at.get(bar)
        if not snap:
            continue
        lo, hi = float(row["pool_lower"]), float(row["pool_upper"])
        atomic = short_sl(hi, sl_pct)
        strict = component_from_entry(snap, entry_pool_lower=lo, entry_pool_upper=hi, mode="strict_overlap")
        touch = component_from_entry(snap, entry_pool_lower=lo, entry_pool_upper=hi, mode="touch_or_overlap")
        ts = sl_from_component_upper(touch, sl_pct=sl_pct, short_sl_fn=short_sl)
        ss = sl_from_component_upper(strict, sl_pct=sl_pct, short_sl_fn=short_sl)
        touch_sl = ts["sl"]
        category = "unchanged"
        if touch.get("status") != "CONFIRMED_PRICE_LEVEL_EDGE":
            category = "unresolved"
            unresolved += 1
        elif touch_sl is not None and touch_sl != atomic:
            category = "differs"
            diff += 1
        else:
            unchanged += 1
        sl19.append(
            {
                "position_id": row["position_id"],
                "decision_close_utc": bar,
                "atomic_sl": atomic,
                "strict_overlap_sl": ss["sl"],
                "strict_status": strict.get("status"),
                "touch_connected_sl": touch_sl,
                "touch_sl_status": ts["sl_status"],
                "touch_component_status": touch.get("status"),
                "touch_unresolved_reason": touch.get("unresolved_reason") or "",
                "touch_component_upper": touch.get("component_upper"),
                "touch_member_count": touch.get("member_count"),
                "sl_category_vs_atomic": category,
            }
        )

    def wcsv(name: str, rows: list[dict[str, Any]]) -> None:
        with (OUT / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    wcsv("connectivity_paths.csv", path_rows)
    wcsv("component_edge_comparison.csv", edge_cmp_rows)
    wcsv("sl_comparison_19.csv", sl19)

    weak = _weak_chain_md(ref_doc, sl19)
    (OUT / "weak_chain_risks.md").write_text(weak, encoding="utf-8")

    summary = _summary_md(ref_doc, unchanged, diff, unresolved, sl_pct)
    (OUT / "summary.md").write_text(summary, encoding="utf-8")
    print("Wrote", OUT, "unchanged", unchanged, "diff", diff, "unresolved", unresolved)
    return 0


def _weak_chain_md(ref_doc: dict, sl19: list[dict]) -> str:
    lines = ["# BT-D53 weak-chain risks\n"]
    for case in ref_doc["cases"]:
        t = case["touch_or_overlap_component"]
        lines.append(f"## {case['decision_close_utc']}\n\n")
        lines.append(f"- Touch members: **{t.get('member_count')}**\n")
        lines.append(f"- Edge touches / genuine overlaps: **{t.get('edge_counts')}**\n")
        lines.append(f"- Articulation pool keys: `{t.get('articulation_pool_keys')}`\n")
        lines.append(f"- Status: **{t.get('status')}** ({t.get('unresolved_reason') or 'ok'})\n")
        lines.append(f"- Path to uppermost: `{'` → `'.join(t.get('path_entry_to_uppermost_pool_keys') or [])}`\n\n")
    lines.append("## Fleet (19 positions)\n\n")
    u = [r for r in sl19 if r["sl_category_vs_atomic"] == "unresolved"]
    d = [r for r in sl19 if r["sl_category_vs_atomic"] == "differs"]
    lines.append(f"- Unresolved touch SL: **{len(u)}**\n")
    for r in u:
        lines.append(
            f"  - {r['decision_close_utc']}: {r['touch_unresolved_reason']} "
            f"(members={r['touch_member_count']}, upper={r['touch_component_upper']})\n"
        )
    lines.append(f"- Confirmed touch SL differs from atomic: **{len(d)}**\n")
    for r in d:
        lines.append(
            f"  - {r['decision_close_utc']}: atomic={r['atomic_sl']} touch={r['touch_connected_sl']}\n"
        )
    return "".join(lines)


def _summary_md(ref_doc: dict, unchanged: int, diff: int, unresolved: int, sl_pct: float) -> str:
    c1230 = next(c for c in ref_doc["cases"] if c["decision_close_utc"] == "2026-09-11T12:30:00Z")
    t = c1230["touch_or_overlap_component"]
    lines = [
        "# BT-D53 summary\n",
        f"- SL multiplier: upper × (1 + {sl_pct}/100)\n",
        f"- 19-position touch SL vs atomic: unchanged **{unchanged}**, differs **{diff}**, unresolved **{unresolved}**\n",
        f"- 12:30 touch component upper (canonical): **{t.get('component_upper')}**\n",
        f"- User approx upper 0.08438 → SL {USER_REF_SL}\n",
        "- BT-D46 / dashboard: **not modified**\n",
    ]
    return "".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
