#!/usr/bin/env python3
"""L1 V3AE — FIRST_STRONG_UPPER_POOL vs V3AB baseline (frozen drawings, isolated)."""

from __future__ import annotations

import copy
import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3AE = Path(__file__).resolve().parent
PROBE = V3AE.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3Y = PROBE / "support_break_exit_v3y"
V3W = PROBE / "tp_research_v3w"
DASHBOARD = PROBE.parent
TRP = DASHBOARD.parent / "trading_research_platform"
REPO_TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

for p in (V3AE, V3W, V3AB, PROBE, PROBE.parent / "manual_line_touch_probe_v1", DASHBOARD, REPO_TRP, TRP, V3Y):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from config_v3w import TP2Params, TP2_GRID
from v3y_config import CONFIRM_DELTA_PCT

import importlib.util

_v3y_engine = importlib.util.spec_from_file_location("v3y_engine", V3Y / "engine.py")
assert _v3y_engine and _v3y_engine.loader
_v3y_engine_mod = importlib.util.module_from_spec(_v3y_engine)
_v3y_engine.loader.exec_module(_v3y_engine_mod)
execute_variant_v3y = _v3y_engine_mod.execute_variant_v3y

from v3ae_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, VARIANTS, build_context_from_snapshot
from v3ae_tp import apply_first_strong_tp, pending_with_first_strong

OUT = V3AE
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
BASELINE_SCENARIO = "BASELINE_V3AB_TP"
FS_PREFIX = "FIRST_STRONG_UPPER_POOL"


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


def _refresh_effective_tp(pending: list[dict], upper_line, mod) -> None:
    pe.enrich_pending_metrics(pending, upper_line, mod, independent=True)


def _trade_export(tr: dict, *, scenario: str, variant_code: str, pending_row: dict | None) -> dict[str, str]:
    fs = {}
    if pending_row and pending_row.get("_fs_meta"):
        try:
            fs = json.loads(pending_row["_fs_meta"])
        except json.JSONDecodeError:
            fs = {}
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
        "exit_reason": tr.get("exit_kind") or tr.get("exit_reason") or "",
        "pnl_pct": tr.get("gross_pnl_pct") or tr.get("pnl_pct") or "",
        "retrospective": "yes",  # L1 manual-channel pilot (immutable snapshot research)
        "tp_research_model": pending_row.get("tp_research_model", "") if pending_row else "",
        "zone_bottom": f"{fs.get('tp_bottom', fs.get('zone_bottom', ''))}",
        "zone_top": f"{fs.get('zone_top', '')}",
        "zone_depth": str(fs.get("zone_depth", fs.get("zone_depth", ""))),
        "zone_origins": str(fs.get("zone_origins", "")),
        "zone_width_pct": f"{fs.get('zone_width_pct', '')}",
        "dist_entry_pct": f"{fs.get('dist_from_entry_pct', tr.get('tp_target_dist_pct', ''))}",
        "fallback_far_d2": "yes" if fs.get("fallback_far_d2") else "no",
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _pending_by_eid(pending: list[dict]) -> dict[str, dict]:
    return {r["touch_event_id"]: r for r in pending if r.get("touch_event_id")}


def _compare_entries(baseline_ex: list[dict], other_ex: list[dict]) -> dict[str, Any]:
    b = {t["touch_event_id"] for t in baseline_ex}
    o = {t["touch_event_id"] for t in other_ex}
    return {
        "baseline_only": sorted(b - o),
        "other_only": sorted(o - b),
        "shared": sorted(b & o),
        "identical_entry_set": b == o,
    }


def _trade_delta_analysis(
    baseline_rows: list[dict], fs_rows: list[dict]
) -> dict[str, Any]:
    by_id = {r["touch_event_id"]: r for r in baseline_rows}
    capped: list[str] = []
    improved: list[str] = []
    for r in fs_rows:
        eid = r["touch_event_id"]
        b = by_id.get(eid)
        if not b:
            continue
        try:
            p_fs = float(r.get("pnl_pct") or 0)
            p_bl = float(b.get("pnl_pct") or 0)
        except ValueError:
            continue
        if p_fs < p_bl - 1e-9:
            capped.append(eid)
        elif p_fs > p_bl + 1e-9:
            improved.append(eid)
    new_ids = sorted({r["touch_event_id"] for r in fs_rows} - {r["touch_event_id"] for r in baseline_rows})
    dropped = sorted({r["touch_event_id"] for r in baseline_rows} - {r["touch_event_id"] for r in fs_rows})
    return {
        "capped_winners": capped,
        "improved_vs_baseline": improved,
        "new_entries": new_ids,
        "dropped_entries": dropped,
    }


def _build_report(
    manifest: dict,
    stats: dict,
    ref_rows: list[dict],
    trade_rows: list[dict],
) -> str:
    lines = [
        "# L1 V3AE — FIRST_STRONG_UPPER_POOL (isoliert)",
        "",
        f"Run: `{manifest['run_id']}`",
        f"Drawings SHA256: `{manifest['drawings_sha256']}`",
        f"Snapshot: `{manifest['drawings_snapshot']}`",
        f"Supportbruch: unverändert V3Y (Δ {CONFIRM_DELTA_PCT} %)",
        "",
        "## 1. Getestete Schwellenvarianten (V3W TP2-Grid)",
        "",
    ]
    for p in TP2_GRID:
        lines.append(
            f"- `{p.code}` — width≥{p.min_width_pct} %, depth≥{p.min_depth}, origins≥{p.min_origins}"
        )
    lines.append(f"- Baseline: `{BASELINE_SCENARIO}` (`pick_tp` / max D2 wie V3AB)")
    lines.append("")
    lines.append("## 2–5. Kennzahlen je Szenario × Variante A/B/C")
    lines.append("")
    lines.append(
        "| Szenario | Var | Trades | TP | SUPPORT_BREAK | SL | CHANNEL | Σ% brutto | vs BL Σ% |"
    )
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for sc, by_var in stats.items():
        bl_sum = None
        if sc == BASELINE_SCENARIO:
            bl_sum = {c: by_var[c]["gross_pct"] for c in "ABC"}
        for code in "ABC":
            s = by_var[code]
            vs = ""
            if sc != BASELINE_SCENARIO and bl_sum is not None:
                vs = f"{s['gross_pct'] - stats[BASELINE_SCENARIO][code]['gross_pct']:+.4f}"
            elif sc != BASELINE_SCENARIO:
                base = stats.get(BASELINE_SCENARIO, {}).get(code, {}).get("gross_pct", 0)
                vs = f"{s['gross_pct'] - base:+.4f}"
            ec = s["exit_counts"]
            lines.append(
                f"| {sc} | {code} | {s['trades']} | {ec.get('TP', 0)} | "
                f"{ec.get('SUPPORT_BREAK_EXIT', 0)} | {ec.get('SL', 0)} | "
                f"{ec.get('CHANNEL_UPPER', 0)} | {s['gross_pct']:.4f} | {vs} |"
            )
    lines.extend(
        [
            "",
            "## 6. Entry-Parität vs. Baseline",
            "",
        ]
    )
    for sc, by_var in stats.items():
        if sc == BASELINE_SCENARIO:
            continue
        for code in "ABC":
            e = by_var[code].get("entry_parity", {})
            flag = "ja" if e.get("identical_entry_set") else "nein"
            lines.append(f"- {sc} / {code}: identische Entries **{flag}**")
    lines.extend(["", "## 7–9. Trade-Effekte (Variant B)", ""])
    b_rows = [r for r in trade_rows if r["scenario"] == BASELINE_SCENARIO and r["variant"] == "V3E-B"]
    for sc in stats:
        if sc == BASELINE_SCENARIO:
            continue
        fs_rows = [r for r in trade_rows if r["scenario"] == sc and r["variant"] == "V3E-B"]
        da = _trade_delta_analysis(b_rows, fs_rows)
        ec = stats[sc]["B"]["exit_counts"]
        lines.append(f"### {sc}")
        lines.append(f"- TP-Treffer: {ec.get('TP', 0)} · Supportbruch: {ec.get('SUPPORT_BREAK_EXIT', 0)} · SL: {ec.get('SL', 0)}")
        lines.append(f"- Begrenzte Gewinner (gleicher Entry, niedrigeres PnL): {len(da['capped_winners'])}")
        lines.append(f"- Verbesserte Trades vs Baseline (gleicher Entry): {len(da['improved_vs_baseline'])}")
        lines.append(f"- Neue Entry-Slots (nur FS): `{', '.join(da['new_entries']) or '—'}`")
        lines.append(f"- Entfallene vs BL: `{', '.join(da['dropped_entries']) or '—'}`")
        lines.append("")
    lines.extend(
        [
            "",
            "## 10. Referenztrade `te-d-c0791ea0c65e-0003`",
            "",
        ]
    )
    lines.append(
        "Objektive Regel wählt **nicht** die violette Konzentration (~0.08929): die ersten gültigen „strong“-Zonen "
        "liegen bei ~0.08536–0.08604 (V3AD-Audit, Zone #0–#1)."
    )
    lines.append("")
    for r in ref_rows:
        lines.append(
            f"- **{r.get('scenario','')}**: TP **{r.get('tp_price','')}** · Zone **{r.get('zone_bottom','—')}–{r.get('zone_top','—')}** "
            f"(depth {r.get('zone_depth','—')}, origins {r.get('zone_origins','—')}, dist {r.get('dist_entry_pct','')}%) · "
            f"Exit **{r.get('exit_reason','')}** @ {r.get('exit_utc','')} · PnL **{r.get('pnl_pct','')}%** "
            f"(Baseline-V3AB-Ref: SUPPORT_BREAK +2.89 % @ 0.08736)"
        )
    lines.extend(
        [
            "",
            "Vollständige Trades: `l1_v3ae_trades_all.csv`",
            "",
            "**STOP** — keine Übernahme in V3AB/V3Y/Produktion.",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]

    snap = DEFAULT_SNAPSHOT
    if not snap.is_file():
        raise SystemExit(f"missing frozen snapshot: {snap}")

    frozen = build_context_from_snapshot(snap, expected_sha256=FROZEN_SHA256)
    lines = [ln for ln in frozen["ctx"]["lines_by_id"].values()]
    baseline_pending = frozen["pending"]
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["upper_line"]

    scenarios: list[tuple[str, list[dict]]] = [(BASELINE_SCENARIO, copy.deepcopy(baseline_pending))]
    for params in TP2_GRID:
        scenarios.append(
            (f"{FS_PREFIX}/{params.code}", pending_with_first_strong(baseline_pending, lines, l1_module=mod, params=params))
        )

    stats: dict[str, dict[str, Any]] = {}
    trade_rows: list[dict] = []
    ref_eid = "te-d-c0791ea0c65e-0003"
    ref_pending_snap: dict[str, dict] = {}

    for scenario, pending in scenarios:
        _refresh_effective_tp(pending, upper_line, mod)
        stats[scenario] = {}
        pmap = _pending_by_eid(pending)
        if ref_eid in pmap:
            ref_pending_snap[scenario] = pmap[ref_eid]
        for code, var in VARIANTS.items():
            trades = execute_variant_v3y(
                var,
                copy.deepcopy(pending),
                candles,
                upper_line=upper_line,
                lines_by_id=lines_by_id,
                l1_module=mod,
            )
            ex = _executed(trades)
            parity = {}
            if scenario != BASELINE_SCENARIO:
                bl_ex = _executed(
                    stats.get(BASELINE_SCENARIO, {}).get(code, {}).get("_executed_trades", [])
                )
                if not bl_ex:
                    bl_ex = []  # filled below
                parity = _compare_entries(bl_ex, ex) if bl_ex else {}
            stats[scenario][code] = {
                "trades": len(ex),
                "wins": _wins_losses(trades)[0],
                "losses": _wins_losses(trades)[1],
                "gross_pct": _pnl_sum(trades),
                "exit_counts": _exit_counts(trades),
                "entry_parity": parity,
                "_executed_trades": ex,
            }
            for tr in ex:
                trade_rows.append(
                    _trade_export(
                        tr,
                        scenario=scenario,
                        variant_code=var.code,
                        pending_row=pmap.get(tr.get("touch_event_id", "")),
                    )
                )

    # fix parity after baseline stored
    for scenario in stats:
        if scenario == BASELINE_SCENARIO:
            continue
        for code in "ABC":
            bl_ex = stats[BASELINE_SCENARIO][code]["_executed_trades"]
            fs_ex = stats[scenario][code]["_executed_trades"]
            stats[scenario][code]["entry_parity"] = _compare_entries(bl_ex, fs_ex)

    ref_rows = []
    for scenario, prow in ref_pending_snap.items():
        tr = next(
            (
                r
                for r in trade_rows
                if r["touch_event_id"] == ref_eid and r["scenario"] == scenario and r["variant"] == "V3E-B"
            ),
            {},
        )
        ref_rows.append({**tr, "scenario": scenario, **{k: tr.get(k, "") for k in _trade_export(tr or {}, scenario=scenario, variant_code="V3E-B", pending_row=prow)}})

    # cleaner ref build
    ref_rows = []
    for scenario, prow in ref_pending_snap.items():
        tr = next(
            (
                r
                for r in trade_rows
                if r["touch_event_id"] == ref_eid and r["scenario"] == scenario and r["variant"] == "V3E-B"
            ),
            None,
        )
        row = _trade_export(tr or {}, scenario=scenario, variant_code="V3E-B", pending_row=prow)
        ref_rows.append(row)

    manifest = {
        "run_id": RUN_ID,
        "drawings_sha256": frozen["drawings_sha256"],
        "drawings_snapshot": str(snap),
        "frozen_sha256_expected": FROZEN_SHA256,
        "scenarios": [s[0] for s in scenarios],
        "tp2_grid": [
            {"code": p.code, "min_width_pct": p.min_width_pct, "min_depth": p.min_depth, "min_origins": p.min_origins}
            for p in TP2_GRID
        ],
        "confirm_delta_pct": CONFIRM_DELTA_PCT,
        "variant_stats": {
            sc: {c: {k: v for k, v in stats[sc][c].items() if k != "_executed_trades"} for c in "ABC"}
            for sc in stats
        },
        "reference_trade": ref_rows,
    }

    _write_csv(OUT / "l1_v3ae_trades_all.csv", trade_rows)
    (OUT / "l1_v3ae_run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (OUT / "l1_v3ae_reference_trade.json").write_text(json.dumps(ref_rows, indent=2), encoding="utf-8")
    report = _build_report(manifest, stats, ref_rows, trade_rows)
    (OUT / "L1_V3AE_FIRST_STRONG_UPPER_POOL_REPORT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AE_FIRST_STRONG_UPPER_POOL_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
