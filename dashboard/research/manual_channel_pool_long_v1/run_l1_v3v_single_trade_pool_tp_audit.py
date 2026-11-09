#!/usr/bin/env python3
"""L1 V3V — READ-ONLY single-trade pool TP audit (2026-08-26 d-a6d9a05da2f1)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROBE_DIR = Path(__file__).resolve().parent
if str(PROBE_DIR) not in sys.path:
    sys.path.insert(0, str(PROBE_DIR))

import run_l1_pilot as l1
from config import MIN_POOL_DIST_PCT, D2_MIN_DEPTH
from pd1_exact_d2_adapter import (
    D2Band,
    compute_exact_bands_for_side,
    members_to_instant_pools,
    origin_logical_v2_key,
    price_window_for_entry,
    qualify_upper_d2_bands,
    upper_d2_bands_at_close,
    _iso_ms,
)
from pool_d2_metrics import analyze_d2_landscape

PREFIX = "l1_v3v"
OUT = PROBE_DIR

ENTRY_UTC = "2026-08-26 15:10:00 UTC"
CONFIRM_UTC = "2026-08-26 15:05:00 UTC"
TOUCH_UTC = "2026-08-26 15:00:00 UTC"
ENTRY = 0.08444
SL = 0.08342672
TP_A = 0.09496
TP_B = 0.0894
TP_C = 0.0900
ZONE_LO, ZONE_HI = 0.0894, 0.0900
SUPPORT_ID = "d-a6d9a05da2f1"
EXIT_SL_UTC = "2026-08-30 22:20:00 UTC"

V3P_SNAP = OUT / "l1_v3p_drawings_snapshot_76f70d9d43b85c27.json"
V3U_SNAP = OUT / "l1_v3u_drawings_snapshot_d05d218646aced84.json"


def _parse_utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _ts(s: str) -> int:
    return int(_parse_utc(s).timestamp())


def _decision_close(wall: datetime) -> datetime:
    from pool_scan.clock import bar_close

    return bar_close(wall, "5m")


def _lines_from_snapshot(path: Path) -> list:
    payload = json.loads(path.read_text(encoding="utf-8"))
    drawings = payload.get("drawings") or payload.get("snapshot", {}).get("drawings")
    if drawings is None and "drawings" in payload:
        drawings = payload["drawings"]
    if isinstance(payload, dict) and "drawings" not in payload and path.name.endswith(".json"):
        # snapshot wraps drawings at top level in our artifacts
        drawings = [d for d in (payload.get("drawings") or [])]
    if not drawings:
        inner = json.loads(path.read_text(encoding="utf-8"))
        drawings = inner.get("drawings") or []
    lines = []
    for d in drawings:
        ln = l1.line_from_drawing(d)
        if ln:
            lines.append(ln)
    return lines


def _active_upper_members_in_zone(
    members: list[dict], dc: datetime, entry: float, zlo: float, zhi: float
) -> list[dict[str, Any]]:
    pools = members_to_instant_pools(members, dc)
    t_ms = _iso_ms(dc)
    y_min, y_max = price_window_for_entry(entry)
    rows: list[dict[str, Any]] = []
    for pl in pools:
        if pl.get("side") != "upper":
            continue
        from pd1_exact_d2_adapter import _pool_active_at

        if not _pool_active_at(pl, t_ms):
            continue
        lo, hi = float(pl["bottom"]), float(pl["top"])
        if hi < zlo or lo > zhi:
            continue
        rows.append(
            {
                "pool_id": pl["pool_id"],
                "origin_id": pl["origin"],
                "lower_bound": lo,
                "upper_bound": hi,
                "overlap_lo": max(lo, zlo),
                "overlap_hi": min(hi, zhi),
            }
        )
    return rows


def _zone_d2_slice(bands: list[D2Band], zlo: float, zhi: float) -> list[D2Band]:
    out = []
    for b in bands:
        if b.top < zlo or b.bottom > zhi:
            continue
        out.append(b)
    return out


def _band_row(b: D2Band, entry: float, *, selected: bool = False, nearest: bool = False) -> dict:
    dist = (b.bottom - entry) / entry * 100.0 if b.bottom > entry else None
    return {
        "lower_bound": f"{b.bottom:.10f}",
        "upper_bound": f"{b.top:.10f}",
        "depth": b.depth,
        "width_pct": round((b.top - b.bottom) / b.bottom * 100.0, 6) if b.bottom > 0 else "",
        "dist_from_entry_pct": round(dist, 6) if dist is not None else "",
        "selected_by_backtester": "yes" if selected else "no",
        "nearest_qualified": "yes" if nearest else "no",
    }


def _replay_exit(
    candles: list[dict],
    entry_ts: int,
    sl: float,
    tp: float | None,
    *,
    exit_cap_ts: int | None = None,
) -> dict[str, Any]:
    start_idx = next(i for i, c in enumerate(candles) if int(c["time"]) == entry_ts)
    ambig = False
    for j in range(start_idx, len(candles)):
        c = candles[j]
        if exit_cap_ts is not None and int(c["time"]) > exit_cap_ts:
            break
        hi, lo = float(c["high"]), float(c["low"])
        hit_sl = lo <= sl
        hit_tp = tp is not None and hi >= tp
        if hit_sl and hit_tp:
            return {
                "tp_reached": "ambiguous",
                "sl_first": "ambiguous",
                "first_exit_utc": l1.fmt_utc(int(c["time"])),
                "exit_price": f"{sl:.10f}",
                "gross_pnl_pct": round((sl - ENTRY) / ENTRY * 100.0, 4),
                "same_bar_ambiguity": "yes",
            }
        if hit_sl:
            px = sl
            return {
                "tp_reached": "no",
                "sl_first": "yes",
                "first_exit_utc": l1.fmt_utc(int(c["time"])),
                "exit_price": f"{px:.10f}",
                "gross_pnl_pct": round((px - ENTRY) / ENTRY * 100.0, 4),
                "same_bar_ambiguity": "no",
            }
        if hit_tp:
            px = tp
            return {
                "tp_reached": "yes",
                "sl_first": "no",
                "first_exit_utc": l1.fmt_utc(int(c["time"])),
                "exit_price": f"{px:.10f}",
                "gross_pnl_pct": round((px - ENTRY) / ENTRY * 100.0, 4),
                "same_bar_ambiguity": "no",
            }
    return {
        "tp_reached": "no",
        "sl_first": "no",
        "first_exit_utc": "",
        "exit_price": "",
        "gross_pnl_pct": "",
        "same_bar_ambiguity": "no",
    }


def _price_path(candles: list[dict], entry_ts: int, end_ts: int) -> tuple[list[dict], dict]:
    start = next(i for i, c in enumerate(candles) if int(c["time"]) == entry_ts)
    end = next(i for i, c in enumerate(candles) if int(c["time"]) == end_ts)
    subset = candles[start : end + 1]
    max_hi, max_ts = -1.0, None
    min_lo, min_ts = 1e9, None
    first_touch_bar = ""
    first_touch_ts: int | None = None
    first_close_inside = ""
    first_reject_close = ""
    prev_inside = False
    for c in subset:
        ts = int(c["time"])
        hi, lo, close = float(c["high"]), float(c["low"]), float(c["close"])
        if hi > max_hi:
            max_hi, max_ts = hi, ts
        if lo < min_lo:
            min_lo, min_ts = lo, ts
        inside = hi >= ZONE_LO and lo <= ZONE_HI
        if inside and not first_touch_bar:
            first_touch_bar = l1.fmt_utc(ts)
            first_touch_ts = ts
        if ZONE_LO <= close <= ZONE_HI and not first_close_inside:
            first_close_inside = l1.fmt_utc(ts)
        now_inside = close >= ZONE_LO and close <= ZONE_HI
        if prev_inside and not now_inside and close < ZONE_LO and not first_reject_close:
            first_reject_close = l1.fmt_utc(ts)
        prev_inside = now_inside
    summary = {
        "max_high": f"{max_hi:.10f}",
        "max_high_utc": l1.fmt_utc(max_ts) if max_ts else "",
        "max_high_ts": max_ts or "",
        "max_favorable_pct": round((max_hi - ENTRY) / ENTRY * 100.0, 4),
        "min_low": f"{min_lo:.10f}",
        "min_low_utc": l1.fmt_utc(min_ts) if min_ts else "",
        "min_low_ts": min_ts or "",
        "first_intrabar_touch_zone_utc": first_touch_bar,
        "first_intrabar_touch_ts": first_touch_ts or "",
        "first_close_in_zone_utc": first_close_inside,
        "first_confirmed_rejection_close_below_zone_utc": first_reject_close,
    }
    rows = []
    for c in subset:
        rows.append(
            {
                "bar_open_utc": l1.fmt_utc(int(c["time"])),
                "open": c["open"],
                "high": c["high"],
                "low": c["low"],
                "close": c["close"],
                "touches_zone_0894_0900": "yes"
                if float(c["high"]) >= ZONE_LO and float(c["low"]) <= ZONE_HI
                else "no",
                "close_inside_zone": "yes"
                if ZONE_LO <= float(c["close"]) <= ZONE_HI
                else "no",
            }
        )
    return rows, summary


def main() -> int:
    l1._bootstrap_import_paths()
    confirm_dt = _parse_utc(CONFIRM_UTC)
    dc = _decision_close(confirm_dt)
    entry_ts = _ts(ENTRY_UTC)
    exit_cap = _ts(EXIT_SL_UTC)

    members, perr = l1.load_pool_snapshot(dc)
    if not members:
        raise SystemExit(f"POOL SSOT failed: {perr}")

    m, all_bands, qualified = analyze_d2_landscape(members, dc, ENTRY, apply_provisional_research_filter=False)
    ql = sorted(qualified, key=lambda x: float(x["lower_bound"]))
    by_max = max(qualified, key=lambda x: float(x["lower_bound"])) if qualified else None
    nearest = ql[0] if ql else None
    pick = l1.pick_tp(ENTRY, qualified, None, None, independent_support=True)

    # V3U channel deckel (exclude support)
    v3u_lines = _lines_from_snapshot(V3U_SNAP)
    v3p_lines = _lines_from_snapshot(V3P_SNAP)
    ch_v3u, id_v3u = l1.channel_upper_at(v3u_lines, entry_ts, ENTRY, exclude_drawing_ids=frozenset({SUPPORT_ID}))
    ch_v3p, id_v3p = l1.channel_upper_at(v3p_lines, entry_ts, ENTRY, exclude_drawing_ids=frozenset({SUPPORT_ID}))
    pick_v3p = l1.pick_tp(ENTRY, qualified, ch_v3p, id_v3p)

    entry_rows = []
    for b in all_bands:
        if b.bottom <= ENTRY:
            continue
        is_q = any(abs(float(q["lower_bound"]) - b.bottom) < 1e-12 for q in qualified)
        if not is_q:
            continue
        entry_rows.append(
            _band_row(
                b,
                ENTRY,
                selected=bool(by_max and abs(b.bottom - float(by_max["lower_bound"])) < 1e-12),
                nearest=bool(nearest and abs(b.bottom - float(nearest["lower_bound"])) < 1e-12),
            )
        )

    zone_members = _active_upper_members_in_zone(members, dc, ENTRY, ZONE_LO, ZONE_HI)
    zone_band = _zone_d2_slice(all_bands, ZONE_LO, ZONE_HI)
    origins = sorted({r["origin_id"] for r in zone_members})

    # nearest qualified below zone
    nearer_substantial = [q for q in ql if float(q["lower_bound"]) < ZONE_LO][:8]

    candles = l1.load_candles(start=_ts("2026-08-20 00:00:00 UTC"), end=_ts("2026-09-05 00:00:00 UTC"))
    path_rows, path_sum = _price_path(candles, entry_ts, exit_cap)

    # timeline snapshots
    timeline_defs: list[tuple[str, datetime]] = [
        ("entry_decision", confirm_dt),
        ("entry_bar_open", _parse_utc(ENTRY_UTC)),
    ]
    if path_sum.get("first_intrabar_touch_ts"):
        timeline_defs.append(
            ("first_zone_touch", datetime.fromtimestamp(int(path_sum["first_intrabar_touch_ts"]), tz=timezone.utc))
        )
    if path_sum.get("max_high_ts"):
        timeline_defs.append(
            ("max_high", datetime.fromtimestamp(int(path_sum["max_high_ts"]), tz=timezone.utc))
        )
    timeline_defs.append(("sl_exit", _parse_utc(EXIT_SL_UTC)))
    timeline_rows: list[dict] = []
    for label, w in timeline_defs:
        dc_t = _decision_close(w)
        wall_s = l1.fmt_utc(int(w.timestamp()))
        mem_t, err_t = l1.load_pool_snapshot(dc_t)
        bands_t, _ = upper_d2_bands_at_close(mem_t, dc_t, ENTRY, min_depth=D2_MIN_DEPTH)
        zm = _active_upper_members_in_zone(mem_t or [], dc_t, ENTRY, ZONE_LO, ZONE_HI) if mem_t else []
        zb = _zone_d2_slice(bands_t, ZONE_LO, ZONE_HI)
        max_d = max((b.depth for b in zb), default=0)
        timeline_rows.append(
            {
                "label": label,
                "wall_utc": wall_s,
                "decision_close_utc": l1.fmt_utc(int(dc_t.timestamp())),
                "pool_ssot_ok": "yes" if mem_t else "no",
                "pool_ssot_error": err_t or "",
                "d2_band_count_depth_ge2": len(bands_t),
                "zone_0894_0900_d2_slices": len(zb),
                "zone_max_depth": max_d,
                "zone_active_upper_members": len(zm),
                "zone_unique_origins": len({x['origin_id'] for x in zm}),
                "zone_d2_tops": ";".join(f"{b.bottom:.5f}-{b.top:.5f}(d{b.depth})" for b in zb),
            }
        )

    tp_D = float(nearest["lower_bound"]) if nearest else None
    comparisons = []
    for ref, tp, note in [
        ("A_backtester_v3u", TP_A, "max(lower_bound) pool-only V3U"),
        ("B_user_zone_low", TP_B, "post-hoc user screenshot reference"),
        ("C_user_zone_high", TP_C, "post-hoc user screenshot reference"),
        ("D_nearest_qualified_d2", tp_D, "nearest D2-qualified at entry decision"),
    ]:
        if tp is None:
            continue
        dist = (tp - ENTRY) / ENTRY * 100.0
        rep = _replay_exit(candles, entry_ts, SL, tp, exit_cap_ts=exit_cap)
        comparisons.append(
            {
                "reference": ref,
                "note": note,
                "planned_tp": f"{tp:.10f}",
                "dist_from_entry_pct": round(dist, 4),
                **rep,
            }
        )
    comparisons.append(
        {
            "reference": "E_substantial_concentration_rule",
            "note": "No objective substantial-zone rule validated in L1 codebase (V3 pool landscape thresholds are provisional, not trade-TP)",
            "planned_tp": "",
            "dist_from_entry_pct": "",
            "tp_reached": "n/a",
            "sl_first": "n/a",
            "first_exit_utc": "",
            "exit_price": "",
            "gross_pnl_pct": "",
            "same_bar_ambiguity": "n/a",
        }
    )

    # channel path at max high
    ch_at_max = None
    if path_sum["max_high_utc"]:
        t_max = _ts(path_sum["max_high_utc"])
        ch_at_max, id_max = l1.channel_upper_at(v3p_lines, t_max, ENTRY, exclude_drawing_ids=frozenset({SUPPORT_ID}))

    report = [
        "# L1 V3V — Single trade pool TP audit",
        "",
        f"Trade: DOGEUSDT 5m LONG @ `{ENTRY_UTC}` entry `{ENTRY}` support `{SUPPORT_ID}`",
        f"C1 confirm bar close (pool SSOT): `{l1.fmt_utc(int(dc.timestamp()))}`",
        f"V3U exit: SL `{EXIT_SL_UTC}` @ `{SL}` (−1.20%)",
        "",
        "## Why TP 0.09496 (V3U variant A)",
        "",
        f"- `channel_upper_at` on **V3U** snapshot: `{ch_v3u}` id `{id_v3u}` → **no channel deckel** within 12% span",
        f"- D2 gate: {m.d2_qualified_count} qualified bands (depth≥2, dist≥{MIN_POOL_DIST_PCT}%)",
        f"- `pick_tp` pool-only rule: **max(lower_bound)** → `{pick.tp_price}` mode `{pick.tp_mode}`",
        f"- Selected pool_id: `{pick.tp_pool_id}` ({m.d2_qualified_count} nearer targets skipped)",
        "",
        "## User zone 0.0894–0.0900 @ entry decision",
        "",
        f"- ACTIVE upper members overlapping zone: **{len(zone_members)}**",
        f"- Unique origin IDs: **{len(origins)}** → `{', '.join(origins[:12])}{'…' if len(origins)>12 else ''}`",
        f"- D2 depth≥2 slices intersecting zone: **{len(zone_band)}** (max depth **{max((b.depth for b in zone_band), default=0)}**)",
        f"- Nearest qualified D2 below zone bottom: `{nearest['lower_bound'] if nearest else 'n/a'}` ({m.nearest_d2_dist_pct}% )",
        "",
        "## V3P vs V3U channel (retrospective drawings)",
        "",
        f"- **V3P** `channel_upper_at` @ entry: `{ch_v3p}` (`{id_v3p}`) → legacy pick TP `{pick_v3p.tp_price}` mode `{pick_v3p.tp_mode}`",
        f"- **V3P** historical exit (same entry, older drawing set): **CHANNEL_UPPER** ~0.08997 @ 2026-08-28 01:30 UTC (+6.54%)",
        f"- **V3U** (missing/lost upper partner `{id_v3p}` in snapshot): pool-only far TP → SL −1.20%",
        f"- V3P upper @ max-high bar ({path_sum['max_high_utc']}): `{ch_at_max}` (`{id_max}`)",
        "",
        "## Price path entry → SL",
        "",
        f"- Max high `{path_sum['max_high']}` @ {path_sum['max_high_utc']} (**{path_sum['max_favorable_pct']}%** MFE)",
        f"- First intrabar touch 0.0894–0.0900: {path_sum['first_intrabar_touch_zone_utc'] or '—'}",
        f"- First **close** inside zone: {path_sum['first_close_in_zone_utc'] or '—'}",
        f"- First close rejection below zone: {path_sum['first_confirmed_rejection_close_below_zone_utc'] or '—'}",
        "",
        "## TP comparison (isolated, same SL)",
        "",
    ]
    for c in comparisons:
        report.append(
            f"- **{c['reference']}** TP {c['planned_tp']} dist {c['dist_from_entry_pct']}% "
            f"reached={c['tp_reached']} exit={c['first_exit_utc']} pnl={c['gross_pnl_pct']}% ambig={c['same_bar_ambiguity']}"
        )

    (OUT / f"{PREFIX}_upper_d2_entry_snapshot.csv").write_text(
        "lower_bound,upper_bound,depth,width_pct,dist_from_entry_pct,selected_by_backtester,nearest_qualified\n"
        + "\n".join(
            ",".join(str(row[k]) for k in row)
            for row in entry_rows
        )
        + "\n",
        encoding="utf-8",
    )
    with (OUT / f"{PREFIX}_upper_d2_timeline.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(timeline_rows[0].keys()))
        w.writeheader()
        w.writerows(timeline_rows)
    with (OUT / f"{PREFIX}_tp_comparison.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(comparisons[0].keys()))
        w.writeheader()
        w.writerows(comparisons)
    with (OUT / f"{PREFIX}_price_path.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(path_rows[0].keys()))
        w.writeheader()
        w.writerows(path_rows)

    (OUT / f"{PREFIX}_single_trade_pool_tp_audit.md").write_text("\n".join(report) + "\n", encoding="utf-8")

    meta = {
        "entry_utc": ENTRY_UTC,
        "decision_close_utc": l1.fmt_utc(int(dc.timestamp())),
        "tp_a": TP_A,
        "pick_tp_price": pick.tp_price,
        "nearest_qualified": nearest["lower_bound"] if nearest else None,
        "zone_at_entry": {
            "members": len(zone_members),
            "origins": origins,
            "d2_slices": [{"lo": b.bottom, "hi": b.top, "depth": b.depth} for b in zone_band],
        },
        "path_summary": path_sum,
        "v3p_channel": {"price": ch_v3p, "id": id_v3p, "pick_tp": pick_v3p.tp_price},
        "v3u_channel": {"price": ch_v3u, "id": id_v3u},
    }
    print(json.dumps(meta, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
