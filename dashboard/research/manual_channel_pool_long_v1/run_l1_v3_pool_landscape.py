#!/usr/bin/env python3
"""L1 V3 — upper pool landscape diagnostics + comparison (baseline B vs B+POOL)."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
ROOT = PROBE_DIR.parents[1]
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import pilot_engine as pe
import run_l1_pilot as l1
from config import (
    L1_V3_VARIANT_A,
    L1_V3_VARIANT_B,
    MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT,
    MIN_BANDS_PER_SUBSTANTIAL_ZONE,
    MIN_SUBSTANTIAL_UPPER_ZONES,
    PILOT_ALLOWED_LOWER_TREND_IDS,
    UPPER_MAIN_CHANNEL_LINE_ID,
)

OUT_DIR = PROBE_DIR


def _drawings_manifest() -> dict[str, Any]:
    text = DRAWINGS_PATH.read_text(encoding="utf-8") if DRAWINGS_PATH.is_file() else ""
    payload = json.loads(text) if text else {}
    ids = [str(d.get("drawing_id") or d.get("id") or "") for d in payload.get("drawings") or []]
    allow = sorted(PILOT_ALLOWED_LOWER_TREND_IDS)
    return {
        "path": str(DRAWINGS_PATH),
        "sha256": hashlib.sha256(text.encode()).hexdigest() if text else "",
        "drawing_count": len(payload.get("drawings") or []),
        "allowlist_present": {d: d in ids for d in allow},
        "upper_main_present": UPPER_MAIN_CHANNEL_LINE_ID in ids,
        "note": "Live drawings.json — not modified by this run.",
    }


def _refs(pending: list[dict]) -> tuple[dict | None, dict | None]:
    loss = next((r for r in pending if r.get("entry_utc") == "2026-08-25 21:15:00 UTC"), None)
    win = next((r for r in pending if r.get("entry_utc") == "2026-08-26 15:10:00 UTC"), None)
    return loss, win


def _executed(trades: list[dict]) -> list[dict]:
    return sorted(
        [t for t in trades if t.get("exit_kind") not in ("NO_TRADE", "")],
        key=lambda t: t.get("entry_utc", ""),
    )


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted({k for r in rows for k in r})
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    l1._bootstrap_import_paths()
    manifest = _drawings_manifest()
    ctx = l1.build_pilot_context()
    if ctx is None:
        return 2

    mod = sys.modules[l1.__name__]
    pending = ctx["pending"]
    tr_a = pe.execute_variant(
        L1_V3_VARIANT_A,
        copy.deepcopy(pending),
        ctx["candles"],
        upper_line=ctx["upper_line"],
        l1_module=mod,
    )
    tr_b = pe.execute_variant(
        L1_V3_VARIANT_B,
        copy.deepcopy(pending),
        ctx["candles"],
        upper_line=ctx["upper_line"],
        l1_module=mod,
    )

    loss_ref, win_ref = _refs(pending)
    candidates = [
        {k: v for k, v in r.items() if not str(k).startswith("_")}
        for r in pending
        if r.get("long_trigger_allowed") == "yes"
    ]
    _write_csv(OUT_DIR / "l1_v3_pool_candidates.csv", candidates)
    _write_csv(OUT_DIR / "l1_v3_variant_a_trades.csv", tr_a)
    _write_csv(OUT_DIR / "l1_v3_variant_b_trades.csv", tr_b)

    sa = pe.summarize_variant(tr_a, ctx["touch_stats"])
    sb = pe.summarize_variant(tr_b, ctx["touch_stats"])

    pool_rejects_b = sum(
        1
        for t in tr_b
        if (t.get("reject_reason") or "").startswith(
            ("FIRST_", "NEAREST_", "INSUFFICIENT_", "NO_SUBSTANTIAL", "UNRESOLVED_POOL")
        )
    )

    md: list[str] = [
        "# L1 V3 — Upper-D2-Pool-Landschaft",
        "",
        "## Reproduzierbarkeit / Zeichnungen",
        "",
        f"- Pfad: `{manifest['path']}`",
        f"- SHA256: `{manifest['sha256']}`",
        f"- Zeichnungen: {manifest['drawing_count']}",
        f"- Obere Kanallinie `{UPPER_MAIN_CHANNEL_LINE_ID}` vorhanden: {manifest['upper_main_present']}",
        "",
        "**Allowlist in drawings.json:**",
        "",
    ]
    for did, ok in manifest["allowlist_present"].items():
        flag = "ja" if ok else "**NEIN (keine Touches möglich)**"
        md.append(f"- `{did}`: {flag}")
    md.extend(
        [
            "",
            "Gesicherter früherer 4-Trade-Lauf (`l1_variant_B_trades_20261108T074319Z.bak`) setzte "
            "`d-1ff495203465` voraus — in der aktuellen drawings.json **fehlt** diese Linie.",
            "",
            "## Datenquelle Pool-SSOT",
            "",
            "Identisch zum L1-Gate: `load_historical_pool_state_at_close`, **DOGEUSDT 5m PD1**, Snapshot am "
            "Confirm-Bar-Close. Katalog-Member: `micro` upper bands mit lower/upper_bound, strength.",
            "Kanonische **Exact Unique-Origin D2-Zähler** sind in den ACTIVE-Zeilen hier **nicht** exponiert; "
            "Proxy: Anzahl distinkter `pool_id`-Origin-Slots in qualifizierten Bändern.",
            "",
            "## Forschungsfilter (vorläufig)",
            "",
            f"- `MIN_BANDS_PER_SUBSTANTIAL_ZONE` = {MIN_BANDS_PER_SUBSTANTIAL_ZONE}",
            f"- `MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT` = {MAX_DISTANCE_TO_FIRST_SUBSTANTIAL_UPPER_PCT:.2f} %",
            f"- `MIN_SUBSTANTIAL_UPPER_ZONES` = {MIN_SUBSTANTIAL_UPPER_ZONES}",
            "",
            "## A — Referenzfälle Pool-Landschaft",
            "",
        ]
    )

    def ref_block(label: str, row: dict | None) -> list[str]:
        if not row:
            return [f"### {label}", "_Kein Kandidat im aktuellen Lauf._", ""]
        return [
            f"### {label}",
            f"- Entry: {row.get('entry_price')} | Linie: `{row.get('touch_drawing_id')}`",
            f"- SSOT-Close: {row.get('pool_snapshot_close_utc')}",
            f"- ACTIVE upper: {row.get('pool_active_upper_count')} | Gate-qualifiziert: {row.get('pool_gate_qualified_count')}",
            f"- Zonen / substanziell / dünn: {row.get('pool_merged_zones')} / {row.get('pool_substantial_zones')} / {row.get('pool_thin_zones')}",
            f"- Nächste substanzielle Zone: {row.get('pool_nearest_substantial_dist_pct')} % | Bänder in Zone: {row.get('pool_nearest_zone_band_count')}",
            f"- Origin-Slots (Proxy): {row.get('pool_unique_origin_slots')} | substanziell im Kanal: {row.get('pool_substantial_in_channel')}",
            f"- Kanalraum: {row.get('channel_room_pct')} %",
            f"- Landschaftsfilter: {row.get('pool_landscape_pass')} — `{row.get('pool_landscape_reason') or 'PASS'}`",
            "",
        ]

    md.extend(ref_block("Referenz 1 — Verlust 25.08.2026 21:15 UTC", loss_ref))
    md.extend(ref_block("Referenz 2 — Gewinn 26.08.2026 15:10 UTC", win_ref))

    distinguishable = (
        loss_ref
        and win_ref
        and loss_ref.get("pool_landscape_pass") == "no"
        and win_ref.get("pool_landscape_pass") == "yes"
    )
    md.append(
        f"**Unterscheidbarkeit (Messwerte):** "
        + ("ja — Verlust abgelehnt, Gewinn bestanden." if distinguishable else "eingeschränkt / siehe Tabelle.")
    )
    md.append("")

    md.extend(
        [
            "## B — Vergleich Variante A vs B+POOL",
            "",
            "| | Executed | SL | Pool TP | Channel TP | Open | Σ Brutto % |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
            f"| **A** (bisherige B, Room 1,50 %) | {sa['executed']} | {sa['sl']} | {sa['tp_pool']} | {sa['tp_channel']} | {sa['open_end']} | {sa['pnl_sum']:.4f} |",
            f"| **B+POOL** | {sb['executed']} | {sb['sl']} | {sb['tp_pool']} | {sb['tp_channel']} | {sb['open_end']} | {sb['pnl_sum']:.4f} |",
            "",
            f"- Kandidaten durch Pool-Landschaftsfilter (B+POOL): **{pool_rejects_b}**",
            "",
            "### C — Gewinn-Trades A",
            "",
        ]
    )

    def trade_tables(trades: list[dict], title: str) -> list[str]:
        ex = _executed(trades)
        wins = [t for t in ex if t.get("exit_kind") in ("TP", "CHANNEL_UPPER")]
        losses = [t for t in ex if t.get("exit_kind") == "SL"]
        lines = [f"### {title}", ""]
        if wins:
            lines.append("| Entry UTC | Entry | SL | TP@Entry | Exit | Exit UTC | Typ | Line | PnL % |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | ---: |")
            for t in wins:
                lines.append(
                    f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                    f"| {t['exit_price']} | {t['exit_utc']} | {t['exit_kind']} | `{t['touch_drawing_id']}` | {t['gross_pnl_pct']} |"
                )
        else:
            lines.append("_Keine._")
        lines.extend(["", f"### Stop-Loss {title}", ""])
        if losses:
            lines.append("| Entry UTC | Entry | SL | TP@Entry | Exit | SL UTC | Line | PnL % |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- | ---: |")
            for t in losses:
                lines.append(
                    f"| {t['entry_utc']} | {t['entry_price']} | {t['sl_price']} | {t['tp_price']} "
                    f"| {t['exit_price']} | {t['exit_utc']} | `{t['touch_drawing_id']}` | {t['gross_pnl_pct']} |"
                )
        else:
            lines.append("_Keine._")
        lines.append("")
        return lines

    md.extend(trade_tables(tr_a, "Variante A"))
    md.extend(trade_tables(tr_b, "Variante B+POOL"))

    md.extend(
        [
            "## D — Alle Long-Trigger-Kandidaten (Auszug Metriken)",
            "",
            "Vollständige CSV: `l1_v3_pool_candidates.csv`.",
            "",
            "| Entry UTC | line | Room % | qual bands | subst zones | near dist % | near bands | Pool pass | A exit | B+ exit |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
        ]
    )
    by_id_a = {t["touch_event_id"]: t for t in tr_a}
    by_id_b = {t["touch_event_id"]: t for t in tr_b}
    for r in candidates:
        if not r.get("entry_utc"):
            continue
        ta = by_id_a.get(r["touch_event_id"], {})
        tb = by_id_b.get(r["touch_event_id"], {})
        md.append(
            f"| {r.get('entry_utc')} | `{r.get('touch_drawing_id')}` | {r.get('channel_room_pct','')} | "
            f"{r.get('pool_qualified_bands','')} | {r.get('pool_substantial_zones','')} | "
            f"{r.get('pool_nearest_substantial_dist_pct','')} | {r.get('pool_nearest_zone_band_count','')} | "
            f"{r.get('pool_landscape_pass')} | {ta.get('exit_kind','')} | {tb.get('exit_kind','')} |"
        )

    report = OUT_DIR / "l1_v3_pool_landscape.md"
    report.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"Wrote {report}")
    print(f"A executed={sa['executed']} pnl={sa['pnl_sum']:.4f}")
    print(f"B+POOL executed={sb['executed']} pnl={sb['pnl_sum']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
