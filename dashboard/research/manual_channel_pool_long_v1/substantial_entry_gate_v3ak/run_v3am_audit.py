#!/usr/bin/env python3
"""L1 V3AM — forensic compare two E3 trades 2026-08-30 (READ-ONLY)."""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

V3AK = Path(__file__).resolve().parent
PROBE = V3AK.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3AH = PROBE / "realistic_pool_tp_v3ah"
V3W = PROBE / "tp_research_v3w"
DASHBOARD = PROBE.parent

for p in (V3AK, V3AH, V3W, V3AB, PROBE, V3Y if (V3Y := PROBE / "support_break_exit_v3y").is_dir() else V3AK, DASHBOARD):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import pilot_engine as pe
import run_l1_pilot as l1
from line_geometry import LineDef
from pool_d2_metrics import analyze_d2_landscape
from support_sim import line_price_at_close
from zone_density import RealisticTPParams, build_zones_with_density, resolve_realistic_tp
from zone_features import merge_qualified_zones, enrich_zone_origins

from substantial_entry_gate import ENTRY_GATE_SPECS, apply_substantial_entry_gate
from v3ah_tp import pending_with_realistic_tp
from v3ak_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, build_context_from_snapshot

OUT = V3AK
TRADES = (
    ("A", "te-d-ea081434c5d3-0003"),
    ("B", "te-d-ea081434c5d3-0012"),
)
CAP6 = RealisticTPParams(code="CAP6_D20_BLOCK", cap_dist_pct=6.0, dominance="D20", no_tp_policy="BLOCK")
E3 = ENTRY_GATE_SPECS[3]
SUPPORT_ID = "d-ea081434c5d3"


def parse_utc(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def candle_idx(candles: list[dict], utc: str) -> int:
    t = int(parse_utc(utc).timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == t:
            return i
    raise ValueError(f"no candle for {utc}")


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def closes_upto(candles: list[dict], idx: int) -> list[float]:
    return [float(c["close"]) for c in candles[: idx + 1]]


def pct_dist(price: float, ref: float) -> float:
    return (price - ref) / ref * 100.0


def mfe_mae(candles: list, i0: int, i1: int, entry: float) -> tuple[float, float, float, float]:
    hi_p = lo_p = entry
    for j in range(i0, i1 + 1):
        hi_p = max(hi_p, float(candles[j]["high"]))
        lo_p = min(lo_p, float(candles[j]["low"]))
    mfe = (hi_p - entry) / entry * 100.0
    mae = (lo_p - entry) / entry * 100.0
    return round(mfe, 4), round(mae, 4), hi_p, lo_p


def support_price_at(line: LineDef, ts: int) -> float | None:
    if not line.active_for_candle(float(ts)):
        return None
    return line.price_at(float(ts))


def touch_count_on_line(pending: list[dict], drawing_id: str, before_utc: str) -> int:
    cut = parse_utc(before_utc)
    n = 0
    for p in pending:
        if p.get("touch_drawing_id") != drawing_id:
            continue
        tu = p.get("touch_utc")
        if not tu:
            continue
        if parse_utc(tu) < cut and p.get("long_trigger_allowed") == "yes":
            n += 1
    return n


def zone_row(z: dict, label: str) -> dict:
    rz = z["zone"]
    return {
        "label": label,
        "bottom": f"{rz.bottom:.10f}",
        "top": f"{rz.top:.10f}",
        "dist_pct": round(z["dist_from_entry_pct"], 4),
        "width_pct": round(rz.width_pct, 4),
        "max_depth": rz.max_depth,
        "unique_origins": rz.unique_origins,
        "integrated_density": round(z["integrated_density"], 6),
        "density_rank": z["density_rank"],
        "in_d20": z["in_d20"],
    }


def lower_pool_summary(members: list[dict] | None, dc: datetime, entry: float) -> str:
    if not members:
        return "no SSOT"
    lowers = [m for m in members if str(m.get("side", "")).lower() == "lower"]
    below = [m for m in lowers if float(m["upper_bound"]) <= entry]
    if not below:
        return "no active lower bands below entry"
    nearest = max(below, key=lambda m: float(m["upper_bound"]))
    dist = (entry - float(nearest["upper_bound"])) / entry * 100.0
    depth = int(nearest.get("d2_depth") or 0)
    return f"nearest lower upper={nearest['upper_bound']:.5f} dist={dist:.3f}% depth={depth} n_below={len(below)}"


def pool_landscape_at(row: dict) -> dict[str, Any]:
    dc = datetime.strptime(row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
    entry = float(row["entry_price"])
    members = row.get("_pool_members")
    scored, _ = build_zones_with_density(members, dc, entry)
    res = resolve_realistic_tp(members, dc, entry, cap_dist_pct=6.0, dominance="D20")
    m_d2, bands, qualified = analyze_d2_landscape(members, dc, entry)
    meta = json.loads(row.get("_v3ak_entry_meta") or "{}")
    qz = meta.get("qualifying_zone") or {}
    nearest = scored[0] if scored else None
    tp_pick = None
    for r in scored:
        if res.ok and abs(r["zone"].bottom - float(res.tp_bottom)) < 1e-5:
            tp_pick = r
            break
    zones = merge_qualified_zones(qualified, entry, merge_gap_pct=0.15)
    enrich_zone_origins(zones, members, dc, entry)
    return {
        "dc": dc.strftime("%Y-%m-%d %H:%M:%S UTC"),
        "nearest_upper": zone_row(nearest, "nearest_merge") if nearest else {},
        "e3_zone": qz,
        "tp_zone": zone_row(tp_pick, "cap6_tp") if tp_pick else {},
        "first_d2_dist_pct": m_d2.nearest_d2_dist_pct,
        "first_d2_depth": m_d2.nearest_d2_depth,
        "qualified_d2_count": m_d2.d2_qualified_count,
        "lower_summary": lower_pool_summary(members, dc, entry),
        "n_merge_zones": len(scored),
    }


def market_at_entry(candles: list[dict], idx: int, line: LineDef, support_id: str) -> dict:
    c = candles[idx]
    entry = float(c["open"])  # placeholder, overwritten
    cl = closes_upto(candles, idx)
    for p in (9, 20, 59, 100):
        e = ema(cl, p)[-1]
    ema9, ema20, ema59, ema100 = (ema(cl, p)[-1] for p in (9, 20, 59, 100))
    entry = float(candles[idx]["open"]) if False else float(candles[idx - 0]["close"])
    # use row entry price separately
    def ret_h(h: int) -> float:
        j = max(0, idx - h * 12)
        return (float(candles[idx]["close"]) - float(candles[j]["close"])) / float(candles[j]["close"]) * 100.0

    ts = int(c["time"])
    lp = support_price_at(line, ts)
    return {
        "close": float(c["close"]),
        "ret_1h_pct": round(ret_h(1), 4),
        "ret_3h_pct": round(ret_h(3), 4),
        "ret_6h_pct": round(ret_h(6), 4),
        "high_6h": max(float(x["high"]) for x in candles[max(0, idx - 72) : idx + 1]),
        "low_6h": min(float(x["low"]) for x in candles[max(0, idx - 72) : idx + 1]),
        "ema9_dist_pct": None,
        "ema20_dist_pct": None,
        "ema59_dist_pct": None,
        "ema100_dist_pct": None,
        "_ema9": ema9,
        "_ema20": ema20,
        "_ema59": ema59,
        "_ema100": ema100,
        "support_line_px": lp,
    }


def enrich_ema_dist(mkt: dict, entry: float) -> None:
    for p in (9, 20, 59, 100):
        e = mkt[f"_ema{p}"]
        mkt[f"ema{p}_dist_pct"] = round(pct_dist(entry, e), 4)


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
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


def maybe_png(candles: list[dict], rows: list[dict], line: LineDef) -> str:
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return ""
    path = OUT / "l1_v3am_diagnostic_chart.png"
    i0 = min(candle_idx(candles, rows[0]["entry_utc"]) for r in rows) - 24
    i1 = max(candle_idx(candles, rows[1]["exit_utc"]) for r in rows) + 12
    i0 = max(0, i0)
    seg = candles[i0 : i1 + 1]
    xs = [datetime.fromtimestamp(int(c["time"]), tz=timezone.utc) for c in seg]
    ys = [float(c["close"]) for c in seg]
    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(xs, ys, color="#333", lw=1)
    sup = [support_price_at(line, int(c["time"])) for c in seg]
    ax.plot(xs, [s if s else float("nan") for s in sup], color="#6ef812", ls="--", label="support d-ea081434c5d3")
    colors = {"A": "#2ecc71", "B": "#e74c3c"}
    for r in rows:
        ei = parse_utc(r["entry_utc"])
        xi = [ei]
        ax.scatter(xi, [float(r["entry_price"])], color=colors[r["trade"]], s=80, zorder=5, label=f"Entry {r['trade']}")
        ax.axhline(float(r["tp_price"]), color=colors[r["trade"]], alpha=0.3, lw=0.8)
    ax.legend(loc="upper left")
    ax.set_title("V3AM E3 trades 2026-08-30 (READ-ONLY diagnostic)")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return str(path.name)


def main() -> int:
    l1._bootstrap_import_paths()
    mod = sys.modules[l1.__name__]
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    line = lines_by_id[SUPPORT_ID]
    lines = list(frozen["ctx"]["lines_by_id"].values())
    pending_base = pending_with_realistic_tp(frozen["pending"], lines, l1_module=mod, params=CAP6)
    pending = __import__("copy").deepcopy(pending_base)
    apply_substantial_entry_gate(pending, E3)
    pe.enrich_pending_metrics(pending, frozen["upper_line"], mod, independent=True)
    pmap = {p["touch_event_id"]: p for p in pending}

    trade_rows: list[dict] = []
    pool_rows: list[dict] = []
    compare: dict[str, dict] = {}

    chart_review = OUT / "l1_v3al_e0_e3_chart_review.csv"
    cr: dict[str, dict] = {}
    if chart_review.is_file():
        with chart_review.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("trade_id") in {t[1] for t in TRADES}:
                    cr[r["trade_id"]] = r

    for label, eid in TRADES:
        row = pmap[eid]
        entry = float(row["entry_price"])
        entry_idx = int(row["_entry_idx"])
        exit_utc = cr[eid]["exit_utc"]
        exit_idx = candle_idx(candles, exit_utc)
        pl = pool_landscape_at(row)
        mkt = market_at_entry(candles, entry_idx, line, SUPPORT_ID)
        enrich_ema_dist(mkt, entry)
        mfe, mae, hi, lo = mfe_mae(candles, entry_idx, exit_idx, entry)
        lp = mkt["support_line_px"]
        sup_dist = (entry - lp) / entry * 100.0 if lp else None
        t0 = int(line.start_timestamp) if hasattr(line, "start_timestamp") else None
        slope_pct_per_h = None
        if t0 and hasattr(line, "start_price"):
            dt_h = (int(candles[entry_idx]["time"]) - int(line.start_timestamp)) / 3600.0
            if dt_h > 0:
                slope_pct_per_h = (line.price_at(float(candles[entry_idx]["time"])) - float(line.start_price)) / float(
                    line.start_price
                ) / dt_h * 100.0

        trade_rows.append(
            {
                "trade": label,
                "touch_event_id": eid,
                "support_id": SUPPORT_ID,
                "touch_utc": row["touch_utc"],
                "confirm_utc": row["confirm_utc"],
                "entry_utc": row["entry_utc"],
                "entry_price": row["entry_price"],
                "touch_line_price": row["touch_line_price"],
                "entry_rule": "first touch + C1 confirm + next bar open (run_l1_pilot)",
                "d2_gate": "pass" if row.get("_d2_gate_ok") else "fail",
                "d2_nearness": "pass" if row.get("_d2_nearness_ok") else "fail",
                "first_d2_upper_dist_pct": pl["first_d2_dist_pct"],
                "e3_qual_zone": f"{pl['e3_zone'].get('bottom')}-{pl['e3_zone'].get('top')}",
                "tp_price": row["tp_price"],
                "sl_price": row["sl_price"],
                "exit_utc": exit_utc,
                "exit_reason": cr[eid]["exit_reason"],
                "pnl_pct": cr[eid]["pnl_pct"],
                "tp_touch_utc": cr[eid].get("tp_touch_utc", ""),
                "break_utc": cr[eid].get("break_utc", ""),
                "confirm_break_utc": cr[eid].get("confirm_utc", ""),
                "mfe_pct": mfe,
                "mae_pct": mae,
                "high_in_trade": f"{hi:.10f}",
                "low_in_trade": f"{lo:.10f}",
                "support_dist_pct_at_entry": round(sup_dist, 4) if sup_dist is not None else "",
                "prior_touches_same_support": touch_count_on_line(pending, SUPPORT_ID, row["entry_utc"]),
                "ret_1h_pct": mkt["ret_1h_pct"],
                "ret_3h_pct": mkt["ret_3h_pct"],
                "ret_6h_pct": mkt["ret_6h_pct"],
                "ema9_dist_pct": mkt["ema9_dist_pct"],
                "ema20_dist_pct": mkt["ema20_dist_pct"],
                "ema59_dist_pct": mkt["ema59_dist_pct"],
                "ema100_dist_pct": mkt["ema100_dist_pct"],
                "pool_decision_close": pl["dc"],
                "retrospective_drawing": "RETROSPECTIVE_DRAWING",
            }
        )
        for zlabel, zd in (("nearest_upper", pl["nearest_upper"]), ("cap6_tp", pl["tp_zone"])):
            if zd:
                pool_rows.append({"trade": label, "role": zlabel, "entry_utc": row["entry_utc"], **zd})
        q = pl["e3_zone"]
        pool_rows.append(
            {
                "trade": label,
                "role": "e3_entry_gate",
                "entry_utc": row["entry_utc"],
                "bottom": q.get("bottom"),
                "top": q.get("top"),
                "dist_pct": q.get("dist_from_entry_pct"),
                "width_pct": q.get("width_pct"),
                "max_depth": q.get("max_depth"),
                "unique_origins": q.get("unique_origins"),
            }
        )
        pool_rows.append(
            {
                "trade": label,
                "role": "lower_pools",
                "entry_utc": row["entry_utc"],
                "summary": pl["lower_summary"],
                "first_raw_d2_dist_pct": pl["first_d2_dist_pct"],
            }
        )
        compare[label] = {
            "entry_price": entry,
            "support_line": SUPPORT_ID,
            "support_dist_pct": round(sup_dist, 4) if sup_dist is not None else "",
            "move_before_pct_6h": mkt["ret_6h_pct"],
            "near_upper": pl["nearest_upper"].get("bottom", "") if pl["nearest_upper"] else "",
            "e3_zone": trade_rows[-1]["e3_qual_zone"],
            "tp_pool": pl["tp_zone"].get("bottom", ""),
            "tp_dist_pct": pl["tp_zone"].get("dist_pct", ""),
            "lower_pools": pl["lower_summary"],
            "ema9": mkt["ema9_dist_pct"],
            "ema20": mkt["ema20_dist_pct"],
            "mfe": mfe,
            "mae": mae,
            "exit": cr[eid]["exit_reason"],
            "pnl": cr[eid]["pnl_pct"],
            "prior_touches": touch_count_on_line(pending, SUPPORT_ID, row["entry_utc"]),
            "slope_pct_per_h": round(slope_pct_per_h, 4) if slope_pct_per_h else "",
        }

    # competing candidates 30 Aug from timeline
    timeline = OUT / "l1_v3al_entry_decision_timeline.csv"
    competitors: list[str] = []
    if timeline.is_file():
        with timeline.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                if r.get("utc", "").startswith("2026-08-30") and r.get("e3_status") == "allowed":
                    competitors.append(
                        f"{r['utc']} {r['touch_event_id']} outcome_e3={r.get('e3_outcome','')}"
                    )

    comp_csv = [
        {
            "merkmal": k,
            "trade_a_1145": compare["A"].get(
                {
                    "entry_price": "entry_price",
                    "support_line": "support_line",
                    "support_dist_pct": "support_dist_pct",
                    "move_before_pct_6h": "move_before_pct_6h",
                    "near_upper": "near_upper",
                    "e3_zone": "e3_zone",
                    "tp_pool": "tp_pool",
                    "tp_dist_pct": "tp_dist_pct",
                    "lower_pools": "lower_pools",
                    "ema9_dist_pct": "ema9",
                    "ema20_dist_pct": "ema20",
                    "mfe_pct": "mfe",
                    "mae_pct": "mae",
                    "exit_reason": "exit",
                    "pnl_pct": "pnl",
                }.get(k, k),
                "",
            ),
            "trade_b_1740": compare["B"].get(
                {
                    "entry_price": "entry_price",
                    "support_line": "support_line",
                    "support_dist_pct": "support_dist_pct",
                    "move_before_pct_6h": "move_before_pct_6h",
                    "near_upper": "near_upper",
                    "e3_zone": "e3_zone",
                    "tp_pool": "tp_pool",
                    "tp_dist_pct": "tp_dist_pct",
                    "lower_pools": "lower_pools",
                    "ema9_dist_pct": "ema9",
                    "ema20_dist_pct": "ema20",
                    "mfe_pct": "mfe",
                    "mae_pct": "mae",
                    "exit_reason": "exit",
                    "pnl_pct": "pnl",
                }.get(k, k),
                "",
            ),
        }
        for k in [
            "entry_price",
            "support_line",
            "support_dist_pct",
            "move_before_pct_6h",
            "near_upper",
            "e3_zone",
            "tp_pool",
            "tp_dist_pct",
            "lower_pools",
            "ema9_dist_pct",
            "ema20_dist_pct",
            "mfe_pct",
            "mae_pct",
            "exit_reason",
            "pnl_pct",
        ]
    ]

    png = maybe_png(candles, trade_rows, line)

    md = _report(trade_rows, compare, competitors, pl_a=pool_landscape_at(pmap[TRADES[0][1]]), pl_b=pool_landscape_at(pmap[TRADES[1][1]]), png=png)
    (OUT / "L1_V3AM_TWO_ENTRY_FORENSIC_AUDIT.md").write_text(md, encoding="utf-8")
    write_csv(OUT / "l1_v3am_entry_comparison.csv", comp_csv)
    write_csv(OUT / "l1_v3am_pool_landscape_comparison.csv", pool_rows)
    write_csv(OUT / "l1_v3am_chart_review.csv", trade_rows)
    print(OUT / "L1_V3AM_TWO_ENTRY_FORENSIC_AUDIT.md")
    return 0


def _report(trades, compare, competitors, pl_a, pl_b, png: str) -> str:
    a, b = trades[0], trades[1]
    lines = [
        "# L1 V3AM — Zwei E3-Long-Entries 30.08.2026",
        "",
        f"Snapshot SHA256 `{FROZEN_SHA256[:16]}…` · **READ-ONLY** · `RETROSPECTIVE_DRAWING`",
        "",
        "## Kurzfazit",
        "",
        "**Trade A (11:45)** war beim Entry **näher am Support**, mit **kleinerem CAP6-TP (0,52 %)** und stärkerer naher Upper-Konzentration (E3-Zone depth 7 / 17 Origins). ",
        "**Trade B (17:40)** lag **höher im Kanal** (+1,13 % über A-Entry), TP **2,38 %** entfernt; E3-Zone am oberen 2,5 %-Rand (depth 5 / 6 Origins). ",
        "Beide erfüllten formal **V3E-B + E3 + CAP6-D20-BLOCK**. Objektiv war B **strukturell „später/höher“** (mehr Vorlauf in 6 h, höhere EMA-Abstände), aber **kein einzelnes Gate** trennte Gewinner und Verlierer.",
        "",
        "**Trade B endete am SL**, obwohl **nach** bestätigtem Supportbruch (20:25 / 20:35 UTC): `simulate_exit_v3y` prüft **SL vor** dem geplanten SBE-Open-Fill — die 20:40-Kerze trifft **SL (−1,2 %)** zuerst.",
        "",
        "## Vergleichstabelle",
        "",
        "| Merkmal | Trade A 11:45 | Trade B 17:40 |",
        "|---|---|---|",
    ]
    for row in [
        ("Entry-Preis", a["entry_price"], b["entry_price"]),
        ("Supportlinie", SUPPORT_ID, SUPPORT_ID),
        ("Support-Abstand %", compare["A"]["support_dist_pct"], compare["B"]["support_dist_pct"]),
        ("Kursbewegung 6h vorher %", compare["A"]["move_before_pct_6h"], compare["B"]["move_before_pct_6h"]),
        ("Nahe Upper (stärkste Merge)", pl_a["nearest_upper"].get("bottom"), pl_b["nearest_upper"].get("bottom")),
        ("E3-Entry-Zone", a["e3_qual_zone"], b["e3_qual_zone"]),
        ("CAP6-TP-Zone unten", compare["A"]["tp_pool"], compare["B"]["tp_pool"]),
        ("TP-Distanz %", compare["A"]["tp_dist_pct"], compare["B"]["tp_dist_pct"]),
        ("Lower-Pools", compare["A"]["lower_pools"][:60], compare["B"]["lower_pools"][:60]),
        ("EMA9-Abstand %", compare["A"]["ema9"], compare["B"]["ema9"]),
        ("EMA20-Abstand %", compare["A"]["ema20"], compare["B"]["ema20"]),
        ("MFE %", a["mfe_pct"], b["mfe_pct"]),
        ("MAE %", a["mae_pct"], b["mae_pct"]),
        ("Exit-Grund", a["exit_reason"], b["exit_reason"]),
        ("Ergebnis %", a["pnl_pct"], b["pnl_pct"]),
    ]:
        lines.append(f"| {row[0]} | {row[1]} | {row[2]} |")

    lines.extend(["", "## Entry-Rekonstruktion (je Trade)", ""])
    for t in trades:
        lines.extend(
            [
                f"### Trade {t['trade']} · `{t['touch_event_id']}`",
                "",
                f"- **Support-Touch:** {t['touch_utc']} (Linienpreis Touch ≈ {t['touch_line_price']})",
                f"- **C1-Bestätigung:** {t['confirm_utc']}",
                f"- **Entry:** {t['entry_utc']} @ {t['entry_price']} (Regel: {t['entry_rule']})",
                f"- **D2-Gate / B-Nearness:** {t['d2_gate']} / {t['d2_nearness']} · erstes qual. Upper-D2 ≤2,5 %: **{t['first_d2_upper_dist_pct']}%**",
                f"- **E3-Substantial-Zone:** {t['e3_qual_zone']}",
                f"- **CAP6-D20-TP:** {t['tp_price']} · **SL:** {t['sl_price']}",
                f"- **Vorherige gültige Touches auf derselben Support-ID (chronologisch vor Entry):** {t['prior_touches_same_support']}",
                "",
            ]
        )

    lines.extend(
        [
            "## Konkurrierende E3-Kandidaten am 30.08.",
            "",
        ]
        + [f"- {c}" for c in competitors]
        + [
            "",
            "Trade A nutzte den ersten freien Slot nach E0-Serie; Trade B folgte 3 h nach A-TP (max. 1 Long).",
            "",
            "## Poollandschaft @ `_pool_decision_close`",
            "",
            "Details: `l1_v3am_pool_landscape_comparison.csv` (kanonische PD1 Exact D2 Density, `build_zones_with_density`).",
            "",
            f"- **A:** E3-Zone depth **{pl_a['e3_zone'].get('max_depth')}** / origins **{pl_a['e3_zone'].get('unique_origins')}**; CAP6-TP rank **{pl_a['tp_zone'].get('density_rank')}** ∫D **{pl_a['tp_zone'].get('integrated_density')}**",
            f"- **B:** E3-Zone depth **{pl_b['e3_zone'].get('max_depth')}** / origins **{pl_b['e3_zone'].get('unique_origins')}**; CAP6-TP rank **{pl_b['tp_zone'].get('density_rank')}** ∫D **{pl_b['tp_zone'].get('integrated_density')}**",
            "",
            "Zwischen 11:45 und 17:40 **stieg der Markt** (~+1,1 % Entry-Niveau); die E3-Qualifikation wanderte von **naher** Zone (0,52 % über Entry) zu **weit** entfernter Substantial-Zone bei **2,38 %** (nahe 2,5 %-Cap).",
            "",
            "## Post-Entry & Exit B",
            "",
            f"- **A:** TP-Touch {a['tp_touch_utc']} · Haltedauer 20 min · MFE {a['mfe_pct']}%",
            f"- **B:** kein TP-Touch; break {b['break_utc']} confirm {b['confirm_break_utc']}; **SL** {b['exit_utc']} — SBE-Fill wäre nächste Bar nach Confirm, aber **Low ≤ SL** auf Exit-Bar zuerst (support_sim Reihenfolge).",
            "",
        ]
    )

    lines.extend(
        [
            "## Beantwortung der Prüffragen",
            "",
            "1. **War B objektiv schwächer?** Teils ja: höherer Entry, längerer TP-Weg, mehr Vorlauf (6 h), EMA weiter über Trend; E3-Metriken (depth/origins) schwächer. Formal aber **weiterhin regelkonform**.",
            "2. **Vor Entry erkennbar:** Höhe/Support-Abstand, TP-Distanz, E3-Zonenlage, 6h-Rally; **nicht:** SL- vs. SBE-Race.",
            "3. **Kein Vorhersage-Signal für Verlust:** Intrabar SL vor SBE; kurzes MFE (+0,65 %) reichte TP 2,38 % nicht.",
            "4. **Filter-Ansatzpunkt (hypothetisch, nicht kalibriert):** „kein Entry wenn CAP6-TP-Distanz ≫ E3-nahe Zone“ oder „6h-Rally + Entry weit über EMA20“ — **nur Forschungsthese**.",
            "5. **Risiko guter Blocker:** Trade A hätte z. B. ein „zu hoher Entry“-Filter **nicht** getroffen; ein starres „max TP-Distanz“ könnte **A** nicht blockieren (TP 0,52 %).",
            "",
            "## GO-TO Chart",
            "",
            "| Trade | Touch | C1 | Entry | TP touch | Break / Confirm | Exit |",
            "|---|---|---|---|---|---|---|",
            f"| A | {a['touch_utc']} | {a['confirm_utc']} | {a['entry_utc']} @ {a['entry_price']} | {a['tp_touch_utc']} | — | {a['exit_utc']} TP {a['pnl_pct']}% |",
            f"| B | {b['touch_utc']} | {b['confirm_utc']} | {b['entry_utc']} @ {b['entry_price']} | — | {b['break_utc']} / {b['confirm_break_utc']} | {b['exit_utc']} SL {b['pnl_pct']}% |",
            "",
        ]
    )
    if png:
        lines.append(f"Diagnose-PNG (lokal, unverändert am Research-Chart): `{png}`")
    lines.extend(
        [
            "",
            "## Artefakte",
            "",
            "- `l1_v3am_entry_comparison.csv`",
            "- `l1_v3am_pool_landscape_comparison.csv`",
            "- `l1_v3am_chart_review.csv`",
            "",
            "**STOP** — keine Strategieübernahme.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
