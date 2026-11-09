#!/usr/bin/env python3
"""L1 V3AB — V3Y backtest on current canonical drawings (isolated)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3AB = Path(__file__).resolve().parent
PROBE = V3AB.parent
V3Y = PROBE / "support_break_exit_v3y"
DASHBOARD = PROBE.parent
TRP = DASHBOARD.parent / "trading_research_platform"
if not TRP.is_dir():
    TRP = Path.home() / "projects" / "trading_research_platform"
REPO_TRP = Path("/home/telgenbuescher/projects/trading_research_platform")

for p in (
    V3AB,
    PROBE,
    PROBE.parent / "manual_line_touch_probe_v1",
    DASHBOARD,
    REPO_TRP,
    TRP,
):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
if str(V3Y) not in sys.path:
    sys.path.insert(0, str(V3Y))

import pilot_engine as pe
import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g
from drawing_refresh import CANONICAL_DRAWINGS_PATH, refresh_drawings_for_run
from drawings.manager import DrawingManager
from drawings.persistence import load_drawings
from research_charts.drawing_health import compute_drawing_health
from v3y_config import CONFIRM_DELTA_PCT, DRAWINGS_SHA256 as V3Y_SNAPSHOT_SHA, EXIT_KIND

from v3ab_context import VARIANTS, build_context_from_snapshot

import importlib.util

_v3y_engine = importlib.util.spec_from_file_location("v3y_engine", V3Y / "engine.py")
assert _v3y_engine and _v3y_engine.loader
_v3y_engine_mod = importlib.util.module_from_spec(_v3y_engine)
_v3y_engine.loader.exec_module(_v3y_engine_mod)
execute_variant_v3y = _v3y_engine_mod.execute_variant_v3y

OUT = V3AB
V3Y_SNAPSHOT = PROBE / "l1_v3u_drawings_snapshot_d05d218646aced84.json"
V3Y_MANIFEST = V3Y / "l1_v3y_run_manifest.json"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _executed(trades: list[dict]) -> list[dict]:
    return v3g._executed(trades)


def _pnl_sum(trades: list[dict]) -> float:
    return round(sum(float(t["gross_pnl_pct"]) for t in _executed(trades) if t.get("gross_pnl_pct")), 4)


def _longest_loss_streak(trades: list[dict]) -> int:
    streak = best = 0
    for t in _executed(trades):
        if float(t.get("gross_pnl_pct") or 0) < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


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


def _trade_row(tr: dict, *, scenario: str, variant_code: str) -> dict[str, str]:
    retro = tr.get("drawing_causality") == "RETROSPECTIVE_DRAWING"
    return {
        "scenario": scenario,
        "variant": variant_code,
        "touch_event_id": tr.get("touch_event_id", ""),
        "entry_utc": tr.get("entry_utc", ""),
        "entry_price": tr.get("entry_price", ""),
        "support_drawing_id": tr.get("touch_drawing_id", tr.get("support_drawing_id", "")),
        "tp_price": tr.get("tp_price", ""),
        "sl_price": tr.get("sl_price", ""),
        "exit_utc": tr.get("exit_utc", ""),
        "exit_price": tr.get("exit_price", ""),
        "exit_reason": tr.get("exit_kind", ""),
        "pnl_pct": tr.get("gross_pnl_pct", ""),
        "retrospective": "yes" if retro else "no",
        "tp_mode": tr.get("tp_mode", ""),
    }


def _analyze_new_entries(baseline: list[dict], v3ab_y: list[dict]) -> list[str]:
    b = {t["touch_event_id"] for t in _executed(baseline)}
    y = {t["touch_event_id"] for t in _executed(v3ab_y)}
    return sorted(y - b)


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _drawing_ids_from_snapshot(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return sorted(d.get("drawing_id") for d in data.get("drawings") or [] if d.get("drawing_id"))


def _load_v3y_stats() -> dict[str, Any]:
    if not V3Y_MANIFEST.is_file():
        return {}
    return json.loads(V3Y_MANIFEST.read_text(encoding="utf-8"))


def assert_no_drift(canonical: Path) -> dict[str, Any]:
    mgr = DrawingManager()
    for d in load_drawings(canonical):
        mgr.add_drawing(d)
    health = compute_drawing_health(mgr, canonical)
    if health.get("drift"):
        raise SystemExit(
            "STOP: drawing drift=true (RAM reload vs file). "
            f"ram_only={health.get('ids_only_in_ram')} file_only={health.get('ids_only_in_file')} "
            f"diffs={len(health.get('ids_with_content_diff') or [])}"
        )
    return health


def main() -> int:
    l1._bootstrap_import_paths()

    canonical = CANONICAL_DRAWINGS_PATH
    if not canonical.is_file():
        raise SystemExit(f"canonical drawings missing: {canonical}")

    health = assert_no_drift(canonical)
    refresh = refresh_drawings_for_run(OUT, run_id=RUN_ID, prefix="l1_v3ab")
    snapshot_path = refresh.snapshot_path
    sha = refresh.sha256

    frozen = build_context_from_snapshot(snapshot_path, expected_sha256=sha)
    pending = frozen["pending"]
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["upper_line"]
    mod = sys.modules[l1.__name__]

    baseline: dict[str, list[dict]] = {}
    v3ab_y: dict[str, list[dict]] = {}
    stats: dict[str, Any] = {}

    for code, var in VARIANTS.items():
        baseline[code] = pe.execute_variant(
            var, __import__("copy").deepcopy(pending), candles, upper_line=upper_line, l1_module=mod
        )
        v3ab_y[code] = execute_variant_v3y(
            var,
            __import__("copy").deepcopy(pending),
            candles,
            upper_line=upper_line,
            lines_by_id=lines_by_id,
            l1_module=mod,
        )
        w_b, l_b = _wins_losses(baseline[code])
        w_y, l_y = _wins_losses(v3ab_y[code])
        ex_b = _executed(baseline[code])
        ex_y = _executed(v3ab_y[code])
        stats[code] = {
            "trades_baseline": len(ex_b),
            "trades_v3ab_y": len(ex_y),
            "wins_baseline": w_b,
            "wins_v3ab_y": w_y,
            "losses_baseline": l_b,
            "losses_v3ab_y": l_y,
            "gross_baseline_pct": _pnl_sum(baseline[code]),
            "gross_v3ab_y_pct": _pnl_sum(v3ab_y[code]),
            "avg_baseline_pct": round(_pnl_sum(baseline[code]) / len(ex_b), 4) if ex_b else 0,
            "avg_v3ab_y_pct": round(_pnl_sum(v3ab_y[code]) / len(ex_y), 4) if ex_y else 0,
            "exit_counts_baseline": _exit_counts(baseline[code]),
            "exit_counts_v3ab_y": _exit_counts(v3ab_y[code]),
            "longest_loss_streak_baseline": _longest_loss_streak(baseline[code]),
            "longest_loss_streak_v3ab_y": _longest_loss_streak(v3ab_y[code]),
            "new_entries_v3ab_y": _analyze_new_entries(baseline[code], v3ab_y[code]),
            "dropped_vs_baseline": sorted(
                {t["touch_event_id"] for t in ex_b} - {t["touch_event_id"] for t in ex_y}
            ),
        }

    v3y_old_ids = _drawing_ids_from_snapshot(V3Y_SNAPSHOT)
    v3ab_ids = refresh.drawing_ids
    id_diff = {
        "removed_vs_v3y_snapshot": sorted(set(v3y_old_ids) - set(v3ab_ids)),
        "added_vs_v3y_snapshot": sorted(set(v3ab_ids) - set(v3y_old_ids)),
        "unchanged": sorted(set(v3y_old_ids) & set(v3ab_ids)),
    }

    v3y_prior = _load_v3y_stats()
    v3y_test_b = v3y_prior.get("test_b") or []

    trade_rows: list[dict] = []
    for code in "ABC":
        vname = VARIANTS[code].code
        for tr in _executed(baseline[code]):
            trade_rows.append(_trade_row(tr, scenario="BASELINE_NO_SUPPORT_EXIT", variant_code=vname))
        for tr in _executed(v3ab_y[code]):
            trade_rows.append(_trade_row(tr, scenario="V3AB_SUPPORT_BREAK_EXIT", variant_code=vname))

    manifest = {
        "run_id": RUN_ID,
        "canonical_path": str(canonical),
        "drawing_health": health,
        "drawings_sha256": sha,
        "drawings_snapshot": str(snapshot_path),
        "v3y_frozen_snapshot_sha256": V3Y_SNAPSHOT_SHA,
        "drawing_id_diff_vs_v3y_frozen": id_diff,
        "drawing_inventory": frozen["drawing_inventory"],
        "confirm_delta_pct": CONFIRM_DELTA_PCT,
        "variants": stats,
        "v3y_prior_manifest_test_b": v3y_test_b,
        "stale_copy_warnings": refresh.warnings,
    }

    _write_csv(OUT / "l1_v3ab_trades_all.csv", trade_rows)
    for code, var in VARIANTS.items():
        vcode = var.code
        sub = [r for r in trade_rows if r.get("variant") == vcode]
        _write_csv(OUT / f"l1_v3ab_trades_variant_{code}.csv", sub)
    _write_csv(OUT / "l1_v3ab_summary.csv", [{"variant": k, **v} for k, v in stats.items()])
    (OUT / "l1_v3ab_drawing_inventory.json").write_text(
        json.dumps(frozen["drawing_inventory"], indent=2), encoding="utf-8"
    )
    (OUT / "l1_v3ab_run_manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")

    report = _build_report(manifest, stats, v3y_test_b, id_diff, trade_rows)
    (OUT / "L1_V3AB_NEW_DRAWINGS_BACKTEST_REPORT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AB_NEW_DRAWINGS_BACKTEST_REPORT.md")
    return 0


def _build_report(
    manifest: dict,
    stats: dict,
    v3y_test_b: list,
    id_diff: dict,
    trade_rows: list,
) -> str:
    lines = [
        "# L1 V3AB — Backtest mit aktuellen Zeichnungen",
        "",
        f"Run: `{manifest['run_id']}`",
        f"Kanonisch: `{manifest['canonical_path']}`",
        f"Snapshot SHA256: `{manifest['drawings_sha256']}`",
        f"V3Y-Frozen-SHA (Vergleich Zeichnungen): `{manifest['v3y_frozen_snapshot_sha256'][:16]}…`",
        f"Drawing health preflight: **drift=false** (RAM reload ≡ file)",
        "",
        "Strategie: identisch V3Y (`INDEPENDENT_SUPPORT`, C1, Pool/Kanal-TP, SL −1,20 %, Supportbruch 0,20 %).",
        "Alle Trades: **RETROSPECTIVE_DRAWING**. Keine Übernahme in Produktion.",
        "",
        "## Zeichnungs-Δ vs. V3U/V3Y-Frozen-Snapshot",
        "",
        f"- Entfernt ({len(id_diff['removed_vs_v3y_snapshot'])}): `{', '.join(id_diff['removed_vs_v3y_snapshot']) or '—'}`",
        f"- Neu ({len(id_diff['added_vs_v3y_snapshot'])}): `{', '.join(id_diff['added_vs_v3y_snapshot']) or '—'}`",
        f"- Unveränderte IDs: **{len(id_diff['unchanged'])}**",
        "",
        "Inventar: `l1_v3ab_drawing_inventory.json` · Immutable: `l1_v3ab_drawings_snapshot_*.json`",
        "",
        "## Vergleich V3Y (alte Zeichnungen) → V3AB (neue Zeichnungen)",
        "",
        "| Var | Trades V3Y→V3AB (Y) | Σ% V3Y Y | Σ% V3AB Y | Σ% Baseline (ohne Support-Exit) | SUPPORT_BREAK | neue Entries Y |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    v3y_by_var = {row.get("variant"): row for row in v3y_test_b if isinstance(row, dict)}
    for code in "ABC":
        s = stats[code]
        prior = v3y_by_var.get(code, {})
        lines.append(
            f"| {code} | {prior.get('trades_v3y', '—')}→{s['trades_v3ab_y']} | "
            f"{prior.get('gross_v3y_pct', '—')} | {s['gross_v3ab_y_pct']} | {s['gross_baseline_pct']} | "
            f"{s['exit_counts_v3ab_y'].get('SUPPORT_BREAK_EXIT', 0)} | {len(s['new_entries_v3ab_y'])} |"
        )

    lines.extend(
        [
            "",
            "## Kennzahlen je Variante (V3AB)",
            "",
            "| Var | Trades BL / Y | Gewinne BL / Y | Verluste BL / Y | Ø% BL / Y | längste Loss BL / Y |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for code in "ABC":
        s = stats[code]
        lines.append(
            f"| {code} | {s['trades_baseline']}/{s['trades_v3ab_y']} | {s['wins_baseline']}/{s['wins_v3ab_y']} | "
            f"{s['losses_baseline']}/{s['losses_v3ab_y']} | {s['avg_baseline_pct']}/{s['avg_v3ab_y_pct']} | "
            f"{s['longest_loss_streak_baseline']}/{s['longest_loss_streak_v3ab_y']} |"
        )

    lines.extend(
        [
            "",
            "Exit-Mix (V3AB Support-Exit): siehe `l1_v3ab_summary.csv` → `exit_counts_*`.",
            "",
            "Vollständige Trades: `l1_v3ab_trades_all.csv`",
            "",
            "## Reproduktion",
            "",
            "```bash",
            "cd support_break_exit_v3ab",
            "python3 run_v3ab.py",
            "```",
            "",
            "**STOP** — keine Änderung an drawings.json, V3Y-Artefakten oder Hauptstrategie.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
