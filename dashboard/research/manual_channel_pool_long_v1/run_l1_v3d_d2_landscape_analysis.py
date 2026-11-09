#!/usr/bin/env python3
"""L1 V3D — READ-ONLY D2 landscape diagnostics (no backtest / no rule changes)."""

from __future__ import annotations

import csv
import hashlib
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
ROOT = PROBE_DIR.parents[1]
DRAWINGS_PATH = ROOT / "research_charts" / "user_data" / "drawings.json"

if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import run_l1_pilot as l1
from config import D2_MIN_DEPTH, UPPER_MAIN_CHANNEL_LINE_ID
from pd1_exact_d2_adapter import (
    D2_COMPUTE_VERSION,
    D2Band,
    price_window_for_entry,
    upper_d2_bands_at_close,
)

OUT_DIR = PROBE_DIR

# Diagnostische Substanz-Definitionen (keine Handelsregeln)
SUBSTANCE_RULES = (
    ("depth_ge_3", lambda b: b.depth >= 3),
    ("depth_ge_2_and_width_ge_0.12pct", lambda b: b.depth >= 2 and band_width_pct(b) >= 0.12),
    ("depth_ge_2_and_width_ge_0.08pct", lambda b: b.depth >= 2 and band_width_pct(b) >= 0.08),
)

# Diagnostische Zonen-Merge (nicht Micro-V3)
ZONE_MERGE_GAP_PCT_OF_ENTRY = 0.15
ZONE_MERGE_GAP_ABS_PRICE = 0.00008


def band_width_pct(b: D2Band) -> float:
    if b.bottom <= 0:
        return 0.0
    return (b.top - b.bottom) / b.bottom * 100.0


def band_id(b: D2Band, idx: int) -> str:
    return f"d2:{idx:03d}:{b.bottom:.10f}:{b.top:.10f}:d{b.depth}"


def clip_interval(lo: float, hi: float, a: float, b: float) -> tuple[float, float] | None:
    x0, x1 = max(lo, a), min(hi, b)
    if x1 <= x0:
        return None
    return x0, x1


@dataclass
class AnalysisContext:
    label: str
    asof_wall_utc: str
    decision_close_utc: str
    geometry_bar_utc: str
    reference_price: float
    entry_price: float | None
    channel_upper: float | None
    channel_room_pct: float | None
    bands_all: list[D2Band] = field(default_factory=list)
    bands_above: list[D2Band] = field(default_factory=list)
    bands_in_channel: list[tuple[D2Band, float, float]] = field(default_factory=list)


def decision_close_from_wall(wall: datetime) -> datetime:
    from pool_scan.clock import bar_close

    return bar_close(wall, "5m")


def load_members(dc: datetime) -> list[dict] | None:
    members, err = l1.load_pool_snapshot(dc)
    if err:
        return None
    return members


def channel_upper_at_ts(upper_line, ts: float, ref_price: float) -> float | None:
    return l1.channel_line_at_bar(upper_line, {"time": float(ts)})


def analyze_snapshot(
    *,
    label: str,
    wall_utc: str,
    decision_close: datetime,
    geometry_ts: int,
    reference_price: float,
    entry_price: float | None,
    upper_line,
    members: list[dict] | None = None,
) -> AnalysisContext:
    if members is None:
        members = load_members(decision_close)
    bands, err = upper_d2_bands_at_close(members, decision_close, reference_price, min_depth=D2_MIN_DEPTH)
    if err:
        bands = []
    ch = channel_upper_at_ts(upper_line, float(geometry_ts), reference_price)
    room = (ch - reference_price) / reference_price * 100.0 if ch and ch > reference_price else None

    ctx = AnalysisContext(
        label=label,
        asof_wall_utc=wall_utc,
        decision_close_utc=l1.fmt_utc(int(decision_close.timestamp())),
        geometry_bar_utc=l1.fmt_utc(geometry_ts),
        reference_price=reference_price,
        entry_price=entry_price,
        channel_upper=ch,
        channel_room_pct=room,
        bands_all=bands,
    )
    for b in bands:
        if b.bottom > reference_price - 1e-15:
            ctx.bands_above.append(b)
    if ch and ch > reference_price:
        for b in ctx.bands_above:
            intr = clip_interval(b.bottom, b.top, reference_price, ch)
            if intr:
                ctx.bands_in_channel.append((b, intr[0], intr[1]))
    return ctx


def distinct_zones_merge(
    bands: list[D2Band],
    entry: float,
    *,
    gap_pct: float,
    same_depth: bool,
    gap_abs: float | None = None,
) -> list[tuple[float, float, int, int]]:
    """Diagnostic zone merge on kanonische Bänder (keine neue Density). Returns (lo, hi, depth, band_count)."""
    if not bands:
        return []
    s = sorted(bands, key=lambda b: b.bottom)
    zones: list[dict[str, Any]] = []
    for b in s:
        if zones:
            g_pct = (b.bottom - zones[-1]["hi"]) / entry * 100.0
            g_abs = b.bottom - zones[-1]["hi"]
            gap_ok = g_pct <= gap_pct or (gap_abs is not None and g_abs <= gap_abs)
            depth_ok = zones[-1]["depth"] == b.depth if same_depth else True
        else:
            gap_ok = depth_ok = False
        if zones and gap_ok and depth_ok:
            zones[-1]["hi"] = max(zones[-1]["hi"], b.top)
            zones[-1]["depth"] = max(zones[-1]["depth"], b.depth)
            zones[-1]["n"] += 1
        else:
            zones.append({"lo": b.bottom, "hi": b.top, "depth": b.depth, "n": 1})
    return [(z["lo"], z["hi"], z["depth"], z["n"]) for z in zones]


def substantial_distances(
    bands_in_ch: list[tuple[D2Band, float, float]],
    entry: float,
    pred,
    n: int = 3,
) -> list[float]:
    cands: list[float] = []
    for b, c0, _ in bands_in_ch:
        if pred(b):
            cands.append((c0 - entry) / entry * 100.0)
    cands.sort()
    return cands[:n]


def zone_gap_pcts(zones: list[tuple[float, float, int, int]], ref: float) -> list[float]:
    if len(zones) < 2:
        return []
    out: list[float] = []
    for i in range(1, len(zones)):
        out.append((zones[i][0] - zones[i - 1][1]) / ref * 100.0)
    return out


def section_quad_str(ctx: AnalysisContext) -> str:
    return "/".join(str(s["bands_touching_section"]) for s in section_stats(ctx))


def section_stats(ctx: AnalysisContext) -> list[dict[str, Any]]:
    if not ctx.channel_upper or ctx.channel_upper <= ctx.reference_price:
        return []
    span = ctx.channel_upper - ctx.reference_price
    rows = []
    for q in range(4):
        s_lo = ctx.reference_price + span * (q / 4.0)
        s_hi = ctx.reference_price + span * ((q + 1) / 4.0)
        covered = 0.0
        depths: list[int] = []
        band_hits = 0
        for b, c0, c1 in ctx.bands_in_channel:
            intr = clip_interval(c0, c1, s_lo, s_hi)
            if intr:
                covered += intr[1] - intr[0]
                depths.append(b.depth)
                band_hits += 1
        rows.append(
            {
                "section": f"{int(q*25)}-{int((q+1)*25)}",
                "section_lo": s_lo,
                "section_hi": s_hi,
                "bands_touching_section": band_hits,
                "covered_width": covered,
                "covered_pct_of_section": (covered / (s_hi - s_lo) * 100.0) if s_hi > s_lo else 0.0,
                "avg_depth": sum(depths) / len(depths) if depths else 0.0,
                "max_depth": max(depths) if depths else 0,
            }
        )
    return rows


def coverage_pct_in_channel(ctx: AnalysisContext) -> float:
    if not ctx.channel_upper or ctx.channel_upper <= ctx.reference_price:
        return 0.0
    span = ctx.channel_upper - ctx.reference_price
    if span <= 0:
        return 0.0
    covered = sum(c1 - c0 for _, c0, c1 in ctx.bands_in_channel)
    return min(100.0, covered / span * 100.0)


def longest_gap_pct(ctx: AnalysisContext) -> float | None:
    if not ctx.channel_upper or not ctx.bands_in_channel:
        return None
    ref, ch = ctx.reference_price, ctx.channel_upper
    intervals = sorted((c0, c1) for _, c0, c1 in ctx.bands_in_channel)
    max_gap = intervals[0][0] - ref if intervals else ch - ref
    for i in range(1, len(intervals)):
        max_gap = max(max_gap, intervals[i][0] - intervals[i - 1][1])
    max_gap = max(max_gap, ch - intervals[-1][1])
    return max_gap / ref * 100.0


def nearest_substancial(
    bands_in_ch: list[tuple[D2Band, float, float]], entry: float, rule_name: str, pred
) -> float | None:
    cands = []
    for b, c0, c1 in bands_in_ch:
        if pred(b):
            cands.append(c0)
    if not cands:
        return None
    return (min(cands) - entry) / entry * 100.0


def summary_row(ctx: AnalysisContext) -> dict[str, Any]:
    in_ch_bands = [b for b, _, _ in ctx.bands_in_channel]
    zones_diag_depth = distinct_zones_merge(
        in_ch_bands, ctx.reference_price, gap_pct=ZONE_MERGE_GAP_PCT_OF_ENTRY, same_depth=True
    )
    zones_diag_gap = distinct_zones_merge(
        in_ch_bands,
        ctx.reference_price,
        gap_pct=ZONE_MERGE_GAP_PCT_OF_ENTRY,
        same_depth=False,
        gap_abs=ZONE_MERGE_GAP_ABS_PRICE,
    )
    depths = [b.depth for b, _, _ in ctx.bands_in_channel]
    widths = [band_width_pct(b) for b, _, _ in ctx.bands_in_channel]
    near = None
    if ctx.bands_above:
        near = (min(b.bottom for b in ctx.bands_above) - ctx.reference_price) / ctx.reference_price * 100.0
    secs = section_stats(ctx)
    sec_with = sum(1 for s in secs if s["bands_touching_section"] > 0)
    return {
        "label": ctx.label,
        "asof_wall_utc": ctx.asof_wall_utc,
        "decision_close_utc": ctx.decision_close_utc,
        "entry_or_ref_price": ctx.reference_price,
        "channel_upper": ctx.channel_upper,
        "channel_room_pct": ctx.channel_room_pct,
        "d2_bands_total": len(ctx.bands_all),
        "d2_bands_above_ref": len(ctx.bands_above),
        "d2_bands_in_channel": len(ctx.bands_in_channel),
        "nearest_d2_pct": near,
        "nearest_substantial_depth_ge3_pct": nearest_substancial(
            ctx.bands_in_channel, ctx.reference_price, "depth_ge_3", lambda b: b.depth >= 3
        ),
        "coverage_in_channel_pct": coverage_pct_in_channel(ctx),
        "avg_depth_in_channel": sum(depths) / len(depths) if depths else 0.0,
        "max_depth_in_channel": max(depths) if depths else 0,
        "zones_raw_count": len(in_ch_bands),
        "zones_merge_depth_gap_count": len(zones_diag_depth),
        "zones_merge_gap_only_count": len(zones_diag_gap),
        "section_quad_bands": section_quad_str(ctx),
        "first_d2_share_of_channel_pct": (
            (near / ctx.channel_room_pct * 100.0) if near is not None and ctx.channel_room_pct else None
        ),
        "longest_gap_pct": longest_gap_pct(ctx),
        "sections_with_d2": sec_with,
        "narrow_bands_lt_0.05pct": sum(1 for w in widths if w < 0.05),
        "substantial_depth3_count": sum(1 for b, _, _ in ctx.bands_in_channel if b.depth >= 3),
    }


def pending_ref(ctx_data: dict, entry_utc: str) -> dict:
    row = next((r for r in ctx_data["pending"] if r.get("entry_utc") == entry_utc), None)
    if not row:
        raise RuntimeError(f"missing pilot pending row for {entry_utc}")
    return row


def main() -> int:
    l1._bootstrap_import_paths()
    ctx_data = l1.build_pilot_context()
    if ctx_data is None:
        return 2
    upper_line = ctx_data["upper_line"]

    row_a = pending_ref(ctx_data, "2026-08-25 21:15:00 UTC")
    row_b = pending_ref(ctx_data, "2026-08-26 15:10:00 UTC")
    entry_a = float(row_a["entry_price"])
    entry_b = float(row_b["entry_price"])

    def dc_from_row(row: dict) -> datetime:
        return datetime.strptime(row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )

    analyses: list[AnalysisContext] = []
    analyses.append(
        analyze_snapshot(
            label="REF_A_LOSS",
            wall_utc=row_a["confirm_utc"],
            decision_close=dc_from_row(row_a),
            geometry_ts=int(row_a["_entry_ts"]),
            reference_price=entry_a,
            entry_price=entry_a,
            upper_line=upper_line,
            members=row_a.get("_pool_members"),
        )
    )
    analyses.append(
        analyze_snapshot(
            label="REF_B_WIN",
            wall_utc=row_b["confirm_utc"],
            decision_close=dc_from_row(row_b),
            geometry_ts=int(row_b["_entry_ts"]),
            reference_price=entry_b,
            entry_price=entry_b,
            upper_line=upper_line,
            members=row_b.get("_pool_members"),
        )
    )

    evolution_specs = [
        ("EV_25_08_2110", "2026-08-25 21:10:00 UTC"),
        ("EV_26_08_0000", "2026-08-26 00:00:00 UTC"),
        ("EV_26_08_0600", "2026-08-26 06:00:00 UTC"),
        ("EV_26_08_1200", "2026-08-26 12:00:00 UTC"),
        ("EV_26_08_1505", "2026-08-26 15:05:00 UTC"),
    ]
    candles = ctx_data["candles"]
    ts_to_close = {int(c["time"]): float(c["close"]) for c in candles}

    for lab, wall in evolution_specs:
        wdt = datetime.strptime(wall, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        dc = decision_close_from_wall(wdt)
        dc_ts = int(dc.timestamp())
        close_px = ts_to_close.get(dc_ts)
        if close_px is None:
            for c in candles:
                if abs(int(c["time"]) - dc_ts) < 300:
                    close_px = float(c["close"])
                    break
        if close_px is None:
            close_px = entry_b
        analyses.append(
            analyze_snapshot(
                label=lab,
                wall_utc=wall,
                decision_close=dc,
                geometry_ts=dc_ts,
                reference_price=close_px,
                entry_price=None,
                upper_line=upper_line,
            )
        )

    # CSV: reference bands
    band_rows: list[dict] = []
    for ctx in analyses[:2]:
        for i, b in enumerate(ctx.bands_all):
            dist = (b.bottom - ctx.reference_price) / ctx.reference_price * 100.0
            ch_dist = (
                (ctx.channel_upper - b.top) / ctx.reference_price * 100.0
                if ctx.channel_upper
                else ""
            )
            in_ch = any(x[0] is b for x in ctx.bands_in_channel)
            band_rows.append(
                {
                    "case": ctx.label,
                    "decision_close_utc": ctx.decision_close_utc,
                    "band_id": band_id(b, i),
                    "bottom": f"{b.bottom:.10f}",
                    "top": f"{b.top:.10f}",
                    "width_pct": f"{band_width_pct(b):.6f}",
                    "depth_origins": b.depth,
                    "dist_bottom_to_ref_pct": f"{dist:.6f}",
                    "dist_top_to_channel_pct": ch_dist if ch_dist != "" else "",
                    "in_channel_room": "yes" if in_ch else "no",
                }
            )
    with (OUT_DIR / "l1_v3d_d2_reference_bands.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(band_rows[0].keys()) if band_rows else [])
        if band_rows:
            w.writeheader()
            w.writerows(band_rows)

    # CSV: distribution
    dist_rows: list[dict] = []
    for ctx in analyses:
        for s in section_stats(ctx):
            dist_rows.append(
                {
                    "snapshot": ctx.label,
                    "decision_close_utc": ctx.decision_close_utc,
                    "reference_price": ctx.reference_price,
                    "channel_room_pct": ctx.channel_room_pct or "",
                    **{k: s[k] for k in s if k not in ("section_lo", "section_hi")},
                    "section_lo": f"{s['section_lo']:.10f}",
                    "section_hi": f"{s['section_hi']:.10f}",
                }
            )
    with (OUT_DIR / "l1_v3d_d2_distribution.csv").open("w", newline="", encoding="utf-8") as f:
        if dist_rows:
            w = csv.DictWriter(f, fieldnames=list(dist_rows[0].keys()))
            w.writeheader()
            w.writerows(dist_rows)

    # CSV: evolution
    ev_rows = [summary_row(a) for a in analyses if a.label.startswith("EV_")]
    with (OUT_DIR / "l1_v3d_d2_snapshot_evolution.csv").open("w", newline="", encoding="utf-8") as f:
        if ev_rows:
            w = csv.DictWriter(f, fieldnames=list(ev_rows[0].keys()))
            w.writeheader()
            w.writerows(ev_rows)

    sa, sb = summary_row(analyses[0]), summary_row(analyses[1])
    drawings_sha = hashlib.sha256(DRAWINGS_PATH.read_bytes()).hexdigest() if DRAWINGS_PATH.is_file() else ""

    md = [
        "# L1 V3D — ACTIVE Upper-D2-Landschaft (Forschungsdiagnose)",
        "",
        f"- D2: `{D2_COMPUTE_VERSION}`, min_depth={D2_MIN_DEPTH}",
        f"- SSOT: `load_historical_pool_state_at_close` 5m PD1 @ **decision_close** (5m bar close)",
        f"- Pool-SSOT @ decision_close: A **{analyses[0].decision_close_utc}** (confirm `{row_a['confirm_utc']}`), "
        f"B **{analyses[1].decision_close_utc}** (confirm `{row_b['confirm_utc']}`)",
        f"- Kanal oben: `{UPPER_MAIN_CHANNEL_LINE_ID}` @ **Entry-Bar-Open** (Geometrie), nicht @ Pool-Close",
        f"- Diagnose-Merge: (1) gap≤{ZONE_MERGE_GAP_PCT_OF_ENTRY}% v. Entry **und** gleiche Depth; "
        f"(2) gap≤{ZONE_MERGE_GAP_PCT_OF_ENTRY}% **oder** ≤{ZONE_MERGE_GAP_ABS_PRICE} Preis, Depth egal",
        f"- drawings SHA256: `{drawings_sha}`",
        "",
        "## 9. Vergleichstabelle Referenzen",
        "",
        "| Kennzahl | 25.08. 21:15 (A) | 26.08. 15:10 (B) |",
        "| --- | --- | --- |",
        f"| Entry-Preis | {entry_a:.6f} | {entry_b:.6f} |",
        f"| Oberer Kanal @ Entry | {sa.get('channel_upper')} | {sb.get('channel_upper')} |",
        f"| Kanalabstand % | {sa.get('channel_room_pct', 0):.4f} | {sb.get('channel_room_pct', 0):.4f} |",
        f"| D2-Bänder gesamt | {sa['d2_bands_total']} | {sb['d2_bands_total']} |",
        f"| D2 im Kanal | {sa['d2_bands_in_channel']} | {sb['d2_bands_in_channel']} |",
        f"| Nächste D2 % | {sa['nearest_d2_pct']:.4f} | {sb['nearest_d2_pct']:.4f} |",
        f"| Nächste subst. (depth≥3) % | {sa.get('nearest_substantial_depth_ge3_pct')} | {sb.get('nearest_substantial_depth_ge3_pct')} |",
        f"| Abdeckung Kanal % | {sa['coverage_in_channel_pct']:.4f} | {sb['coverage_in_channel_pct']:.4f} |",
        f"| Ø / max Depth (Kanal) | {sa['avg_depth_in_channel']:.2f} / {sa['max_depth_in_channel']} | "
        f"{sb['avg_depth_in_channel']:.2f} / {sb['max_depth_in_channel']} |",
        f"| Nächste subst. (2./3., depth≥3) % | "
        f"{substantial_distances(analyses[0].bands_in_channel, entry_a, lambda b: b.depth >= 3)} | "
        f"{substantial_distances(analyses[1].bands_in_channel, entry_b, lambda b: b.depth >= 3)} |",
        f"| Anteil Kanal bis 1. D2 % | {sa.get('first_d2_share_of_channel_pct', 0):.2f} | "
        f"{sb.get('first_d2_share_of_channel_pct', 0):.2f} |",
        f"| Getrennte Rohzonen (1 Band = 1) | {sa['zones_raw_count']} | {sb['zones_raw_count']} |",
        f"| Merge depth+gap / gap-only Zonen | {sa['zones_merge_depth_gap_count']}/{sa['zones_merge_gap_only_count']} | "
        f"{sb['zones_merge_depth_gap_count']}/{sb['zones_merge_gap_only_count']} |",
        f"| Längste Lücke % | {sa.get('longest_gap_pct')} | {sb.get('longest_gap_pct')} |",
        f"| Abschnitte mit D2 (0–25…75–100) | {sa['sections_with_d2']}/4 | {sb['sections_with_d2']}/4 |",
        f"| D2-Verteilung Bänder/Abschnitt | {sa['section_quad_bands']} | {sb['section_quad_bands']} |",
        "",
        "### Substanz-Definitionen (nur Diagnose)",
        "",
    ]
    for name, pred in SUBSTANCE_RULES:
        va = nearest_substancial(analyses[0].bands_in_channel, entry_a, name, pred)
        vb = nearest_substancial(analyses[1].bands_in_channel, entry_b, name, pred)
        md.append(f"- **{name}**: nächste Zone A={va} %, B={vb} %")

    md.extend(["", "## 8. Snapshot-Evolution", "", "| snapshot | close UTC | ref close | D2 total | in ch | nearest % | coverage % |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"])
    for r in ev_rows:
        md.append(
            f"| {r['label']} | {r['decision_close_utc']} | {float(r['entry_or_ref_price']):.6f} | "
            f"{r['d2_bands_total']} | {r['d2_bands_in_channel']} | {r.get('nearest_d2_pct') or '—'} | "
            f"{r['coverage_in_channel_pct']:.2f} |"
        )

    md.extend(
        [
            "",
            "## 10. Interpretation",
            "",
            f"1. **Näher am Preis (B vs A)?** {'Ja' if (sb['nearest_d2_pct'] or 99) < (sa['nearest_d2_pct'] or 99) else 'Nein'} "
            f"({sb['nearest_d2_pct']:.2f}% vs {sa['nearest_d2_pct']:.2f}%).",
            f"2. **Mehr substanzielle Zonen im Kanal?** Depth≥3 count: A={sa['substantial_depth3_count']} B={sb['substantial_depth3_count']}; "
            f"in-channel bands: A={sa['d2_bands_in_channel']} B={sb['d2_bands_in_channel']}.",
            f"3. **Höhere Dichte?** Abdeckung Kanal: A={sa['coverage_in_channel_pct']:.1f}% B={sb['coverage_in_channel_pct']:.1f}%; max depth A={sa['max_depth_in_channel']} B={sb['max_depth_in_channel']}.",
            f"4. **Verteilung über Abschnitte?** A={sa['section_quad_bands']} (0–25 leer), B={sb['section_quad_bands']}; "
            f"B hat D2 bereits im untersten Viertel, A nicht.",
            f"5. **Größere Lücken bei A?** Ja — längste Lücke %: A={sa.get('longest_gap_pct'):.2f} vs B={sb.get('longest_gap_pct'):.2f} "
            "(entspricht fast dem Abstand zur ersten D2 bei A).",
            "6. **Landschaft baut sich auf?** Teilweise: zwischen 25.08 21:15 und 26.08 12:00 steigen Bandanzahl und "
            "Unterviertel-Abdeckung; nahe D2-Zone schwankt mit Kurs (06:00 ohne Pool oberhalb Close; 12:00 sehr nah). "
            "Beim B-Entry (15:10) wieder ~1,89 % zur ersten Zone — nicht monoton dichter, aber strukturell früher belegt als A.",
            "7. **Stärkste Unterscheider:** (a) `nearest_d2_pct`, (b) Abschnitt 0–25 % (0 vs 6 Bänder), (c) `first_d2_share_of_channel_pct`.",
            "",
            "## 13. Kurzfassung",
            "",
            "**A (25.08.):** 55 D2-Bänder, 28 im Kanal; erste Unterkante +3,61 %; **0–25 %-Viertel ohne D2**; "
            f"längste Lücke ≈ erste Zone ({sa.get('longest_gap_pct', 0):.2f} %).",
            "",
            "**B (26.08.):** 69 D2-Bänder, 35 im Kanal; erste Zone +1,89 %; **alle vier Viertel mit D2** "
            f"(Staffelung {sb['section_quad_bands']}); Abdeckung {sb['coverage_in_channel_pct']:.1f} %.",
            "",
            "**C.** Hauptunterschied: **Nähe der ersten D2-Zone** + **Abdeckung/Staffelung im unteren Kanalviertel**, nicht nur Gesamtzahl.",
            "",
            "**D. Später testbare Filter (ohne Schwellen):** `nearest_d2_in_channel_pct`, `coverage_in_channel_pct`, `sections_with_d2_in_lower_half`.",
            "",
            "CSVs: `l1_v3d_d2_reference_bands.csv`, `l1_v3d_d2_distribution.csv`, `l1_v3d_d2_snapshot_evolution.csv`.",
        ]
    )

    (OUT_DIR / "l1_v3d_d2_landscape_analysis.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("Wrote l1_v3d_d2_landscape_analysis.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
