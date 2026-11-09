#!/usr/bin/env python3
"""L1 V3W — isolated multi-TP research (Research I + II)."""

from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3W = Path(__file__).resolve().parent
PROBE = V3W.parent
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))
if str(V3W) not in sys.path:
    sys.path.insert(0, str(V3W))

import run_l1_v3g_nearness_staggering as v3g
from config_v3w import CONTROL_AUG26, CONTROL_SEP24, OOS_HOLDOUT_START_UTC, TP2_GRID, TP3_VARIANTS
from context import VARIANTS, admitted_rows, build_frozen_context
from simulation import simulate_multi_legs, simulate_standard
from tp_targets import all_models, resolve
from zone_features import enrich_zone_origins, merge_qualified_zones
from pool_d2_metrics import analyze_d2_landscape

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _parse_utc_ts(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())


def _holding_minutes(entry_utc: str, exit_utc: str) -> float | None:
    if not exit_utc:
        return None
    return (_parse_utc_ts(exit_utc) - _parse_utc_ts(entry_utc)) / 60.0


def _simulate_row(row: dict, res) -> Any:
    candles = row["_candles_ref"]
    entry_idx = int(row["_entry_idx"])
    entry = float(row["entry_price"])
    sl = float(row["_sl"])
    if len(res.leg_weights) > 1 or res.meta.get("breakeven_after_leg") is not None:
        return simulate_multi_legs(
            candles,
            entry_idx,
            entry,
            sl,
            res.tp_prices,
            res.leg_weights,
            channel_line=res.channel_line if res.model_id == "TP5_v3u_channel_pool" else None,
            breakeven_after_leg=res.meta.get("breakeven_after_leg"),
        )
    return simulate_standard(
        candles,
        entry_idx,
        entry,
        sl,
        res.pool_tp,
        res.channel_line if res.model_id == "TP5_v3u_channel_pool" else None,
    )


def _trade_row(
    *,
    study: str,
    model_id: str,
    variant: str,
    row: dict,
    res,
    sim,
) -> dict[str, Any]:
    win = sim.gross_pnl_pct > 0
    loss = sim.exit_kind == "SL" or (sim.gross_pnl_pct < 0 and sim.exit_kind != "OPEN_AT_WINDOW_END")
    return {
        "study": study,
        "tp_model": model_id,
        "variant": variant,
        "touch_event_id": row.get("touch_event_id", ""),
        "entry_utc": row.get("entry_utc", ""),
        "entry_price": row.get("entry_price", ""),
        "support_drawing_id": row.get("touch_drawing_id", ""),
        "upper_channel_drawing_id": row.get("tp_channel_line_id", ""),
        "sl_price": row.get("sl_price", ""),
        "planned_tps": "|".join(f"{x:.10f}" for x in res.tp_prices) if res.tp_prices else "",
        "leg_weights": "|".join(str(w) for w in res.leg_weights),
        "pool_decision_close": row.get("_pool_decision_close", ""),
        "drawing_causality": row.get("drawing_causality", ""),
        "resolution_ok": "yes" if res.ok else "no",
        "resolution_reason": res.reason,
        "exit_utc": sim.exit_utc,
        "exit_price": f"{sim.exit_price:.10f}" if sim.exit_price is not None else "",
        "exit_kind": sim.exit_kind,
        "gross_pnl_pct": sim.gross_pnl_pct,
        "mfe_pct": sim.mfe_pct,
        "mae_pct": sim.mae_pct,
        "holding_minutes": _holding_minutes(row.get("entry_utc", ""), sim.exit_utc),
        "ambiguous": "yes" if sim.ambiguous else "no",
        "is_winner": "yes" if win else "no",
        "is_loser": "yes" if loss else "no",
        "counterfactual_independent": "yes" if study == "identical_entries" else "no",
        "partial_tp_legs_filled": len(sim.leg_fills),
    }


def _aggregate(trades: list[dict]) -> dict[str, Any]:
    realized = [t for t in trades if t["resolution_ok"] == "yes" and t["exit_kind"] not in ("NO_TRADE", "")]
    pnls = [float(t["gross_pnl_pct"]) for t in realized]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "candidates": len(trades),
        "executed": len(realized),
        "winners": sum(1 for p in pnls if p > 0),
        "losers": sum(1 for p in pnls if p < 0),
        "hit_rate_pct": round(100.0 * len(wins) / len(pnls), 2) if pnls else 0.0,
        "gross_sum_pct": round(sum(pnls), 4),
        "avg_pct": round(statistics.mean(pnls), 4) if pnls else 0.0,
        "median_pct": round(statistics.median(pnls), 4) if pnls else 0.0,
        "avg_win_pct": round(statistics.mean(wins), 4) if wins else 0.0,
        "avg_loss_pct": round(statistics.mean(losses), 4) if losses else 0.0,
        "max_loss_pct": round(min(pnls), 4) if pnls else 0.0,
        "open_end": sum(1 for t in realized if t["exit_kind"] == "OPEN_AT_WINDOW_END"),
        "partial_tp_trades": sum(1 for t in realized if int(t.get("partial_tp_legs_filled") or 0) > 1),
    }


def research_identical(pending: list[dict], candles: list[dict], models: list[str]) -> list[dict]:
    rows: list[dict] = []
    for code in "ABC":
        for row in admitted_rows(pending, code):
            row = dict(row)
            row["_candles_ref"] = candles
            for mid in models:
                res = resolve(mid, row)
                if not res.ok:
                    sim = type("X", (), {"gross_pnl_pct": 0.0, "exit_kind": "NO_TRADE", "exit_utc": "", "exit_price": None, "mfe_pct": 0.0, "mae_pct": 0.0, "ambiguous": False, "leg_fills": []})()
                    rows.append(_trade_row(study="identical_entries", model_id=mid, variant=code, row=row, res=res, sim=sim))
                    continue
                sim = _simulate_row(row, res)
                rows.append(_trade_row(study="identical_entries", model_id=mid, variant=code, row=row, res=res, sim=sim))
    return rows


def research_chronological(pending: list[dict], candles: list[dict], models: list[str]) -> list[dict]:
    import pilot_engine as pe

    rows: list[dict] = []
    for code in "ABC":
        variant = VARIANTS[code]
        for mid in models:
            position_free = 0
            for row in sorted(
                admitted_rows(pending, code),
                key=lambda r: int(r["_entry_idx"]),
            ):
                if int(row["_entry_idx"]) < position_free:
                    continue
                row2 = dict(row)
                row2["_candles_ref"] = candles
                res = resolve(mid, row2)
                if not res.ok:
                    continue
                sim = _simulate_row(row2, res)
                if sim.exit_idx is None:
                    continue
                rows.append(_trade_row(study="chronological", model_id=mid, variant=code, row=row2, res=res, sim=sim))
                if sim.exit_kind == "OPEN_AT_WINDOW_END":
                    position_free = len(candles)
                else:
                    position_free = sim.exit_idx + 1
    return rows


def export_zone_csv(pending: list[dict], path: Path) -> None:
    out_rows = []
    for row in pending:
        if not row.get("entry_utc"):
            continue
        members = row.get("_pool_members")
        dc_s = row.get("_pool_decision_close") or ""
        if not members or not dc_s:
            continue
        dc = datetime.strptime(dc_s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        entry = float(row["entry_price"])
        _m, _b, qualified = analyze_d2_landscape(members, dc, entry, apply_provisional_research_filter=False)
        zones = merge_qualified_zones(qualified, entry, merge_gap_pct=0.15)
        enrich_zone_origins(zones, members, dc, entry)
        for i, z in enumerate(zones):
            out_rows.append(
                {
                    "touch_event_id": row.get("touch_event_id", ""),
                    "entry_utc": row.get("entry_utc", ""),
                    "support_drawing_id": row.get("touch_drawing_id", ""),
                    "pool_decision_close": dc_s,
                    "zone_index": i,
                    "zone_bottom": f"{z.bottom:.10f}",
                    "zone_top": f"{z.top:.10f}",
                    "zone_width_pct": round(z.width_pct, 6),
                    "zone_max_depth": z.max_depth,
                    "zone_band_count": z.band_count,
                    "zone_unique_origins": z.unique_origins,
                    "dist_from_entry_pct": round(z.dist_from_entry_pct, 6),
                    "drawing_causality": row.get("drawing_causality", ""),
                }
            )
    if not out_rows:
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)


def main() -> int:
    bundle = build_frozen_context()
    pending = bundle["pending"]
    candles = bundle["ctx"]["candles"]
    for row in pending:
        row["_candles_ref"] = candles

    models = all_models()
    identical = research_identical(pending, candles, models)
    chronological = research_chronological(pending, candles, models)
    all_trades = identical + chronological

    def write_csv(name: str, rows: list[dict]) -> None:
        if not rows:
            return
        p = V3W / name
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
            w.writeheader()
            w.writerows(rows)

    write_csv("v3w_identical_entries_results.csv", identical)
    write_csv("v3w_chronological_results.csv", chronological)
    write_csv("v3w_all_trades.csv", all_trades)
    write_csv("v3w_winning_trades.csv", [t for t in all_trades if t.get("is_winner") == "yes"])
    write_csv("v3w_losing_trades.csv", [t for t in all_trades if t.get("is_loser") == "yes"])

    leg_rows: list[dict] = []

    export_zone_csv(pending, V3W / "v3w_upper_d2_zones.csv")

    # comparison tables
    cmp_ident: list[dict] = []
    cmp_chron: list[dict] = []
    monthly: dict[tuple[str, str, str], list[float]] = defaultdict(list)

    for study, bucket, dest in (
        ("identical_entries", identical, cmp_ident),
        ("chronological", chronological, cmp_chron),
    ):
        for code in "ABC":
            for mid in models:
                sub = [t for t in bucket if t["study"] == study and t["variant"] == code and t["tp_model"] == mid]
                agg = _aggregate(sub)
                dest.append({"study": study, "variant": code, "tp_model": mid, **agg})
                for t in sub:
                    if t["resolution_ok"] == "yes" and t.get("entry_utc"):
                        mo = t["entry_utc"][:7]
                        monthly[(study, code, mid, mo)].append(float(t["gross_pnl_pct"]))

    write_csv("v3w_tp_model_comparison.csv", cmp_ident + cmp_chron)

    monthly_rows = []
    for key, pnls in sorted(monthly.items()):
        study, code, mid, mo = key
        monthly_rows.append(
            {
                "study": study,
                "variant": code,
                "tp_model": mid,
                "month": mo,
                "trades": len(pnls),
                "gross_sum_pct": round(sum(pnls), 4),
            }
        )
    write_csv("v3w_monthly_statistics.csv", monthly_rows)

    # manifest + report
    unique_entries = len(
        {(r["touch_event_id"], r["entry_utc"]) for r in pending if r.get("entry_utc")}
    )
    admitted_a = len(admitted_rows(pending, "A"))

    manifest = {
        "run_id": RUN_ID,
        "drawings_sha256": bundle["drawings_sha256"],
        "drawings_snapshot": bundle["drawings_snapshot"],
        "window_mode": bundle["window_mode"],
        "models_tested": models,
        "tp2_grid": [p.__dict__ for p in TP2_GRID],
        "tp3_variants": list(TP3_VARIANTS),
        "unique_entry_events": unique_entries,
        "admitted_variant_A": admitted_a,
        "oos_status": "EXPLORATORY_OOS_NOT_VALIDATED",
        "oos_holdout_start_utc": OOS_HOLDOUT_START_UTC,
        "dyn_research": "NOT_RUN_IN_V3W_MVP",
        "method_limitation": "RETROSPECTIVE_DRAWING",
    }
    (V3W / "v3w_run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # control cases
    controls = {}
    for eu in (CONTROL_AUG26, CONTROL_SEP24):
        for t in identical:
            if t["entry_utc"] == eu and t["variant"] == "A":
                controls.setdefault(eu, {})[t["tp_model"]] = t["gross_pnl_pct"]

    best_gross = max(cmp_ident, key=lambda x: x["gross_sum_pct"]) if cmp_ident else {}
    best_avg = max(cmp_ident, key=lambda x: x["avg_pct"]) if cmp_ident else {}

    lines = [
        "# V3W TP Research Report",
        "",
        f"Run: `{RUN_ID}` · Drawings SHA `{bundle['drawings_sha256'][:16]}…`",
        "",
        "## Scope",
        f"- Unique entry events (with C1): **{unique_entries}**",
        f"- Variant A ADMIT candidates: **{admitted_a}**",
        f"- Models: **{len(models)}** (TP1–TP5 grid, multi M1–M5)",
        "- Research I: identical entries (counterfactual, no one-long)",
        "- Research II: chronological (one open long)",
        "- DYN1/DYN2: **not run** in this pass (`dyn_research` in manifest)",
        "- OOS: **EXPLORATORY / NOT VALIDATED**",
        "",
        "## Overview A — Identical entries (variant A summary)",
        "",
        "| TP model | executed | winners | losers | gross % | avg % | median % |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted([r for r in cmp_ident if r["variant"] == "A"], key=lambda x: -x["gross_sum_pct"]):
        lines.append(
            f"| {row['tp_model']} | {row['executed']} | {row['winners']} | {row['losers']} | "
            f"{row['gross_sum_pct']} | {row['avg_pct']} | {row['median_pct']} |"
        )
    lines.extend(
        [
            "",
            "## Overview B — Chronological (variant A)",
            "",
            "| TP model | executed | winners | losers | gross % | avg % | hit % |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in sorted([r for r in cmp_chron if r["variant"] == "A"], key=lambda x: -x["gross_sum_pct"]):
        lines.append(
            f"| {row['tp_model']} | {row['executed']} | {row['winners']} | {row['losers']} | "
            f"{row['gross_sum_pct']} | {row['avg_pct']} | {row['hit_rate_pct']} |"
        )
    lines.append("\n## Control trade 2026-08-26 (variant A, identical entries)\n")
    for mid, pnl in sorted((controls.get(CONTROL_AUG26) or {}).items()):
        lines.append(f"- {mid}: **{pnl}%**")
    lines.append("\n## Control trade 2026-09-24 (variant A)\n")
    for mid, pnl in sorted((controls.get(CONTROL_SEP24) or {}).items()):
        lines.append(f"- {mid}: **{pnl}%**")
    lines.append(
        f"\nBest gross (identical, all variants): `{best_gross.get('tp_model')}` variant `{best_gross.get('variant')}` = {best_gross.get('gross_sum_pct')}%"
    )
    lines.append(
        f"Best avg trade (identical): `{best_avg.get('tp_model')}` variant `{best_avg.get('variant')}` = {best_avg.get('avg_pct')}%"
    )
    (V3W / "V3W_TP_RESEARCH_REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(
        json.dumps(
            {
                "admitted_A": admitted_a,
                "unique_entries": unique_entries,
                "models": len(models),
                "best_gross_A": [r for r in cmp_ident if r["variant"] == "A"][:3],
                "controls": controls,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
