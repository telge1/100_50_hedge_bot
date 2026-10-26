#!/usr/bin/env python3
"""BT-D52 isolated true price-level SL audit (no BT-D46 / SSOT changes)."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "results/phase_bt_d52_true_price_level_sl_v1"
D47 = ROOT / "results/phase_bt_d47_trade_forensics_v1"
RUN_DIR = ROOT / "results/setup_scanner_runs/bt_d26_dynamic_referenz_sep11_24h_v1"

USER_REF_UPPER = 0.08438
USER_REF_SL = USER_REF_UPPER * 1.002

REF_CASES = [
    {
        "fill_bar_utc": "2026-09-11T12:25:00Z",
        "decision_close_utc": "2026-09-11T12:05:00Z",
        "position_id": "78cd59a534af80d5",
    },
    {
        "fill_bar_utc": "2026-09-11T12:35:00Z",
        "decision_close_utc": "2026-09-11T12:30:00Z",
        "position_id": "aa1a8a9c1bc83efc",
    },
]

for p in (
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d8_canonical_density_cache_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
    ROOT / "tools/phase_bt_d52_true_price_level_sl_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from config_yaml_v1 import load_yaml_and_validate  # noqa: E402
from density_bridge_v1 import create_density_session  # noqa: E402
from price_feed_v1 import load_prices_from_ssot_window  # noqa: E402
from setup_scanner_v1 import _bars_in_range  # noqa: E402
from strategy_rules import short_sl  # noqa: E402

from price_level_component_v1 import component_from_entry_pool, composite_sl_from_component  # noqa: E402


def _audit_row(
    *,
    decision_close_utc: str,
    pool_lo: float,
    pool_hi: float,
    snap: dict,
    sl_pct: float,
    legacy_sl: float | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    comp = component_from_entry_pool(snap, entry_pool_lower=pool_lo, entry_pool_upper=pool_hi)
    sl_doc = composite_sl_from_component(comp, sl_pct=sl_pct, short_sl_fn=short_sl)
    atomic_sl = short_sl(pool_hi, sl_pct)
    candidate = sl_doc.get("candidate_composite_sl")
    return {
        "decision_close_utc": decision_close_utc,
        "entry_pool_lower": pool_lo,
        "entry_pool_upper": pool_hi,
        "legacy_atomic_sl": legacy_sl if legacy_sl is not None else atomic_sl,
        "legacy_atomic_sl_recomputed": atomic_sl,
        "price_level_status": comp.get("status"),
        "unresolved_reason": comp.get("unresolved_reason") or "",
        "component_lower": comp.get("component_lower"),
        "component_upper": comp.get("component_upper"),
        "component_member_count": comp.get("member_count"),
        "component_origin_count": comp.get("unique_origin_count"),
        "candidate_composite_sl": candidate,
        "sl_mode": sl_doc.get("sl_mode"),
        "sl_delta_vs_legacy": (candidate - atomic_sl) if candidate is not None else "",
        "user_reference_upper": USER_REF_UPPER,
        "user_reference_sl": USER_REF_SL,
        "delta_candidate_vs_user_ref_sl": (candidate - USER_REF_SL) if candidate is not None else "",
        **extra,
    }


def _run_safety_checks(rows: list[dict[str, Any]]) -> list[str]:
    issues: list[str] = []
    for r in rows:
        if r["legacy_atomic_sl_recomputed"] != r["legacy_atomic_sl"]:
            issues.append(f"{r['decision_close_utc']}: legacy SL recompute mismatch")
        if r["price_level_status"] == "CONFIRMED_PRICE_LEVEL_EDGE":
            if r["candidate_composite_sl"] is None:
                issues.append(f"{r['decision_close_utc']}: confirmed but no candidate SL")
            if r["entry_pool_lower"] < r["component_lower"] - 1e-15:
                issues.append(f"{r['decision_close_utc']}: entry below component lower")
            if r["entry_pool_upper"] > r["component_upper"] + 1e-15:
                issues.append(f"{r['decision_close_utc']}: entry above component upper")
    return issues


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
            snap = density.collect_at_close(bar)
        except KeyError:
            continue
        snap_at[bar] = snap

    audit_positions = list(csv.DictReader((D47 / "positions_audit.csv").open(encoding="utf-8")))
    comp_rows: list[dict[str, Any]] = []
    sl_rows: list[dict[str, Any]] = []
    unresolved_rows: list[dict[str, Any]] = []

    for row in audit_positions:
        bar = row["decision_close_utc"]
        snap = snap_at.get(bar)
        if not snap:
            continue
        pool_lo = float(row["pool_lower"])
        pool_hi = float(row["pool_upper"])
        comp = component_from_entry_pool(snap, entry_pool_lower=pool_lo, entry_pool_upper=pool_hi)
        comp_rows.append(
            {
                "position_id": row["position_id"],
                "decision_close_utc": bar,
                "entry_pool_lower": pool_lo,
                "entry_pool_upper": pool_hi,
                **{k: comp.get(k) for k in comp if k not in ("member_pool_keys",)},
                "member_pool_keys_pipe": "|".join(comp.get("member_pool_keys") or []),
                "origin_ids_pipe": "|".join(comp.get("unique_origin_ids") or []),
            }
        )
        sl_row = _audit_row(
            decision_close_utc=bar,
            pool_lo=pool_lo,
            pool_hi=pool_hi,
            snap=snap,
            sl_pct=sl_pct,
            legacy_sl=float(row["frozen_sl"]),
            extra={"position_id": row["position_id"], "fill_bar_utc": row["entry_bar_utc"]},
        )
        sl_rows.append(sl_row)
        if comp.get("status") == "UNRESOLVED_PRICE_LEVEL_EDGE":
            unresolved_rows.append({**sl_row, "articulation": "|".join(comp.get("articulation_pool_keys") or [])})

    ref_doc: dict[str, Any] = {"contract_version": "bt_d52_reference_1225_1235_v1", "cases": []}
    for case in REF_CASES:
        aud = next(r for r in audit_positions if r["position_id"] == case["position_id"])
        bar = case["decision_close_utc"]
        snap = snap_at[bar]
        pool_lo = float(aud["pool_lower"])
        pool_hi = float(aud["pool_upper"])
        comp = component_from_entry_pool(snap, entry_pool_lower=pool_lo, entry_pool_upper=pool_hi)
        sl_doc = composite_sl_from_component(comp, sl_pct=sl_pct, short_sl_fn=short_sl)
        ref_doc["cases"].append(
            {
                **case,
                "event_note": "fill_bar is simulated fill/exit; pool selected at decision_close",
                "selected_atomic_bounds": {"lower": pool_lo, "upper": pool_hi},
                "origin_set_key": aud["origin_set_key"],
                "legacy_frozen_sl": float(aud["frozen_sl"]),
                "legacy_atomic_sl_formula": f"entry_pool_upper * (1 + {sl_pct}/100)",
                "price_level_component": comp,
                "composite_sl": sl_doc,
                "user_visual_reference": {
                    "upper": USER_REF_UPPER,
                    "sl": USER_REF_SL,
                    "note": "visual only; not forced onto unrelated entry pools",
                },
                "comparison_to_user_reference": {
                    "same_price_level_as_user_zone": pool_lo >= 0.08410 - 1e-9 and pool_hi <= USER_REF_UPPER + 1e-9,
                    "component_upper_vs_user": (
                        (comp.get("component_upper") - USER_REF_UPPER)
                        if comp.get("component_upper") is not None
                        else None
                    ),
                },
            }
        )

    (OUT / "reference_1225_1235.json").write_text(json.dumps(ref_doc, indent=2), encoding="utf-8")

    def write_csv(name: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with (OUT / name).open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()), extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)

    write_csv("price_level_components.csv", comp_rows)
    write_csv("sl_comparison.csv", sl_rows)
    if unresolved_rows:
        write_csv("unresolved_components.csv", unresolved_rows)
    else:
        (OUT / "unresolved_components.csv").write_text(
            "decision_close_utc,position_id,unresolved_reason\n", encoding="utf-8"
        )

    issues = _run_safety_checks(sl_rows)
    summary_lines = [
        "# BT-D52 audit summary\n",
        f"- Positions audited: **{len(sl_rows)}**\n",
        f"- Unresolved price-level components: **{len(unresolved_rows)}**\n",
        f"- SL config `sl_above_upper_top_pct`: **{sl_pct}** (→ multiply upper by 1.002)\n",
        "- Production BT-D46 / dashboard: **unchanged**\n",
    ]
    for case in ref_doc["cases"]:
        c = case["price_level_component"]
        summary_lines.append(
            f"\n## Fill {case['fill_bar_utc']} (decision {case['decision_close_utc']})\n"
        )
        summary_lines.append(
            f"- Atomic pool: **{case['selected_atomic_bounds']['lower']} – "
            f"{case['selected_atomic_bounds']['upper']}**\n"
        )
        summary_lines.append(
            f"- Component: **{c.get('component_lower')} – {c.get('component_upper')}** "
            f"({c.get('member_count')} members) status={c.get('status')}\n"
        )
        summary_lines.append(f"- Legacy SL: **{case['legacy_frozen_sl']}**\n")
        summary_lines.append(
            f"- Candidate composite SL: **{case['composite_sl'].get('candidate_composite_sl')}** "
            f"({case['composite_sl'].get('sl_mode')})\n"
        )
    if issues:
        summary_lines.append("\n## Safety check failures\n")
        for i in issues:
            summary_lines.append(f"- {i}\n")
    else:
        summary_lines.append("\n## Safety checks\n\nAll causal checks passed.\n")

    (OUT / "summary.md").write_text("".join(summary_lines), encoding="utf-8")
    print("Wrote", OUT, "issues", len(issues))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
