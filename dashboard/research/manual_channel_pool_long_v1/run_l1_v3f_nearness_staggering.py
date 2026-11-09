#!/usr/bin/env python3
"""L1 V3F — V3E filters on extended window 2026-08-25 .. 2026-09-01 (excl.)."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
ROOT = PROBE_DIR.parents[1]
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"
V3E_SHA_PREFIX = "3e7041de2814607b"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
from config import (
    D2_MIN_DEPTH,
    L1_V3E_VARIANT_A,
    L1_V3E_VARIANT_B,
    L1_V3E_VARIANT_C,
    MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT,
    MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER,
    PILOT_ALLOWED_LOWER_TREND_IDS,
)
from line_geometry import utc_ts
from pd1_exact_d2_adapter import D2_COMPUTE_VERSION
from pool_d2_metrics import (
    apply_d2_gate_to_pending,
    enrich_d2_channel_filter_fields,
)

OUT_DIR = PROBE_DIR
BAR_SEC = 300
EXPECTED_BARS = 7 * 24 * 12  # 2016

V3F_WINDOW_START = utc_ts(datetime(2026, 8, 25, 0, 0, 0, tzinfo=timezone.utc))
V3F_WINDOW_END = utc_ts(datetime(2026, 9, 1, 0, 0, 0, tzinfo=timezone.utc))
EXT_SEGMENT_START = utc_ts(datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc))
V3E_WINDOW_END = utc_ts(datetime(2026, 8, 29, 0, 0, 0, tzinfo=timezone.utc))


def _utc_ts_from_row(s: str) -> int | None:
    if not s:
        return None
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc).timestamp())


def _drawings_manifest() -> dict[str, str]:
    text = DRAWINGS_PATH.read_text(encoding="utf-8") if DRAWINGS_PATH.is_file() else ""
    payload = json.loads(text) if text else {}
    sha = hashlib.sha256(text.encode()).hexdigest() if text else ""
    ids = {str(d.get("drawing_id") or d.get("id") or "") for d in payload.get("drawings") or []}
    allowed_present = sorted(i for i in PILOT_ALLOWED_LOWER_TREND_IDS if i in ids)
    missing = sorted(i for i in PILOT_ALLOWED_LOWER_TREND_IDS if i not in ids)
    return {
        "sha256": sha,
        "sha_matches_v3e": sha.startswith(V3E_SHA_PREFIX),
        "count": str(len(payload.get("drawings") or [])),
        "allowlist_present": ", ".join(allowed_present),
        "allowlist_missing": ", ".join(missing),
    }


def _git_head() -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def _audit_candles(candles: list[dict]) -> tuple[int, list[str]]:
    by_ts = {int(c["time"]) for c in candles}
    gaps: list[str] = []
    for t in range(V3F_WINDOW_START, V3F_WINDOW_END, BAR_SEC):
        if t not in by_ts:
            gaps.append(l1.fmt_utc(t))
    return len(candles), gaps


def _executed(trades: list[dict]) -> list[dict]:
    return [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "")]


def _realized(trades: list[dict]) -> list[dict]:
    return [t for t in _executed(trades) if t.get("exit_kind") != "OPEN_AT_WINDOW_END"]


def _write_trades_csv(path: Path, trades: list[dict]) -> None:
    rows = [{k: v for k, v in t.items() if not str(k).startswith("_")} for t in trades]
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=sorted({k for r in rows for k in r}))
        w.writeheader()
        w.writerows(rows)


def _summary_metrics(trades: list[dict], touch_stats: dict) -> dict[str, Any]:
    ex = _executed(trades)
    realized = _realized(trades)
    open_tr = [t for t in ex if t.get("exit_kind") == "OPEN_AT_WINDOW_END"]
    wins = [float(t["gross_pnl_pct"]) for t in realized if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
    losses = [float(t["gross_pnl_pct"]) for t in realized if t.get("exit_kind") == "SL"]
    sm = pe.summarize_variant(trades, touch_stats)
    sm["win_count"] = len([t for t in realized if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")])
    sm["loss_count"] = len([t for t in realized if t.get("exit_kind") == "SL"])
    sm["wins_sum"] = sum(wins)
    sm["losses_sum"] = sum(losses)
    sm["gross_realized_sum"] = sum(wins) + sum(losses)
    sm["open_count"] = len(open_tr)
    sm["open_mtm_sum"] = sum(float(t["gross_pnl_pct"]) for t in open_tr) if open_tr else 0.0
    sm["reject_first_d2"] = sm["rejects"].get("REJECTED_FIRST_D2_TOO_FAR", 0)
    sm["reject_stagger"] = sm["rejects"].get("REJECTED_NO_EARLY_D2_STAGGERING", 0)
    return sm


def _admitted_candidates(
    variant,
    pending: list[dict],
    candles: list[dict],
    *,
    upper_line,
    l1_module: Any,
) -> int:
    channel_line_at_bar = l1_module.channel_line_at_bar

    def upper_px(candle: dict) -> float | None:
        return channel_line_at_bar(upper_line, candle)

    cycle = pe.ChannelCycleState()
    n = 0
    for row in sorted(
        pending,
        key=lambda r: (
            int(r["_entry_idx"]) if r.get("_entry_idx") not in (None, "") else 10**9,
            r["touch_event_id"],
        ),
    ):
        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            continue
        entry_idx = int(row["_entry_idx"])
        tsi = row.get("_touch_start_idx")
        if tsi is not None:
            cycle.maybe_reset_at_touch_start(candles, int(tsi), upper_px)
        cycle.advance_to(candles, entry_idx - 1, upper_px)
        entry = float(row["entry_price"])
        main_upper = row.get("_main_upper_at_entry")
        eff = row.get("_effective_tp")
        reason = pe.apply_variant_entry_filters(variant, entry, main_upper, eff, cycle, row=row)
        if reason:
            continue
        if row.get("_pick_ok"):
            n += 1
    return n


def _filter_decisions(row: dict, variants: dict) -> dict[str, str]:
    from channel_cycle import ChannelCycleState

    out: dict[str, str] = {}
    for code, var in variants.items():
        if row.get("long_trigger_allowed") != "yes" or not row.get("_entry_idx"):
            out[code] = row.get("reject_reason") or "NOT_LONG_TRIGGER"
            continue
        if not row.get("_d2_gate_ok"):
            out[code] = row.get("d2_gate_reason") or "D2_GATE_FAIL"
            continue
        entry = float(row["entry_price"])
        main_upper = row.get("_main_upper_at_entry")
        cycle = ChannelCycleState()
        eff = row.get("_effective_tp")
        reason = pe.apply_variant_entry_filters(var, entry, main_upper, eff, cycle, row=row)
        if reason:
            out[code] = reason
        elif not row.get("_pick_ok"):
            out[code] = row.get("reject_reason") or "PICK_FAIL"
        else:
            out[code] = "ADMIT"
    return out


def _chronological_trade_sections(trades: list[dict], label: str) -> list[str]:
    realized = sorted(_realized(trades), key=lambda t: t.get("entry_utc", ""))
    wins = [t for t in realized if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
    losses = [t for t in realized if t.get("exit_kind") == "SL"]
    open_tr = sorted(
        [t for t in _executed(trades) if t.get("exit_kind") == "OPEN_AT_WINDOW_END"],
        key=lambda t: t.get("entry_utc", ""),
    )
    out = [f"### {label}", ""]
    out.append("#### Gewinn-Trades (realisiert)")
    if wins:
        out.append(
            "| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | drawing_id | PnL % |"
        )
        out.append("| --- | --- | --- | --- | --- | --- | --- | --- | ---: |")
        for t in wins:
            out.append(
                f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                f"| {t['exit_price']} | {t['exit_utc']} | {t['exit_kind']} | {t['touch_drawing_id']} "
                f"| {t['gross_pnl_pct']} |"
            )
    else:
        out.append("_Keine._")
    out.extend(["", "#### Stop-Loss-Trades", ""])
    if losses:
        out.append(
            "| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | drawing_id | PnL % |"
        )
        out.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
        for t in losses:
            out.append(
                f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                f"| {t['exit_price']} | {t['exit_utc']} | {t['touch_drawing_id']} | {t['gross_pnl_pct']} |"
            )
    else:
        out.append("_Keine._")
    out.extend(["", "#### Am Fensterende offen (nicht in Σ realisiert)", ""])
    if open_tr:
        out.append("| Entry UTC | Entry | SL | TP@Entry | Mark @End | Exit UTC | drawing_id | MTM % |")
        out.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
        for t in open_tr:
            out.append(
                f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                f"| {t['exit_price']} | {t['exit_utc']} | {t['touch_drawing_id']} | {t['gross_pnl_pct']} |"
            )
    else:
        out.append("_Keine._")
    out.append("")
    return out


def _in_extended_segment(row: dict) -> bool:
    ts = _utc_ts_from_row(str(row.get("entry_utc") or row.get("touch_utc") or ""))
    return ts is not None and EXT_SEGMENT_START <= ts < V3F_WINDOW_END


def _segment_summary(trades: list[dict], pending: list[dict], *, only_new: bool) -> dict[str, int]:
    def keep(row: dict) -> bool:
        ts = _utc_ts_from_row(str(row.get("entry_utc") or ""))
        if ts is None:
            return False
        in_ext = EXT_SEGMENT_START <= ts < V3F_WINDOW_END
        return in_ext if only_new else ts < V3E_WINDOW_END

    ex = [t for t in _executed(trades) if keep(t)]
    cands = [r for r in pending if r.get("entry_utc") and keep(r)]
    return {
        "candidates": len(cands),
        "executed": len(ex),
        "sl": sum(1 for t in ex if t.get("exit_kind") == "SL"),
        "wins": sum(1 for t in ex if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")),
    }


def main() -> int:
    l1._bootstrap_import_paths()
    manifest = _drawings_manifest()
    if not manifest["sha_matches_v3e"]:
        print(
            f"WARN: drawings SHA256 {manifest['sha256'][:16]}… does not match V3E prefix {V3E_SHA_PREFIX}",
            file=sys.stderr,
        )

    saved_start, saved_end = l1.WINDOW_START, l1.WINDOW_END
    l1.WINDOW_START = V3F_WINDOW_START
    l1.WINDOW_END = V3F_WINDOW_END
    try:
        ctx = l1.build_pilot_context()
    finally:
        l1.WINDOW_START, l1.WINDOW_END = saved_start, saved_end

    if ctx is None:
        return 2

    n_candles, gaps = _audit_candles(ctx["candles"])
    mod = sys.modules[l1.__name__]
    lines = list(ctx["lines_by_id"].values())

    pending = copy.deepcopy(ctx["pending"])
    apply_d2_gate_to_pending(pending, lines, l1_module=mod, apply_provisional_research_filter=False)
    pe.enrich_pending_metrics(pending, ctx["upper_line"], mod)
    enrich_d2_channel_filter_fields(pending, ctx["upper_line"], mod)

    variants = {"A": L1_V3E_VARIANT_A, "B": L1_V3E_VARIANT_B, "C": L1_V3E_VARIANT_C}
    tr: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    for code, var in variants.items():
        tr[code] = pe.execute_variant(
            var, copy.deepcopy(pending), ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        sm = _summary_metrics(tr[code], ctx["touch_stats"])
        sm["admitted"] = _admitted_candidates(
            var, pending, ctx["candles"], upper_line=ctx["upper_line"], l1_module=mod
        )
        summary[code] = sm
        _write_trades_csv(OUT_DIR / f"l1_v3f_variant_{code.lower()}_trades.csv", tr[code])

    cand_rows: list[dict] = []
    for row in pending:
        if not row.get("entry_utc"):
            continue
        base = {k: v for k, v in row.items() if not str(k).startswith("_")}
        dec = _filter_decisions(row, variants)
        base["filter_A"], base["filter_B"], base["filter_C"] = dec["A"], dec["B"], dec["C"]
        cand_rows.append(base)
    with (OUT_DIR / "l1_v3f_candidates.csv").open("w", newline="", encoding="utf-8") as f:
        if cand_rows:
            w = csv.DictWriter(f, fieldnames=sorted({k for r in cand_rows for k in r}))
            w.writeheader()
            w.writerows(cand_rows)

    ext_cands = [r for r in pending if r.get("entry_utc") and _in_extended_segment(r)]
    global_blocked = [
        t
        for t in tr["A"]
        if t.get("reject_reason") == "GLOBAL_MAX_ONE_OPEN_LONG" and _in_extended_segment(t)
    ]

    identical_bc = all(
        tr["B"][i].get("exit_kind") == tr["C"][i].get("exit_kind")
        and tr["B"][i].get("reject_reason") == tr["C"][i].get("reject_reason")
        for i in range(len(tr["B"]))
    )

    md = [
        "# L1 V3F — Backtest 25.08.–01.09.2026 (V3E-Filter)",
        "",
        "## Reproduzierbarkeit",
        "",
        f"- Git: `{_git_head()}`",
        f"- Fenster: **{l1.fmt_utc(V3F_WINDOW_START)} .. {l1.fmt_utc(V3F_WINDOW_END)}** (exkl.)",
        f"- Kerzen geladen: **{n_candles}** (erwartet {EXPECTED_BARS})",
        f"- Datenlücken: **{len(gaps)}**" + (f" — erste: {gaps[0]}" if gaps else ""),
        f"- drawings SHA256: `{manifest['sha256']}`",
        f"- Gleicher Stand wie V3E (Präfix `{V3E_SHA_PREFIX}`): **{'ja' if manifest['sha_matches_v3e'] else 'NEIN — Vergleich eingeschränkt'}**",
        f"- Allowlist fehlt: {manifest['allowlist_missing'] or '—'}",
        f"- Filter unverändert: D2-Gate, Nähe≤{MAX_DISTANCE_TO_FIRST_D2_UPPER_PCT}%, Q1≥{MIN_D2_BANDS_IN_FIRST_CHANNEL_QUARTER}",
        "",
        "## Vergleichstabelle (gesamtes Fenster)",
        "",
        "| Kennzahl | A | B | C |",
        "| --- | ---: | ---: | ---: |",
        f"| Touch-Ereignisse | {summary['A']['touch_events']} | = | = |",
        f"| Freigegebene Kandidaten (ohne 1-Long-Cap) | {summary['A']['admitted']} | {summary['B']['admitted']} | {summary['C']['admitted']} |",
        f"| Ausgeführte Trades | {summary['A']['executed']} | {summary['B']['executed']} | {summary['C']['executed']} |",
        f"| Gewinner (realisiert) | {summary['A']['win_count']} | {summary['B']['win_count']} | {summary['C']['win_count']} |",
        f"| Stop-Loss | {summary['A']['sl']} | {summary['B']['sl']} | {summary['C']['sl']} |",
        f"| Kanal-TP | {summary['A']['tp_channel']} | {summary['B']['tp_channel']} | {summary['C']['tp_channel']} |",
        f"| Pool-TP | {summary['A']['tp_pool']} | {summary['B']['tp_pool']} | {summary['C']['tp_pool']} |",
        f"| Am Fensterende offen | {summary['A']['open_count']} | {summary['B']['open_count']} | {summary['C']['open_count']} |",
        f"| Σ realisierte Gewinne % | {summary['A']['wins_sum']:.4f} | {summary['B']['wins_sum']:.4f} | {summary['C']['wins_sum']:.4f} |",
        f"| Σ realisierte Verluste % | {summary['A']['losses_sum']:.4f} | {summary['B']['losses_sum']:.4f} | {summary['C']['losses_sum']:.4f} |",
        f"| **Gesamtbrutto realisiert %** | **{summary['A']['gross_realized_sum']:.4f}** | **{summary['B']['gross_realized_sum']:.4f}** | **{summary['C']['gross_realized_sum']:.4f}** |",
        f"| MTM offen % (nicht in Brutto) | {summary['A']['open_mtm_sum']:.4f} | {summary['B']['open_mtm_sum']:.4f} | {summary['C']['open_mtm_sum']:.4f} |",
        f"| REJECTED_FIRST_D2_TOO_FAR | 0 | {summary['B']['reject_first_d2']} | {summary['C']['reject_first_d2']} |",
        f"| REJECTED_NO_EARLY_D2_STAGGERING | 0 | 0 | {summary['C']['reject_stagger']} |",
        "",
        "## Neues Segment 29.08.–01.09.",
        "",
        "| | Kandidaten (Entry) | Executed A | Executed B | Executed C |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    seg = {c: _segment_summary(tr[c], pending, only_new=True) for c in ("A", "B", "C")}
    md.append(
        f"| nur 29.08+ | {len(ext_cands)} | {seg['A']['executed']} | {seg['B']['executed']} | {seg['C']['executed']} |"
    )
    md.extend(
        [
            "",
            f"- Kandidaten mit Entry im Segment: **{len(ext_cands)}**",
            f"- GLOBAL_MAX_ONE_OPEN_LONG im Segment (A): **{len(global_blocked)}**",
            "",
            "## Unterschied zu V3E (25.08.–29.08. exkl.)",
            "",
            "V3E-Referenz: A exec=2 brutto=5.3449% | B/C exec=1 brutto=6.5449%.",
            "",
        ]
    )
    for code in ("A", "B", "C"):
        old = _segment_summary(tr[code], pending, only_new=False)
        md.append(
            f"- Variante {code} im alten Teil: Kandidaten {old['candidates']}, "
            f"executed {old['executed']} (SL {old['sl']}, Gewinn {old['wins']})"
        )

    md.extend(
        [
            "",
            "## Auswertung Segment 29.08.–01.09. (§10)",
            "",
            f"1. Weitere Long-Kandidaten: **{len(ext_cands)}** mit Entry-Zeit im Segment.",
            f"2. Davon ausgeführt: A={seg['A']['executed']}, B={seg['B']['executed']}, C={seg['C']['executed']}.",
            f"3. A/B/C-Unterschied im Segment: siehe executed-Spalten; B/C identisch bis auf Stagger-Rejects: {identical_bc}.",
            "4./5. Staffelung: nur relevant wenn zusätzliche Kandidaten im Segment — numerisch oben.",
            f"6. 1-Long-Regel: {len(global_blocked)} Ablehnungen `GLOBAL_MAX_ONE_OPEN_LONG` im Segment (Variante A).",
            "7. Schwellen: keine Parameteränderung; Gegenbeispiele im erweiterten Fenster dokumentieren Limitation.",
            "",
        ]
    )
    for code, label in (("A", "Variante A"), ("B", "Variante B"), ("C", "Variante C")):
        md.extend(_chronological_trade_sections(tr[code], label))

    (OUT_DIR / "l1_v3f_comparison_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote l1_v3f_comparison_report.md | candles={n_candles} gaps={len(gaps)}")
    print(
        f"A realized={summary['A']['gross_realized_sum']:.4f} | "
        f"B={summary['B']['gross_realized_sum']:.4f} | C={summary['C']['gross_realized_sum']:.4f} | "
        f"open A/B/C={summary['A']['open_count']}/{summary['B']['open_count']}/{summary['C']['open_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
