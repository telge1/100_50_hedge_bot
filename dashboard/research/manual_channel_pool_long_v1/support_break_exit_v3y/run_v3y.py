#!/usr/bin/env python3
"""L1 V3Y — support break exit research backtest (isolated)."""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

V3Y = Path(__file__).resolve().parent
PROBE = V3Y.parent
if str(PROBE) not in sys.path:
    sys.path.insert(0, str(PROBE))
if str(V3Y) not in sys.path:
    sys.path.insert(0, str(V3Y))

import pilot_engine as pe
import run_l1_pilot as l1
import run_l1_v3g_nearness_staggering as v3g

from v3y_config import CONFIRM_DELTA_PCT, DRAWINGS_SHA256, EXIT_KIND, REFERENCE_TRADE
from context import VARIANTS, build_frozen_context
from engine import execute_variant_v3y, resimulate_row_v3u_baseline, resimulate_row_v3y

OUT = V3Y


def _executed(trades: list[dict]) -> list[dict]:
    return v3g._executed(trades)


def _pnl_sum(trades: list[dict]) -> float:
    return round(sum(float(t["gross_pnl_pct"]) for t in _executed(trades) if t.get("gross_pnl_pct")), 4)


def _trade_out_row(tr: dict, *, scenario: str) -> dict[str, str]:
    retro = tr.get("drawing_causality") == "RETROSPECTIVE_DRAWING"
    return {
        "scenario": scenario,
        "variant": tr.get("variant", ""),
        "entry_utc": tr.get("entry_utc", ""),
        "entry_price": tr.get("entry_price", ""),
        "tp_price": tr.get("tp_price", ""),
        "sl_price": tr.get("sl_price", ""),
        "exit_utc": tr.get("exit_utc", ""),
        "exit_price": tr.get("exit_price", ""),
        "exit_reason": tr.get("exit_kind", ""),
        "pnl_pct": tr.get("gross_pnl_pct", ""),
        "support_drawing_id": tr.get("touch_drawing_id", tr.get("support_drawing_id", "")),
        "retrospective": "yes" if retro else "no",
        "tp_mode": tr.get("tp_mode", ""),
    }


def _apply_exit_to_row(row: dict, kind: str, exit_ts, exit_px, pnl) -> dict:
    out = {k: v for k, v in row.items() if not k.startswith("_")}
    out["exit_kind"] = kind
    out["exit_utc"] = l1.fmt_utc(exit_ts) if exit_ts else ""
    out["exit_price"] = f"{exit_px:.10f}" if exit_px is not None else ""
    out["gross_pnl_pct"] = f"{pnl:.4f}" if pnl is not None else ""
    return out


def test_a_identical_entries(
    pending: list[dict],
    candles: list[dict],
    lines_by_id: dict,
    mod: Any,
    *,
    baseline_trades: dict[str, list[dict]],
) -> tuple[list[dict], dict[str, Any]]:
    """Same V3U chronological entries; re-exit with V3Y only."""
    rows_out: list[dict] = []
    stats: dict[str, Any] = {}
    for code in "ABC":
        base_exec = _executed(baseline_trades[code])
        base_by_eid = {t["touch_event_id"]: t for t in base_exec}
        changed = 0
        support_exits = 0
        for tr in base_exec:
            pending_row = next(
                (p for p in pending if p.get("touch_event_id") == tr["touch_event_id"]),
                None,
            )
            if pending_row is None:
                continue
            kind_u, ts_u, px_u, pnl_u, _ = resimulate_row_v3u_baseline(pending_row, candles, mod)
            kind_y, ts_y, px_y, pnl_y, _ = resimulate_row_v3y(pending_row, candles, lines_by_id, mod)
            r_u = _trade_out_row(
                _apply_exit_to_row(pending_row, kind_u, ts_u, px_u, pnl_u) | {"variant": code},
                scenario="V3U_EXIT",
            )
            r_y = _trade_out_row(
                _apply_exit_to_row(pending_row, kind_y, ts_y, px_y, pnl_y) | {"variant": code},
                scenario="V3Y_EXIT",
            )
            r_u["touch_event_id"] = tr["touch_event_id"]
            r_y["touch_event_id"] = tr["touch_event_id"]
            rows_out.extend([r_u, r_y])
            if kind_y == EXIT_KIND:
                support_exits += 1
            if kind_u != kind_y or abs(float(pnl_u or 0) - float(pnl_y or 0)) > 1e-6:
                changed += 1
        stats[code] = {
            "executed_entries": len(base_exec),
            "exit_changes_vs_v3u": changed,
            "support_break_exits": support_exits,
            "gross_v3u_pct": _pnl_sum(baseline_trades[code]),
            "gross_v3y_resim_pct": round(
                sum(
                    float(
                        _apply_exit_to_row(
                            next(p for p in pending if p["touch_event_id"] == t["touch_event_id"]),
                            *resimulate_row_v3y(
                                next(p for p in pending if p["touch_event_id"] == t["touch_event_id"]),
                                candles,
                                lines_by_id,
                                mod,
                            )[:4],
                        )["gross_pnl_pct"]
                    )
                    for t in base_exec
                ),
                4,
            ),
        }
    return rows_out, stats


def _longest_loss_streak(trades: list[dict]) -> int:
    streak = best = 0
    for t in _executed(trades):
        if float(t.get("gross_pnl_pct") or 0) < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def _analyze_b(
    baseline: list[dict],
    v3y: list[dict],
    *,
    code: str,
) -> dict[str, Any]:
    b_exec = _executed(baseline)
    y_exec = _executed(v3y)
    b_ids = {t["touch_event_id"] for t in b_exec}
    y_ids = {t["touch_event_id"] for t in y_exec}
    new_entries = sorted(y_ids - b_ids)
    lost_entries = sorted(b_ids - y_ids)
    support_cnt = sum(1 for t in y_exec if t.get("exit_kind") == EXIT_KIND)
    sl_b = sum(1 for t in b_exec if t.get("exit_kind") == "SL")
    sl_y = sum(1 for t in y_exec if t.get("exit_kind") == "SL")
    wins_b = [t for t in b_exec if float(t.get("gross_pnl_pct") or 0) > 0]
    wins_y = [t for t in y_exec if float(t.get("gross_pnl_pct") or 0) > 0]
    saved = 0
    early_win = 0
    for t in y_exec:
        if t.get("exit_kind") != EXIT_KIND:
            continue
        ref = next((x for x in b_exec if x["touch_event_id"] == t["touch_event_id"]), None)
        if ref is None:
            continue
        if ref.get("exit_kind") == "SL" and float(t.get("gross_pnl_pct") or 0) > float(ref.get("gross_pnl_pct") or 0):
            saved += 1
        if ref.get("exit_kind") in ("TP", "CHANNEL_UPPER") and float(t.get("gross_pnl_pct") or 0) < float(
            ref.get("gross_pnl_pct") or 0
        ):
            early_win += 1
    return {
        "variant": code,
        "trades_v3u": len(b_exec),
        "trades_v3y": len(y_exec),
        "gross_v3u_pct": _pnl_sum(baseline),
        "gross_v3y_pct": _pnl_sum(v3y),
        "wins_v3u": len(wins_b),
        "wins_v3y": len(wins_y),
        "sl_v3u": sl_b,
        "sl_v3y": sl_y,
        "support_break_exits": support_cnt,
        "saved_from_sl": saved,
        "early_closed_winners": early_win,
        "new_entries_v3y": new_entries,
        "dropped_entries_v3y": lost_entries,
        "longest_loss_streak_v3u": _longest_loss_streak(baseline),
        "longest_loss_streak_v3y": _longest_loss_streak(v3y),
    }


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_(keine Trades)_", ""]
    sep = "|" + "|".join("---" for _ in headers) + "|"
    out = [
        "|" + "|".join(headers) + "|",
        sep,
    ]
    for r in rows:
        out.append("|" + "|".join(r) + "|")
    out.append("")
    return out


def _md_from_trade_rows(trades: list[dict], *, label: str) -> list[str]:
    lines = [f"### {label}", ""]
    body = [
        [
            tr.get("entry_utc", ""),
            tr.get("entry_price", ""),
            tr.get("tp_price", ""),
            tr.get("sl_price", ""),
            tr.get("exit_utc", ""),
            tr.get("exit_price", ""),
            tr.get("exit_kind", tr.get("exit_reason", "")),
            str(tr.get("gross_pnl_pct", tr.get("pnl_pct", ""))),
            "RETRO" if tr.get("drawing_causality") == "RETROSPECTIVE_DRAWING" else "",
        ]
        for tr in trades
    ]
    headers = [
        "Entry UTC",
        "Entry",
        "TP",
        "SL",
        "Exit UTC",
        "Exit",
        "Exit-Grund",
        "Brutto %",
        "Retro",
    ]
    lines.extend(_md_table(headers, body))
    return lines


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    l1._bootstrap_import_paths()
    frozen = build_frozen_context()
    pending = frozen["pending"]
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    upper_line = frozen["ctx"]["upper_line"]
    mod = sys.modules[l1.__name__]

    baseline_trades: dict[str, list[dict]] = {}
    v3y_trades: dict[str, list[dict]] = {}
    for code, var in VARIANTS.items():
        baseline_trades[code] = pe.execute_variant(
            var, __import__("copy").deepcopy(pending), candles, upper_line=upper_line, l1_module=mod
        )
        v3y_trades[code] = execute_variant_v3y(
            var,
            __import__("copy").deepcopy(pending),
            candles,
            upper_line=upper_line,
            lines_by_id=lines_by_id,
            l1_module=mod,
        )

    test_a_rows, test_a_stats = test_a_identical_entries(
        pending, candles, lines_by_id, mod, baseline_trades=baseline_trades
    )

    test_b_rows: list[dict] = []
    test_b_baseline_rows: list[dict] = []
    test_b_summary: list[dict] = []
    for code in "ABC":
        for tr in _executed(baseline_trades[code]):
            test_b_baseline_rows.append(_trade_out_row(tr, scenario="V3U_CHRONO"))
        for tr in _executed(v3y_trades[code]):
            test_b_rows.append(_trade_out_row(tr, scenario="V3Y_CHRONO"))
        test_b_summary.append(_analyze_b(baseline_trades[code], v3y_trades[code], code=code))

    ref_checks = {}
    for tr in _executed(v3y_trades["A"]):
        if tr.get("entry_utc", "").startswith("2026-08-26 15:10"):
            ref_checks = {
                "entry_utc": tr["entry_utc"],
                "exit_utc": tr.get("exit_utc"),
                "exit_price": tr.get("exit_price"),
                "exit_kind": tr.get("exit_kind"),
                "pnl_pct": tr.get("gross_pnl_pct"),
            }

    manifest = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "drawings_sha256": DRAWINGS_SHA256,
        "confirm_delta_pct": CONFIRM_DELTA_PCT,
        "test_a": test_a_stats,
        "test_b": test_b_summary,
        "reference_trade_v3y_A": ref_checks,
        "expected_reference": REFERENCE_TRADE,
    }

    _write_csv(OUT / "l1_v3y_test_a_compare.csv", test_a_rows)
    _write_csv(OUT / "l1_v3y_test_b_trades.csv", test_b_rows)
    _write_csv(OUT / "l1_v3y_test_b_baseline_trades.csv", test_b_baseline_rows)
    _write_csv(OUT / "l1_v3y_test_b_summary.csv", test_b_summary)
    (OUT / "l1_v3y_run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    lines_md = [
        "# L1 V3Y — Supportbruch-Exit Backtest",
        "",
        f"Run: `{manifest['run_id']}` · Drawings SHA256 `{DRAWINGS_SHA256[:16]}…`",
        f"Bestätigungsschwelle: **{CONFIRM_DELTA_PCT:.2f} %** gegen Bruch-Close",
        "",
        "Renditen: **summierte Brutto-Trade-%** (nicht kapitalisiert). Retrospektive Zeichnungen markiert.",
        "",
        "## Referenztrade DOGE 26.08.2026 (Variante A chronologisch)",
        "",
        f"| Feld | Erwartung | V3Y |",
        f"|---|---|---|",
        f"| Exit UTC | `{REFERENCE_TRADE['exit_utc']}` | `{ref_checks.get('exit_utc','—')}` |",
        f"| Exit-Preis | `{REFERENCE_TRADE['exit_price']}` | `{ref_checks.get('exit_price','—')}` |",
        f"| Exit-Grund | `SUPPORT_BREAK_EXIT` | `{ref_checks.get('exit_kind','—')}` |",
        f"| Brutto % | `{REFERENCE_TRADE['pnl_pct']}` | `{ref_checks.get('pnl_pct','—')}` |",
        "",
        "## Test A — identische Entries (V3U-Reihenfolge), Exit-Vergleich",
        "",
        "| Variante | Trades | Δ Exits | SUPPORT_BREAK | Σ V3U % | Σ V3Y (Resim) % |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for code in "ABC":
        s = test_a_stats[code]
        lines_md.append(
            f"| {code} | {s['executed_entries']} | {s['exit_changes_vs_v3u']} | {s['support_break_exits']} | "
            f"{s['gross_v3u_pct']} | {s['gross_v3y_resim_pct']} |"
        )

    lines_md.extend(["", "### Test A — Volltabellen (identische Entries)", ""])
    for code in "ABC":
        base_exec = _executed(baseline_trades[code])
        lines_md.append(f"#### Variante {code}")
        lines_md.append("")
        pair_rows: list[list[str]] = []
        y_by_eid = {
            r["touch_event_id"]: r
            for r in test_a_rows
            if r.get("variant") == code and r.get("scenario") == "V3Y_EXIT"
        }
        u_by_eid = {
            r["touch_event_id"]: r
            for r in test_a_rows
            if r.get("variant") == code and r.get("scenario") == "V3U_EXIT"
        }
        for tr in base_exec:
            eid = tr["touch_event_id"]
            u = u_by_eid.get(eid, {})
            y = y_by_eid.get(eid, {})
            pair_rows.append(
                [
                    tr.get("entry_utc", ""),
                    tr.get("entry_price", ""),
                    tr.get("tp_price", ""),
                    tr.get("sl_price", ""),
                    u.get("exit_utc", ""),
                    u.get("exit_price", ""),
                    u.get("exit_reason", ""),
                    u.get("pnl_pct", ""),
                    y.get("exit_utc", ""),
                    y.get("exit_price", ""),
                    y.get("exit_reason", ""),
                    y.get("pnl_pct", ""),
                ]
            )
        headers_a = [
            "Entry UTC",
            "Entry",
            "TP",
            "SL",
            "Exit UTC (V3U)",
            "Exit (V3U)",
            "Grund U",
            "% U",
            "Exit UTC (V3Y)",
            "Exit (V3Y)",
            "Grund Y",
            "% Y",
        ]
        lines_md.extend(_md_table(headers_a, pair_rows))

    lines_md.extend(
        [
            "CSV: `l1_v3y_test_a_compare.csv`.",
            "",
            "## Test B — chronologisch, max. ein offener Long (V3Y)",
            "",
            "| Var | Trades U→Y | Σ V3U % | Σ V3Y % | Wins U→Y | SL U→Y | SUPPORT_BREAK | gerettet (SL→besser) | früh zu (Gewinner) | neue Entries | längste SL-Serie U→Y |",
            "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in test_b_summary:
        lines_md.append(
            f"| {row['variant']} | {row['trades_v3u']}→{row['trades_v3y']} | {row['gross_v3u_pct']} | {row['gross_v3y_pct']} | "
            f"{row['wins_v3u']}→{row['wins_v3y']} | {row['sl_v3u']}→{row['sl_v3y']} | {row['support_break_exits']} | "
            f"{row['saved_from_sl']} | {row['early_closed_winners']} | {len(row['new_entries_v3y'])} | "
            f"{row['longest_loss_streak_v3u']}→{row['longest_loss_streak_v3y']} |"
        )

    lines_md.extend(["", "### Test B — Volltabellen Trades", ""])
    for code in "ABC":
        lines_md.extend(_md_from_trade_rows(_executed(baseline_trades[code]), label=f"Variante {code} — V3U Baseline"))
        lines_md.extend(_md_from_trade_rows(_executed(v3y_trades[code]), label=f"Variante {code} — V3Y"))
        row = next(x for x in test_b_summary if x["variant"] == code)
        if row["new_entries_v3y"]:
            lines_md.append(f"Neue V3Y-Entries (nicht in V3U-Chrono): `{', '.join(row['new_entries_v3y'])}`")
            lines_md.append("")

    lines_md.extend(
        [
            "CSV: `l1_v3y_test_b_baseline_trades.csv`, `l1_v3y_test_b_trades.csv`, `l1_v3y_test_b_summary.csv`.",
            "",
            "## Tests / Reproduzierbarkeit",
            "",
            "```bash",
            "cd support_break_exit_v3y",
            "python3 test_support_break.py",
            "python3 run_v3y.py",
            "```",
            "",
            "Keine Änderung an V3U/V3W-Produktionscode. Nur Lesen des eingefrorenen V3U-Zeichnungs-Snapshots.",
            "",
            "**STOP** — nicht in Hauptstrategie übernommen.",
        ]
    )
    (OUT / "l1_v3y_full_report.md").write_text("\n".join(lines_md), encoding="utf-8")
    print(OUT / "l1_v3y_full_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
