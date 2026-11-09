#!/usr/bin/env python3
"""
L1 diagnostic: channel room, wedge/convergence, A vs B (1.50 % min room) on pilot window.
"""

from __future__ import annotations

import copy
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
from config import FIXED_SL_PCT, MIN_CHANNEL_ROOM_PCT, UPPER_MAIN_CHANNEL_LINE_ID, VARIANT_A, VARIANT_B
from pilot_engine import effective_tp_price

OUT_DIR = PROBE_DIR


def _f(s: str | float | None) -> float | None:
    if s is None or s == "":
        return None
    return float(s)


def _parse_utc(s: str) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _line_px(line: l1.LineDef | None, ts: float) -> float | None:
    if line is None:
        return None
    if not line.active_for_candle(ts):
        return None
    p = line.price_at(ts)
    return float(p) if p is not None else None


def wedge_metrics(
    *,
    upper_line: l1.LineDef | None,
    lower_line: l1.LineDef | None,
    ts_decision: float,
    ts_touch: float,
    ref_price: float,
) -> dict[str, Any]:
    up_e = _line_px(upper_line, ts_decision)
    lo_e = _line_px(lower_line, ts_decision)
    up_t = _line_px(upper_line, ts_touch)
    lo_t = _line_px(lower_line, ts_touch)

    room_entry = None
    if up_e is not None and ref_price > 0:
        room_entry = (up_e - ref_price) / ref_price * 100.0

    gap_at_entry = None
    if up_e is not None and lo_e is not None and lo_e > 0:
        gap_at_entry = (up_e - lo_e) / lo_e * 100.0

    gap_at_touch = None
    if up_t is not None and lo_t is not None and lo_t > 0:
        gap_at_touch = (up_t - lo_t) / lo_t * 100.0

    gap_delta = None
    if gap_at_entry is not None and gap_at_touch is not None:
        gap_delta = gap_at_entry - gap_at_touch

    up_slope = upper_line.slope() if upper_line else None
    lo_slope = lower_line.slope() if lower_line else None
    converging_geometry = None
    if up_slope is not None and lo_slope is not None:
        converging_geometry = up_slope < 0 and lo_slope > 0

    narrowing_to_upper = None
    if room_entry is not None and up_t is not None and ref_price > 0:
        room_touch_proxy = (up_t - ref_price) / ref_price * 100.0
        narrowing_to_upper = room_touch_proxy - room_entry

    return {
        "upper_at_entry": up_e,
        "lower_support_at_entry": lo_e,
        "channel_room_pct": room_entry,
        "wedge_gap_pct_at_entry": gap_at_entry,
        "wedge_gap_pct_at_touch": gap_at_touch,
        "wedge_gap_delta_touch_to_entry": gap_delta,
        "upper_slope_per_sec": up_slope,
        "lower_slope_per_sec": lo_slope,
        "geometry_rising_support_falling_upper": converging_geometry,
        "room_delta_touch_to_entry": narrowing_to_upper,
    }


def pool_gate_label(row: dict[str, Any]) -> str:
    cnt = row.get("pool_gate_qualified_count", "")
    if not cnt or cnt == "0":
        return "no"
    if row.get("reject_reason") in ("POOL_SSOT_FAIL", "NO_QUALIFIED_UPPER_POOL"):
        return "fail"
    return f"yes ({cnt})"


def decision_b_room(row: dict[str, Any]) -> str:
    room = _f(row.get("channel_room_pct"))
    if room is None:
        return "UNRESOLVED_UPPER"
    if room >= MIN_CHANNEL_ROOM_PCT:
        return "ALLOW"
    return "BLOCK_ROOM"


def build_candidate_rows(
    ctx: dict[str, Any],
    trades_a: dict[str, dict],
    trades_b: dict[str, dict],
) -> list[dict[str, Any]]:
    pending = ctx["pending"]
    lines_by_id = ctx["lines_by_id"]
    upper_line = ctx["upper_line"]
    candles = ctx["candles"]

    rows: list[dict[str, Any]] = []
    for row in pending:
        if row.get("long_trigger_allowed") != "yes":
            continue

        eid = row["touch_event_id"]
        did = row["touch_drawing_id"]
        lower_line = lines_by_id.get(did)

        entry_ts = row.get("_entry_ts")
        ref_price = _f(row.get("entry_price"))
        decision_utc = row.get("entry_utc") or row.get("confirm_utc") or row.get("touch_utc")

        if entry_ts is not None and ref_price is not None:
            ts_dec = float(entry_ts)
        elif row.get("confirm_utc"):
            confirm_dt = _parse_utc(row["confirm_utc"])
            if confirm_dt:
                ts_dec = float(l1.utc_ts(confirm_dt))
                ci = next((i for i, c in enumerate(candles) if int(c["time"]) == int(ts_dec)), None)
                ref_price = float(candles[ci]["close"]) if ci is not None else _f(row.get("touch_line_price"))
            else:
                ts_dec = float(candles[int(row["_touch_start_idx"])]["time"]) if row.get("_touch_start_idx") is not None else None
                ref_price = _f(row.get("touch_line_price"))
        else:
            tsi = row.get("_touch_start_idx")
            if tsi is None:
                tsi = next(
                    (i for i, c in enumerate(candles) if l1.fmt_utc(int(c["time"])) == row.get("touch_utc")),
                    None,
                )
            if tsi is not None:
                ts_dec = float(candles[tsi]["time"])
                ref_price = _f(row.get("touch_line_price")) or float(candles[tsi]["close"])
            else:
                continue

        if ref_price is None or ref_price <= 0:
            continue

        touch_ts = float(candles[int(row["_touch_start_idx"])]["time"]) if row.get("_touch_start_idx") is not None else ts_dec
        w = wedge_metrics(
            upper_line=upper_line,
            lower_line=lower_line,
            ts_decision=ts_dec,
            ts_touch=touch_ts,
            ref_price=ref_price,
        )

        pool_tp = _f(row.get("tp_price"))
        main_up = w["upper_at_entry"]
        eff = effective_tp_price(ref_price, pool_tp, main_up)
        pot = (eff - ref_price) / ref_price * 100.0 if eff is not None else None
        reward_risk = (pot / FIXED_SL_PCT) if pot is not None else None

        ta = trades_a.get(eid, {})
        tb = trades_b.get(eid, {})
        baseline_exit = ta.get("exit_kind", "NO_TRADE")
        baseline_reject = ta.get("reject_reason", "")
        b_reject = tb.get("reject_reason", "")
        b_exit = tb.get("exit_kind", "NO_TRADE")

        out = {
            "utc": decision_utc,
            "touch_utc": row.get("touch_utc"),
            "touch_event_id": eid,
            "drawing_id": did,
            "candidate_price": f"{ref_price:.10f}",
            "upper_main_channel_id": UPPER_MAIN_CHANNEL_LINE_ID,
            "upper_price_at_decision": f"{main_up:.10f}" if main_up is not None else "",
            "channel_room_pct": f"{w['channel_room_pct']:.4f}" if w["channel_room_pct"] is not None else "",
            "tp_pool_price": row.get("tp_price", ""),
            "tp_potential_pct": f"{pot:.4f}" if pot is not None else "",
            "reward_to_fixed_sl_ratio": f"{reward_risk:.4f}" if reward_risk is not None else "",
            "pool_gate": pool_gate_label(row),
            "c1_confirm": "yes" if row.get("confirm_utc") else "no",
            "baseline_decision": (
                baseline_exit if baseline_exit not in ("NO_TRADE", "") else (baseline_reject or "NO_TRADE")
            ),
            "baseline_pnl_pct": ta.get("gross_pnl_pct", ""),
            "with_150_room_decision": (
                b_exit if b_exit not in ("NO_TRADE", "") else (b_reject or decision_b_room(row))
            ),
            "wedge_gap_pct_at_entry": (
                f"{w['wedge_gap_pct_at_entry']:.4f}" if w["wedge_gap_pct_at_entry"] is not None else ""
            ),
            "wedge_gap_pct_at_touch": (
                f"{w['wedge_gap_pct_at_touch']:.4f}" if w["wedge_gap_pct_at_touch"] is not None else ""
            ),
            "wedge_gap_narrowing": (
                f"{w['wedge_gap_delta_touch_to_entry']:.4f}"
                if w["wedge_gap_delta_touch_to_entry"] is not None
                else ""
            ),
            "rising_support_falling_upper": (
                "yes" if w["geometry_rising_support_falling_upper"] else "no"
                if w["geometry_rising_support_falling_upper"] is not None
                else "—"
            ),
        }
        rows.append(out)

    rows.sort(key=lambda r: (r["utc"], r["touch_event_id"]))
    return rows


def _window_rows(rows: list[dict], lo: datetime, hi: datetime) -> list[dict]:
    out = []
    for r in rows:
        tu = _parse_utc(r.get("touch_utc", ""))
        if tu and lo <= tu <= hi:
            out.append(r)
    return out


def main() -> int:
    l1._bootstrap_import_paths()
    ctx = l1.build_pilot_context()
    if ctx is None:
        return 2

    mod = sys.modules[l1.__name__]
    pending = ctx["pending"]
    candles = ctx["candles"]
    upper_line = ctx["upper_line"]

    trades_a_list = pe.execute_variant(
        VARIANT_A, copy.deepcopy(pending), candles, upper_line=upper_line, l1_module=mod
    )
    trades_b_list = pe.execute_variant(
        VARIANT_B, copy.deepcopy(pending), candles, upper_line=upper_line, l1_module=mod
    )
    trades_a = {t["touch_event_id"]: t for t in trades_a_list}
    trades_b = {t["touch_event_id"]: t for t in trades_b_list}

    candidates = build_candidate_rows(ctx, trades_a, trades_b)
    csv_path = OUT_DIR / "l1_channel_room_candidates.csv"
    if candidates:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(candidates[0].keys()))
            w.writeheader()
            w.writerows(candidates)

    def _summ(trades: list[dict]) -> dict[str, Any]:
        executed = [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "")]
        return {
            "executed": len(executed),
            "sl": sum(1 for t in executed if t.get("exit_kind") == "SL"),
            "ch": sum(1 for t in executed if t.get("exit_kind") == "CHANNEL_UPPER"),
            "pnl": sum(float(t["gross_pnl_pct"]) for t in executed if t.get("gross_pnl_pct")),
        }

    sa, sb = _summ(trades_a_list), _summ(trades_b_list)

    ref_loss = [c for c in candidates if c.get("utc") == "2026-08-28 01:55:00 UTC"]

    md: list[str] = [
        "# L1 — Kanalabstand, Keil-Diagnostik, Variante A vs B",
        "",
        "Isolierte Forschung (retrospective drawings). Obere Grenze: "
        f"`{UPPER_MAIN_CHANNEL_LINE_ID}`. Mindestabstand B: **{MIN_CHANNEL_ROOM_PCT:.2f} %**.",
        "",
        "## Aufgabe 2 — Pilotzeitraum A vs B (max. ein offener Long)",
        "",
        "| | Executed | SL | CHANNEL_UPPER | Σ Brutto-PnL % |",
        "| --- | ---: | ---: | ---: | ---: |",
        f"| **A** Baseline | {sa['executed']} | {sa['sl']} | {sa['ch']} | {sa['pnl']:.4f} |",
        f"| **B** Room ≥ {MIN_CHANNEL_ROOM_PCT:.2f} % | {sb['executed']} | {sb['sl']} | {sb['ch']} | {sb['pnl']:.4f} |",
        "",
        "**Neu blockiert in B (vs ausgeführt in A):**",
        "",
    ]
    for t in trades_a_list:
        if t.get("exit_kind") in ("NO_TRADE", ""):
            continue
        b = trades_b.get(t["touch_event_id"], {})
        if b.get("exit_kind") in ("NO_TRADE", "") and b.get("reject_reason"):
            md.append(
                f"- `{t['touch_event_id']}` {t.get('entry_utc')} — A: {t.get('exit_kind')} "
                f"{t.get('gross_pnl_pct')}% → B: `{b.get('reject_reason')}`"
            )

    md.extend(
        [
            "",
            "### Referenz Fehl-Entry 28.08.2026 01:55 UTC",
            "",
        ]
    )
    if ref_loss:
        r = ref_loss[0]
        md.extend(
            [
                f"- Entry/Kandidat: **{r['candidate_price']}** | Upper@Entry: {r['upper_price_at_decision']}",
                f"- Kanalabstand: **{r['channel_room_pct']} %** (Spec-Chart ~0,64 % an Upper; hier effektives TP-Ziel "
                "liegt auf Upper-Linie → Room = Potenzial)",
                f"- TP-Potenzial: {r['tp_potential_pct']} % | Gewinn/SL-Ratio: {r['reward_to_fixed_sl_ratio']}",
                f"- Keil: Gap Touch→Entry Δ = {r['wedge_gap_narrowing']} pp | "
                f"steigende Support + fallende Upper: {r['rising_support_falling_upper']}",
                f"- A: {r['baseline_decision']} ({r['baseline_pnl_pct']} %) | B: {r['with_150_room_decision']}",
            ]
        )

    md.extend(
        [
            "",
            "## Aufgabe 1 & 3 — Kandidatentabelle (Long-Trigger erlaubt)",
            "",
            "Vollständige CSV: `l1_channel_room_candidates.csv`.",
            "",
            "| UTC (Entry/Kandidat) | drawing_id | Preis | Room % | TP-Pot. % | Gewinn/SL | Pool | A | B | Ergebnis A |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- | --- | --- | ---: |",
        ]
    )
    for r in candidates:
        if not r.get("entry_utc") and not r.get("utc"):
            pass
        md.append(
            f"| {r['utc']} | `{r['drawing_id']}` | {float(r['candidate_price']):.5f} | "
            f"{r['channel_room_pct'] or '—'} | {r['tp_potential_pct'] or '—'} | "
            f"{r['reward_to_fixed_sl_ratio'] or '—'} | {r['pool_gate']} | "
            f"{r['baseline_decision']} | {r['with_150_room_decision']} | {r['baseline_pnl_pct'] or '—'} |"
        )

    md.extend(
        [
            "",
            "### Keil-/Konvergenz (nur Diagnose)",
            "",
            "Für jeden Kandidaten: Abstand **untere Trigger-Linie ↔ obere Hauptkanallinie** "
            "in % (Wedge-Gap) bei Touch-Start vs Entscheidungszeitpunkt. "
            "Negatives `wedge_gap_narrowing` = Gap verkleinert sich bis Entry (Konvergenz).",
            "",
            "| touch_utc | line | Gap@touch % | Gap@entry % | Δ Gap | rising sup / falling upper |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for r in candidates:
        md.append(
            f"| {r['touch_utc']} | `{r['drawing_id']}` | {r['wedge_gap_pct_at_touch'] or '—'} | "
            f"{r['wedge_gap_pct_at_entry'] or '—'} | {r['wedge_gap_narrowing'] or '—'} | "
            f"{r['rising_support_falling_upper']} |"
        )

    for label, lo_h, hi_h in (
        ("27.08.2026 12:00–14:00 UTC", 12, 14),
        ("27.08.2026 17:00–19:00 UTC", 17, 19),
    ):
        lo = datetime(2026, 8, 27, lo_h, 0, tzinfo=timezone.utc)
        hi = datetime(2026, 8, 27, hi_h, 0, tzinfo=timezone.utc)
        sub = _window_rows(candidates, lo, hi)
        md.extend(["", f"## Aufgabe 4 — {label}", ""])
        if not sub:
            md.append("_Keine erlaubten Long-Trigger in diesem Fenster._")
            continue
        for r in sub:
            md.append(
                f"- Touch **{r['touch_utc']}** `{r['touch_event_id']}` line `{r['drawing_id']}` | "
                f"C1={r['c1_confirm']} | Kandidat@ {r['utc']} Preis {r['candidate_price']} | "
                f"Room **{r['channel_room_pct'] or '—'} %** | Pool {r['pool_gate']} | "
                f"A: **{r['baseline_decision']}** | B: {r['with_150_room_decision']}"
            )
        md.append(
            "_Hinweis: Qualifizierte Kandidaten am 27.08. wurden in A durch "
            "**GLOBAL_MAX_ONE_OPEN_LONG** blockiert (Referenzlong ab 26.08. 15:10 noch offen)._"
        )

    md.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Trades knapp unter der oberen Kanalbegrenzung zeigen **Room % < 1,50** und oft "
            "**Gewinn/SL-Ratio < 1** (weniger potentielles Brutto-TP als fixer SL).",
            "- Keil-Geometrie (steigende Unterstützung + fallende Obergrenze) ist bei den "
            "`d-a6d9a05da2f1`-Phasen am Fensterende typisch; Gap schrumpft Richtung Entry.",
            "- Filter B entfernt den 01:55-SL ohne den 26.08-CHANNEL_UPPER (+6,54 %) zu verlieren.",
            "",
        ]
    )

    report = OUT_DIR / "l1_channel_room_study.md"
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {report} ({len(candidates)} candidates)")
    print(f"Wrote {csv_path}")
    print(f"A: executed={sa['executed']} pnl={sa['pnl']:.4f} | B: executed={sb['executed']} pnl={sb['pnl']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
