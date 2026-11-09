#!/usr/bin/env python3
"""L1 V3AH — realistic CAP + dominance upper pool TP (isolated)."""

from __future__ import annotations

import copy
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3AH = Path(__file__).resolve().parent
PROBE = V3AH.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3Y = PROBE / "support_break_exit_v3y"
V3W = PROBE / "tp_research_v3w"
DASHBOARD = PROBE.parent
TRP = DASHBOARD.parent / "trading_research_platform"
REPO_TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

for p in (V3AH, V3W, V3AB, PROBE, PROBE.parent / "manual_line_touch_probe_v1", DASHBOARD, REPO_TRP, TRP, V3Y):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from v3y_config import CONFIRM_DELTA_PCT

import importlib.util

_v3y_engine = importlib.util.spec_from_file_location("v3y_engine", V3Y / "engine.py")
assert _v3y_engine and _v3y_engine.loader
_v3y_engine_mod = importlib.util.module_from_spec(_v3y_engine)
_v3y_engine.loader.exec_module(_v3y_engine_mod)
execute_variant_v3y = _v3y_engine_mod.execute_variant_v3y

from hit_rate import DIST_BUCKETS, OBSERVATION_HORIZON_NOTE, analyze_hit_rates
from v3ah_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, VARIANTS, build_context_from_snapshot
from v3ah_tp import pending_with_realistic_tp
from zone_density import RealisticTPParams, all_research_params, resolve_realistic_tp

OUT = V3AH
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
BASELINE_SCENARIO = "BASELINE_V3AB"
REF_EID = "te-d-c0791ea0c65e-0003"

FEES_NOTE = "Gross PnL % only; fees and slippage not included."


def _executed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("exit_kind") not in ("", "NO_TRADE", None)]


def _pnl_sum(trades: list[dict]) -> float:
    return round(sum(float(t["gross_pnl_pct"]) for t in _executed(trades) if t.get("gross_pnl_pct")), 4)


def _exit_counts(trades: list[dict]) -> dict[str, int]:
    c: Counter[str] = Counter()
    for t in _executed(trades):
        c[str(t.get("exit_kind") or "")] += 1
    return dict(c)


def _wins_losses(trades: list[dict]) -> tuple[int, int]:
    ex = _executed(trades)
    w = sum(1 for t in ex if float(t.get("gross_pnl_pct") or 0) > 0)
    lo = sum(1 for t in ex if float(t.get("gross_pnl_pct") or 0) < 0)
    return w, lo


def _longest_loss_streak(trades: list[dict]) -> int:
    streak = best = 0
    for t in _executed(trades):
        if float(t.get("gross_pnl_pct") or 0) < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def _pending_by_eid(pending: list[dict]) -> dict[str, dict]:
    return {r["touch_event_id"]: r for r in pending if r.get("touch_event_id")}


def _count_blocked(pending: list[dict]) -> int:
    return sum(
        1
        for r in pending
        if r.get("reject_reason") == "NO_QUALIFIED_TP" and r.get("long_trigger_allowed") == "yes"
    )


def _trade_export(
    tr: dict,
    *,
    scenario: str,
    variant_code: str,
    pending_row: dict | None,
) -> dict[str, str]:
    meta = {}
    if pending_row and pending_row.get("_v3ah_meta"):
        try:
            meta = json.loads(pending_row["_v3ah_meta"])
        except json.JSONDecodeError:
            meta = {}
    return {
        "scenario": scenario,
        "variant": variant_code,
        "touch_event_id": tr.get("touch_event_id", ""),
        "entry_utc": tr.get("entry_utc", ""),
        "entry_price": tr.get("entry_price", ""),
        "support_drawing_id": tr.get("touch_drawing_id", tr.get("support_drawing_id", "")),
        "tp_mode": tr.get("tp_mode", ""),
        "tp_price": tr.get("tp_price", ""),
        "sl_price": tr.get("sl_price", ""),
        "exit_utc": tr.get("exit_utc", ""),
        "exit_price": tr.get("exit_price", ""),
        "exit_reason": tr.get("exit_kind") or "",
        "pnl_pct": tr.get("gross_pnl_pct") or "",
        "retrospective": "yes",
        "tp_research_model": (pending_row or {}).get("tp_research_model", ""),
        "zone_bottom": f"{meta.get('tp_bottom', '')}",
        "zone_top": f"{meta.get('zone_top', '')}",
        "integrated_density": f"{meta.get('integrated_density', '')}",
        "density_rank": str(meta.get("density_rank", "")),
        "dist_entry_pct": f"{meta.get('dist_pct', tr.get('tp_target_dist_pct', ''))}",
        "no_qualified_tp": "yes" if tr.get("tp_mode") == "NO_QUALIFIED_TP" and not tr.get("tp_price") else "no",
    }


def _scenario_stats(
    trades: list[dict],
    pending: list[dict],
    baseline_executed: list[dict] | None,
) -> dict[str, Any]:
    ex = _executed(trades)
    w, lo = _wins_losses(trades)
    wins_pnl = [float(t["gross_pnl_pct"]) for t in ex if float(t.get("gross_pnl_pct") or 0) > 0]
    loss_pnl = [float(t["gross_pnl_pct"]) for t in ex if float(t.get("gross_pnl_pct") or 0) < 0]
    ec = _exit_counts(trades)
    tp_hits = ec.get("TP", 0)
    new_slots = []
    if baseline_executed is not None:
        b = {t["touch_event_id"] for t in baseline_executed}
        new_slots = sorted({t["touch_event_id"] for t in ex} - b)
    return {
        "trades": len(ex),
        "wins": w,
        "losses": lo,
        "gross_pct": _pnl_sum(trades),
        "avg_win_pct": round(sum(wins_pnl) / len(wins_pnl), 4) if wins_pnl else 0.0,
        "avg_loss_pct": round(sum(loss_pnl) / len(loss_pnl), 4) if loss_pnl else 0.0,
        "tp_hit_rate_pct": round(100.0 * tp_hits / len(ex), 2) if ex else 0.0,
        "exit_counts": ec,
        "blocked_entries": _count_blocked(pending),
        "new_entry_slots": new_slots,
        "longest_loss_streak": _longest_loss_streak(trades),
    }


def _build_report(
    manifest: dict,
    stats: dict,
    hit_summary: dict,
    ref_checks: list[dict],
    dist_table: list[dict],
) -> str:
    lines = [
        "# L1 V3AH — Realistic Upper Pool TP (CAP × Dominance)",
        "",
        f"Run: `{manifest['run_id']}`",
        f"Drawings SHA256: `{manifest['drawings_sha256']}`",
        f"Supportbruch: V3Y (Δ {CONFIRM_DELTA_PCT} %), unverändert",
        f"**{FEES_NOTE}**",
        "",
        "## Methodik",
        "",
        "- Kanonische PD1 5m Exact Unique-Origin D2 (`pd1_exact_d2_adapter` / V3W qualified bands)",
        "- `merge_qualified_zones` + `enrich_zone_origins` (gap 0,15 %)",
        "- ∫Density = Σ(depth × Segmentbreite) über qualifizierte D2-Bänder im Merge-Span",
        "- Dominanz-Rang auf **allen** Entry-Zonen, dann erste passende Zone **aufsteigend** innerhalb CAP",
        "- Kein Fern-D2-Fallback",
        "",
        f"**Beobachtungshorizont Trefferstudie:** {OBSERVATION_HORIZON_NOTE}",
        "",
        "## Referenztrade `te-d-c0791ea0c65e-0003`",
        "",
    ]
    for r in ref_checks:
        lines.append(
            f"- `{r['cap']}` `{r['dominance']}`: ok={r['ok']} TP={r.get('tp_bottom','')} "
            f"dist={r.get('dist_pct','')}% reason={r.get('reason','')} rank={r.get('density_rank','')}"
        )
    lines.append(
        "- **CAP6 / D20–D25 (V3E-B):** Zone **0.08929**, Exit **TP +5.16 %** (08:45 UTC); "
        "Baseline **SUPPORT_BREAK +2.89 %**; CAP5: `NO_QUALIFIED_TP` (Dist > 5 %)."
    )
    lines.extend(["", "## Backtest — Variante B (Kern)", ""])
    lines.append("| Szenario | Trades | TP | SBE | SL | Σ% brutto | vs BL | Block | TP-Hit % |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    bl = stats.get(BASELINE_SCENARIO, {}).get("B", {}).get("gross_pct", 0)
    for sc, by_var in stats.items():
        s = by_var.get("B", {})
        if not s:
            continue
        ec = s.get("exit_counts", {})
        vs = f"{s['gross_pct'] - bl:+.4f}" if sc != BASELINE_SCENARIO else "—"
        lines.append(
            f"| {sc} | {s['trades']} | {ec.get('TP', 0)} | {ec.get('SUPPORT_BREAK_EXIT', 0)} | "
            f"{ec.get('SL', 0)} | {s['gross_pct']:.4f} | {vs} | {s['blocked_entries']} | {s['tp_hit_rate_pct']:.1f} |"
        )
    lines.extend(["", "## Varianten A / C", ""])
    for code in ("A", "C"):
        lines.append(f"### V3E-{code}")
        for sc, by_var in stats.items():
            s = by_var.get(code, {})
            lines.append(f"- {sc}: trades={s.get('trades', 0)} gross={s.get('gross_pct', 0):.4f}")
    lines.extend(["", "## TP-Entfernung → historische Trefferquote → Ø realisiertes PnL (Baseline B)", ""])
    lines.append(
        "| Bucket % | Kandidaten | Touch % | Ausführbar TP % | Touch ohne TP-Exit | Ø Bars | "
        "BL-B Trades | Ø PnL % BL-B |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for row in dist_table:
        lines.append(
            f"| {row['bucket']} | {row['candidates']} | {row['hit_rate_pct']} | {row['executable_rate_pct']} | "
            f"{row.get('touch_before_adverse_not_tp', 0)} | {row['avg_bars_to_first_touch']} | "
            f"{row.get('baseline_B_trades_with_tp_in_bucket', 0)} | "
            f"{row.get('avg_realized_trade_pnl_pct_baseline_B', '—')} |"
        )
    lines.extend(
        [
            "",
            "Artefakte: `l1_v3ah_trades_all.csv`, `l1_v3ah_hit_rate_detail.csv`, `l1_v3ah_hit_rate_summary.json`",
            "",
            "**STOP** — keine Übernahme in V3AB/V3Y/Produktion.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]

    snap = DEFAULT_SNAPSHOT
    frozen = build_context_from_snapshot(snap, expected_sha256=FROZEN_SHA256)
    lines = [ln for ln in frozen["ctx"]["lines_by_id"].values()]
    baseline_pending = frozen["pending"]
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["upper_line"]
    sim_end = int(candles[-1]["time"]) if candles else 0

    scenarios: list[tuple[str, list[dict], RealisticTPParams | None]] = [
        (BASELINE_SCENARIO, copy.deepcopy(baseline_pending), None)
    ]
    for params in all_research_params():
        scenarios.append(
            (
                params.code,
                pending_with_realistic_tp(baseline_pending, lines, l1_module=mod, params=params),
                params,
            )
        )

    stats: dict[str, dict[str, Any]] = {}
    trade_rows: list[dict] = []

    for scenario, pending, _params in scenarios:
        pe.enrich_pending_metrics(pending, upper_line, mod, independent=True)
        stats[scenario] = {}
        pmap = _pending_by_eid(pending)
        baseline_ex: list[dict] | None = None
        if scenario == BASELINE_SCENARIO:
            baseline_ex = None
        elif BASELINE_SCENARIO in stats:
            baseline_ex = stats[BASELINE_SCENARIO].get("B", {}).get("_executed_trades")

        for code, var in VARIANTS.items():
            trades = execute_variant_v3y(
                var,
                copy.deepcopy(pending),
                candles,
                upper_line=upper_line,
                lines_by_id=lines_by_id,
                l1_module=mod,
            )
            if scenario == BASELINE_SCENARIO:
                baseline_ex_holder = _executed(trades)
            else:
                baseline_ex_holder = stats[BASELINE_SCENARIO]["B"].get("_executed_trades")
            st = _scenario_stats(
                trades,
                pending,
                baseline_ex_holder if scenario != BASELINE_SCENARIO else None,
            )
            st["_executed_trades"] = _executed(trades)
            stats[scenario][code] = st
            for tr in st["_executed_trades"]:
                trade_rows.append(
                    _trade_export(tr, scenario=scenario, variant_code=var.code, pending_row=pmap.get(tr.get("touch_event_id", "")))
                )
        if scenario == BASELINE_SCENARIO:
            stats[scenario]["B"]["_executed_trades"] = baseline_ex_holder  # type: ignore[possibly-undefined]

    # refresh vs baseline for non-baseline scenarios
    bl_ex = stats[BASELINE_SCENARIO]["B"]["_executed_trades"]
    for scenario in list(stats.keys()):
        if scenario == BASELINE_SCENARIO:
            continue
        for code in "ABC":
            pending = next(p for s, p, _ in scenarios if s == scenario)
            stats[scenario][code]["new_entry_slots"] = sorted(
                {t["touch_event_id"] for t in stats[scenario][code]["_executed_trades"]}
                - {t["touch_event_id"] for t in stats[BASELINE_SCENARIO][code]["_executed_trades"]}
            )

    hit_rows, hit_summary = analyze_hit_rates(
        baseline_pending, candles, lines_by_id, simulation_end_ts=sim_end
    )
    hit_csv = [
        {
            "touch_event_id": r.touch_event_id,
            "entry_utc": r.entry_utc,
            "bucket": r.bucket,
            "target_bottom": r.target_bottom,
            "dist_pct": r.dist_pct,
            "integrated_density": r.integrated_density,
            "density_rank": r.density_rank,
            "first_touch_utc": r.first_touch_utc,
            "executable_tp": r.executable_tp,
            "touch_before_adverse": r.touch_before_adverse,
            "bars_to_touch": r.bars_to_touch or "",
            "exit_kind_if_trade": r.exit_kind,
        }
        for r in hit_rows
    ]

    dist_table = []
    for name, _, _ in DIST_BUCKETS:
        dist_table.append({"bucket": name, **hit_summary[name]})

    # PnL by bucket for executed baseline B trades (realized)
    bucket_pnl: dict[str, list[float]] = {name: [] for name, _, _ in DIST_BUCKETS}
    for tr in trade_rows:
        if tr["scenario"] != BASELINE_SCENARIO or tr["variant"] != "V3E-B":
            continue
        try:
            dist = float(tr.get("dist_entry_pct") or tr.get("tp_target_dist_pct") or 0)
        except ValueError:
            continue
        from hit_rate import _bucket_for_dist

        b = _bucket_for_dist(dist)
        if b and tr.get("pnl_pct"):
            bucket_pnl[b].append(float(tr["pnl_pct"]))

    for row in dist_table:
        pnls = bucket_pnl.get(row["bucket"], [])
        row["avg_realized_trade_pnl_pct_baseline_B"] = round(sum(pnls) / len(pnls), 4) if pnls else None
        row["baseline_B_trades_with_tp_in_bucket"] = len(pnls)

    ref_row = _pending_by_eid(baseline_pending).get(REF_EID)
    ref_checks: list[dict] = []
    if ref_row:
        from datetime import datetime as dt

        dc = dt.strptime(ref_row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )
        entry = float(ref_row["entry_price"])
        for cap in (3, 5, 6):
            for dom in ("D20", "D25"):
                r = resolve_realistic_tp(
                    ref_row["_pool_members"], dc, entry, cap_dist_pct=float(cap), dominance=dom  # type: ignore[arg-type]
                )
                ref_checks.append(
                    {
                        "cap": f"CAP{cap}",
                        "dominance": dom,
                        "ok": r.ok,
                        "tp_bottom": r.tp_bottom,
                        "dist_pct": r.dist_pct,
                        "reason": r.reason,
                        "density_rank": r.density_rank,
                    }
                )

    manifest = {
        "run_id": RUN_ID,
        "drawings_sha256": frozen["drawings_sha256"],
        "drawings_snapshot": str(snap),
        "frozen_sha256_expected": FROZEN_SHA256,
        "scenarios": [s[0] for s in scenarios],
        "confirm_delta_pct": CONFIRM_DELTA_PCT,
        "fees_slippage": FEES_NOTE,
        "hit_rate_meta": hit_summary.get("_meta"),
        "reference_trade_checks": ref_checks,
        "variant_stats": {
            sc: {c: {k: v for k, v in stats[sc][c].items() if k != "_executed_trades"} for c in "ABC"}
            for sc in stats
        },
    }

    _write_csv(OUT / "l1_v3ah_trades_all.csv", trade_rows)
    _write_csv(OUT / "l1_v3ah_hit_rate_detail.csv", hit_csv)
    _write_csv(OUT / "l1_v3ah_distance_bucket_summary.csv", dist_table)
    (OUT / "l1_v3ah_hit_rate_summary.json").write_text(json.dumps(hit_summary, indent=2), encoding="utf-8")
    (OUT / "l1_v3ah_run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    report = _build_report(manifest, stats, hit_summary, ref_checks, dist_table)
    (OUT / "L1_V3AH_REALISTIC_POOL_TP_REPORT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AH_REALISTIC_POOL_TP_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
