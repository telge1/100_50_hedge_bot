#!/usr/bin/env python3
"""L1 V3AI — READ-ONLY audit of V3AH results (no strategy/backtest changes)."""

from __future__ import annotations

import copy
import csv
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

V3AH = Path(__file__).resolve().parent
PROBE = V3AH.parent
V3AB = PROBE / "support_break_exit_v3ab"
V3Y = PROBE / "support_break_exit_v3y"
V3W = PROBE / "tp_research_v3w"
DASHBOARD = PROBE.parent

for p in (V3AH, V3W, V3AB, PROBE, V3Y, DASHBOARD):
    if p.is_dir() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

import run_l1_pilot as l1
from line_geometry import BAR_SEC_5M, LineDef
from pool_d2_metrics import analyze_d2_landscape
from support_sim import line_price_at_close, simulate_exit_v3y
from v3y_config import CONFIRM_DELTA
from v3ah_context import DEFAULT_SNAPSHOT, FROZEN_SHA256, VARIANTS, build_context_from_snapshot
from v3ah_tp import pending_with_realistic_tp
from zone_density import RealisticTPParams, build_zones_with_density, resolve_realistic_tp

OUT = V3AH
TRADES_CSV = V3AH / "l1_v3ah_trades_all.csv"

AUDIT_SCENARIOS = (
    "BASELINE_V3AB",
    "CAP3_D20_BLOCK",
    "CAP3_D20_ALLOW",
    "CAP5_D20_BLOCK",
    "CAP5_D20_ALLOW",
    "CAP6_D20_BLOCK",
    "CAP6_D20_ALLOW",
    "CAP6_D25_BLOCK",
    "CAP6_D25_ALLOW",
)
VARIANT_A = "D2"
VARIANT_B = "V3E-B"
REF_EID = "te-d-c0791ea0c65e-0003"
HORIZON_BARS = {24: 24 * 12, 48: 48 * 12, 72: 72 * 12}  # 5m bars


def _safe_float(s: Any) -> float | None:
    if s is None or s == "" or str(s).strip() in ("None", "null"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _utc(ts: int | None) -> str:
    if ts is None:
        return ""
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _parse_utc(s: str) -> datetime | None:
    if not s or s == "None":
        return None
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _read_trades() -> list[dict[str, str]]:
    if not TRADES_CSV.is_file():
        raise SystemExit(f"missing {TRADES_CSV}")
    with TRADES_CSV.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _executed(row: dict) -> bool:
    return row.get("exit_reason") not in ("", "NO_TRADE", None) and bool(row.get("exit_utc"))


def _candle_idx(candles: list[dict], utc: str) -> int | None:
    dt = _parse_utc(utc)
    if not dt:
        return None
    t = int(dt.timestamp())
    for i, c in enumerate(candles):
        if int(c["time"]) == t:
            return i
        if int(c["time"]) > t:
            return i - 1 if i > 0 else 0
    return len(candles) - 1


def _first_touch_idx(candles: list[dict], start: int, end_exclusive: int, target: float) -> int | None:
    end = min(end_exclusive, len(candles))
    for j in range(start, end):
        if float(candles[j]["high"]) >= target - 1e-15:
            return j
    return None


def _avg_depth_in_zone(zone_lo: float, zone_hi: float, qualified: list[dict]) -> float | None:
    wsum = dsum = 0.0
    for q in qualified:
        qb, qt = float(q["lower_bound"]), float(q["upper_bound"])
        if qt <= zone_lo or qb >= zone_hi:
            continue
        a, b = max(zone_lo, qb), min(zone_hi, qt)
        w = b - a
        if w > 0:
            wsum += w
            dsum += int(q.get("d2_depth") or 0) * w
    return dsum / wsum if wsum > 0 else None


def _tp_selection_narrative(
    members: list[dict],
    dc: datetime,
    entry: float,
    *,
    cap: float,
    dominance: str,
    chosen_bottom: float | None,
) -> dict[str, Any]:
    scored, _ = build_zones_with_density(members, dc, entry)
    cut = max(1, int(len(scored) * (0.20 if dominance == "D20" else 0.25) + 0.999))
    by_price = sorted(scored, key=lambda r: r["zone"].bottom)
    skips: list[str] = []
    for r in by_price:
        z = r["zone"]
        dist = r["dist_from_entry_pct"]
        dom_ok = r["in_d20"] if dominance == "D20" else r["in_d25"]
        reasons = []
        if dist < 0.5:
            reasons.append("dist<0.5%")
        if dist > cap:
            reasons.append(f"dist>{cap}%")
        if not dom_ok:
            reasons.append(f"not in {dominance} (rank {r['density_rank']}, cut<={cut})")
        if reasons:
            skips.append(
                f"{z.bottom:.5f}-{z.top:.5f} dist={dist:.2f}% rank={r['density_rank']} "
                f"∫D={r['integrated_density']:.6f} SKIP: {','.join(reasons)}"
            )
        elif chosen_bottom is not None and abs(z.bottom - chosen_bottom) < 1e-6:
            break
        elif chosen_bottom is None:
            pass
    why_no = ""
    if chosen_bottom is None:
        parts = []
        if not any(
            r["dist_from_entry_pct"] <= cap
            and (r["in_d20"] if dominance == "D20" else r["in_d25"])
            and r["dist_from_entry_pct"] >= 0.5
            for r in scored
        ):
            parts.append("no zone within CAP meeting dominance")
        elif not scored:
            parts.append("no D2 zones")
        else:
            parts.append("no ascending match after filters")
        why_no = "; ".join(parts)
    return {"skip_log": skips, "no_qualified_reason": why_no, "n_zones": len(scored), "d_cut": cut}


def _support_break_audit(support: LineDef | None, candles: list[dict], start_idx: int) -> dict[str, str]:
    out = {
        "break_utc": "",
        "break_close": "",
        "break_line_price": "",
        "confirm_utc": "",
        "confirm_close": "",
        "confirm_delta_pct": "",
        "exit_fill_open_utc": "",
    }
    if support is None:
        return out
    state = "MONITOR"
    break_idx = -1
    break_close = 0.0
    for j in range(start_idx, len(candles)):
        c = candles[j]
        if not support.active_for_candle(float(c["time"])):
            state = "MONITOR"
            break_idx = -1
            continue
        lp = line_price_at_close(support, c)
        if lp is None:
            continue
        if state == "MONITOR" and float(c["close"]) < lp:
            state = "BREAK_PENDING"
            break_close = float(c["close"])
            break_idx = j
            out["break_utc"] = _utc(c["time"])
            out["break_close"] = f"{break_close:.10f}"
            out["break_line_price"] = f"{lp:.10f}"
        elif state == "BREAK_PENDING" and j > break_idx:
            nc = float(c["close"])
            if nc <= break_close * (1.0 - CONFIRM_DELTA):
                out["confirm_utc"] = _utc(c["time"])
                out["confirm_close"] = f"{nc:.10f}"
                out["confirm_delta_pct"] = f"{(break_close - nc) / break_close * 100:.4f}"
                if j + 1 < len(candles):
                    out["exit_fill_open_utc"] = _utc(candles[j + 1]["time"])
                return out
            if nc >= break_close * (1.0 + CONFIRM_DELTA) or nc >= lp:
                state = "MONITOR"
                break_idx = -1
    return out


def _sl_first_bar(candles: list[dict], start_idx: int, sl: float) -> dict[str, str]:
    for j in range(start_idx, len(candles)):
        c = candles[j]
        if float(c["low"]) <= sl:
            return {
                "sl_touch_utc": _utc(c["time"]),
                "sl_ohlc": f"o={c['open']} h={c['high']} l={c['low']} c={c['close']}",
            }
    return {"sl_touch_utc": "", "sl_ohlc": ""}


def _pool_active_at_touch(touch_utc: str, zone_lo: float, zone_hi: float) -> str:
    dt = _parse_utc(touch_utc)
    if not dt:
        return "unknown"
    try:
        l1._bootstrap_import_paths()
        from research_charts.historical_pool_state_v1 import load_historical_pool_state_at_close

        st = load_historical_pool_state_at_close(
            dt, timeframe="5m", pool_detail=1, include_remaining=False
        )
        uppers = [m for m in st.active_members if str(m.get("side", "")).lower() == "upper"]
        for m in uppers:
            lo, hi = float(m["lower_bound"]), float(m["upper_bound"])
            if hi >= zone_lo and lo <= zone_hi:
                return "yes_overlap"
        return "no_overlap"
    except Exception as exc:
        return f"check_failed:{type(exc).__name__}"


def _rebuild_pending(scenario: str) -> list[dict]:
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    lines = [ln for ln in frozen["ctx"]["lines_by_id"].values()]
    mod = sys.modules[l1.__name__]
    base = frozen["pending"]
    if scenario == "BASELINE_V3AB":
        return copy.deepcopy(base)
    cap, dom, pol = _parse_scenario(scenario)
    params = RealisticTPParams(
        code=scenario,
        cap_dist_pct=cap,
        dominance=dom,  # type: ignore[arg-type]
        no_tp_policy="BLOCK" if pol == "BLOCK" else "ALLOW_NOPOOL",  # type: ignore[arg-type]
    )
    return pending_with_realistic_tp(base, lines, l1_module=mod, params=params)


def _parse_scenario(scenario: str) -> tuple[float, str, str]:
    if scenario == "BASELINE_V3AB":
        return 0.0, "", ""
    parts = scenario.split("_")
    cap = float(parts[0].replace("CAP", ""))
    dom = parts[1]
    pol = parts[2]
    return cap, dom, pol


def _chronology_new_slots(
    trades_by_scenario: dict[str, list[dict]],
) -> dict[tuple[str, str], set[str]]:
    """Entries not in baseline B chronology due to freed slots."""
    out: dict[tuple[str, str], set[str]] = {}
    bl = {t["touch_event_id"] for t in trades_by_scenario.get("BASELINE_V3AB", []) if _executed(t) and t["variant"] == VARIANT_B}
    for sc in AUDIT_SCENARIOS:
        if sc == "BASELINE_V3AB":
            continue
        for var in (VARIANT_A, VARIANT_B):
            ex = [t for t in trades_by_scenario.get(sc, []) if _executed(t) and t["variant"] == var]
            o = {t["touch_event_id"] for t in ex}
            out[(sc, var)] = o - bl
    return out


def _hit_rate_revalidation(
    pending: list[dict],
    candles: list[dict],
    lines_by_id: dict[str, LineDef],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    sim_end = len(candles)
    for row in pending:
        if not row.get("_entry_idx") or not row.get("_d2_gate_ok"):
            continue
        eid = row["touch_event_id"]
        entry_idx = int(row["_entry_idx"])
        entry = float(row["entry_price"])
        dc = datetime.strptime(row["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(
            tzinfo=timezone.utc
        )
        members = row["_pool_members"]
        scored, _ = build_zones_with_density(members, dc, entry)
        support = lines_by_id.get(row.get("touch_drawing_id") or "")
        sl = float(row.get("_sl") or entry * 0.998)

        for rec in scored:
            target = float(rec["zone"].bottom)
            dist = rec["dist_from_entry_pct"]
            if dist < 0.5:
                continue
            touch_pilot = _first_touch_idx(candles, entry_idx, sim_end, target)
            exit_kind, _, _, _, exit_idx = simulate_exit_v3y(
                candles, entry_idx, entry, sl, target, None, support
            )
            for hrs, nbars in HORIZON_BARS.items():
                touch_h = _first_touch_idx(candles, entry_idx, entry_idx + nbars, target)
                rows.append(
                    {
                        "touch_event_id": eid,
                        "target_bottom": target,
                        "dist_pct": round(dist, 4),
                        "horizon_hours": hrs,
                        "metric": "price_high_touch",
                        "value": touch_h is not None,
                        "touch_utc": _utc(candles[touch_h]["time"]) if touch_h else "",
                    }
                )
                tp_pos = exit_kind == "TP"
                rows.append(
                    {
                        "touch_event_id": eid,
                        "target_bottom": target,
                        "dist_pct": round(dist, 4),
                        "horizon_hours": hrs,
                        "metric": "executable_tp_simulated",
                        "value": tp_pos and touch_h is not None and touch_h <= (exit_idx or 10**9),
                        "touch_utc": "",
                    }
                )
            rows.append(
                {
                    "touch_event_id": eid,
                    "target_bottom": target,
                    "dist_pct": round(dist, 4),
                    "horizon_hours": "pilot_end",
                    "metric": "price_high_touch",
                    "value": touch_pilot is not None,
                    "touch_utc": _utc(candles[touch_pilot]["time"]) if touch_pilot else "",
                }
            )
            rows.append(
                {
                    "touch_event_id": eid,
                    "target_bottom": target,
                    "dist_pct": round(dist, 4),
                    "horizon_hours": "pilot_end",
                    "metric": "executable_tp_simulated",
                    "value": exit_kind == "TP",
                    "touch_utc": "",
                }
            )
            adv = touch_pilot is not None and exit_kind != "TP" and (
                exit_idx is None or touch_pilot < exit_idx
            )
            rows.append(
                {
                    "touch_event_id": eid,
                    "target_bottom": target,
                    "dist_pct": round(dist, 4),
                    "horizon_hours": "pilot_end",
                    "metric": "touch_before_adverse_not_tp",
                    "value": adv,
                    "touch_utc": _utc(candles[touch_pilot]["time"]) if touch_pilot else "",
                }
            )
    return rows


def main() -> int:
    l1._bootstrap_import_paths()
    frozen = build_context_from_snapshot(DEFAULT_SNAPSHOT, expected_sha256=FROZEN_SHA256)
    candles = frozen["candles"]
    lines_by_id = frozen["lines_by_id"]
    pmap_base = {r["touch_event_id"]: r for r in frozen["pending"] if r.get("touch_event_id")}

    all_trades = _read_trades()
    trades_by_sc: dict[str, list[dict]] = defaultdict(list)
    for t in all_trades:
        if t.get("scenario") in AUDIT_SCENARIOS:
            trades_by_sc[t["scenario"]].append(t)

    new_slots = _chronology_new_slots(trades_by_sc)

    # --- trade list tables ---
    trade_list_rows: list[dict] = []
    chart_rows: list[dict] = []
    tp_audit_rows: list[dict] = []
    trade_no = 0

    for scenario in AUDIT_SCENARIOS:
        for variant in (VARIANT_A, VARIANT_B):
            ex = sorted(
                [t for t in trades_by_sc.get(scenario, []) if t["variant"] == variant and _executed(t)],
                key=lambda x: x["entry_utc"],
            )
            pending = _rebuild_pending(scenario) if scenario != "BASELINE_V3AB" else None
            pend_map = {r["touch_event_id"]: r for r in (pending or frozen["pending"])}
            for t in ex:
                trade_no += 1
                eid = t["touch_event_id"]
                prow = pend_map.get(eid) or pmap_base.get(eid, {})
                entry_idx = int(prow.get("_entry_idx") or _candle_idx(candles, t["entry_utc"]) or 0)
                support = lines_by_id.get(t.get("support_drawing_id") or t.get("touch_drawing_id") or "")
                tp_px = t.get("tp_price") or ""
                sl_px = t.get("sl_price") or ""
                tp_f = float(tp_px) if tp_px else None
                exit_reason = t.get("exit_reason") or ""

                hold_bars = ""
                if t.get("exit_utc"):
                    ei = _candle_idx(candles, t["entry_utc"])
                    xj = _candle_idx(candles, t["exit_utc"])
                    if ei is not None and xj is not None:
                        hold_bars = str(xj - ei)

                tp_touch = ""
                tp_ohlc = ""
                if tp_f and entry_idx is not None:
                    ti = _first_touch_idx(candles, entry_idx, len(candles), tp_f)
                    if ti is not None:
                        tp_touch = _utc(candles[ti]["time"])
                        c = candles[ti]
                        tp_ohlc = f"o={c['open']} h={c['high']} l={c['low']} c={c['close']}"

                sb = _support_break_audit(support, candles, entry_idx)
                sl_info = _sl_first_bar(candles, entry_idx, float(sl_px) if sl_px else float(prow.get("_sl", 0)))

                cap, dom, pol = _parse_scenario(scenario) if scenario != "BASELINE_V3AB" else (None, None, None)
                meta = {}
                if prow.get("_v3ah_meta"):
                    try:
                        meta = json.loads(prow["_v3ah_meta"])
                    except json.JSONDecodeError:
                        pass
                dc = datetime.strptime(
                    prow["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC"
                ).replace(tzinfo=timezone.utc) if prow.get("_pool_decision_close") else None
                sel = {}
                if dc and prow.get("_pool_members") and cap:
                    sel = _tp_selection_narrative(
                        prow["_pool_members"],
                        dc,
                        float(t["entry_price"]),
                        cap=cap,
                        dominance=dom or "D20",
                        chosen_bottom=_safe_float(meta.get("tp_bottom")),
                    )
                _, _, qualified = (
                    analyze_d2_landscape(prow["_pool_members"], dc, float(t["entry_price"]))
                    if dc and prow.get("_pool_members")
                    else (None, [], [])
                )
                zlo = meta.get("tp_bottom") or meta.get("zone_bottom") or t.get("zone_bottom") or ""
                zhi = meta.get("zone_top") or t.get("zone_top") or ""
                if zlo in ("", "None", None) and tp_px:
                    zlo = tp_px
                if zhi in ("", "None", None):
                    zhi = ""
                avg_d = ""
                if zlo and zhi and zlo not in ("None",) and qualified:
                    ad = _avg_depth_in_zone(float(zlo), float(zhi), qualified)
                    avg_d = f"{ad:.4f}" if ad is not None else ""

                pool_active_entry = "yes" if prow.get("_d2_gate_ok") else "no"
                zlo_f, zhi_f = _safe_float(zlo), _safe_float(zhi)
                pool_touch = (
                    _pool_active_at_touch(tp_touch, zlo_f, zhi_f)
                    if tp_touch and zlo_f is not None and zhi_f is not None
                    else ""
                )

                trade_list_rows.append(
                    {
                        "trade_no": trade_no,
                        "model": scenario,
                        "variant": variant,
                        "entry_utc": t["entry_utc"],
                        "entry_price": t["entry_price"],
                        "support_id": t.get("support_drawing_id", ""),
                        "tp_price": tp_px,
                        "sl_price": sl_px,
                        "exit_utc": t.get("exit_utc", ""),
                        "exit_price": t.get("exit_price", ""),
                        "exit_reason": exit_reason,
                        "pnl_pct": t.get("pnl_pct", ""),
                        "hold_bars_5m": hold_bars,
                        "new_slot_vs_baseline_B": "yes" if eid in new_slots.get((scenario, variant), set()) else "no",
                    }
                )

                tp_audit_rows.append(
                    {
                        "scenario": scenario,
                        "variant": variant,
                        "touch_event_id": eid,
                        "policy": pol or "baseline",
                        "tp_zone_lower": zlo,
                        "tp_zone_upper": zhi,
                        "tp_price": tp_px,
                        "dist_entry_pct": t.get("dist_entry_pct") or meta.get("dist_pct", ""),
                        "integrated_density": t.get("integrated_density") or meta.get("integrated_density", ""),
                        "density_rank": t.get("density_rank") or meta.get("density_rank", ""),
                        "max_depth": meta.get("max_depth", ""),
                        "avg_depth_weighted": avg_d,
                        "unique_origins": meta.get("unique_origins", t.get("zone_top", "")),
                        "cap_dominance": f"{cap}% {dom}" if cap else "max D2 V3AB",
                        "no_qualified_reason": meta.get("reason") or sel.get("no_qualified_reason", ""),
                        "skip_nearer_farther": " | ".join(sel.get("skip_log", [])[:8]),
                        "pool_active_entry_ssot": pool_active_entry,
                        "no_qualified_tp_flag": t.get("no_qualified_tp", ""),
                    }
                )

                chart_rows.append(
                    {
                        "scenario": scenario,
                        "variant": variant,
                        "trade_no": trade_no,
                        "entry_utc": t["entry_utc"],
                        "entry_price": t["entry_price"],
                        "support_id": t.get("support_drawing_id", ""),
                        "tp_zone_lower": zlo,
                        "tp_zone_upper": zhi,
                        "tp_price": tp_px,
                        "sl_price": sl_px,
                        "tp_touch_utc": tp_touch,
                        "break_utc": sb["break_utc"],
                        "confirm_utc": sb["confirm_utc"],
                        "exit_utc": t.get("exit_utc", ""),
                        "exit_price": t.get("exit_price", ""),
                        "exit_reason": exit_reason,
                        "pnl_pct": t.get("pnl_pct", ""),
                        "tp_touch_ohlc": tp_ohlc,
                        "pool_active_at_tp_touch": pool_touch,
                        "sl_touch_utc": sl_info["sl_touch_utc"],
                        "exit_fill_open_utc": sb["exit_fill_open_utc"],
                        "retrospective_drawing": "RETROSPECTIVE_DRAWING",
                    }
                )

    # BLOCK vs ALLOW: non-executed NO_QUALIFIED_TP
    block_rows: list[dict] = []
    for scenario in AUDIT_SCENARIOS:
        if "BLOCK" not in scenario:
            continue
        allow_sc = scenario.replace("_BLOCK", "_ALLOW")
        cap, dom, _ = _parse_scenario(scenario)
        pend_b = {r["touch_event_id"]: r for r in _rebuild_pending(scenario)}
        pend_a = {r["touch_event_id"]: r for r in _rebuild_pending(allow_sc)}
        for eid, rb in pend_b.items():
            if rb.get("reject_reason") == "NO_QUALIFIED_TP" and rb.get("long_trigger_allowed") == "yes":
                ra = pend_a.get(eid, {})
                block_rows.append(
                    {
                        "touch_event_id": eid,
                        "block_scenario": scenario,
                        "allow_scenario": allow_sc,
                        "entry_utc": rb.get("entry_utc", ""),
                        "allow_has_tp": "yes" if ra.get("_pool_tp") else "no",
                        "allow_tp_mode": ra.get("tp_mode", ""),
                    }
                )

    hit_detail = _hit_rate_revalidation(frozen["pending"], candles, lines_by_id)
    hit_summary: list[dict] = []
    for hrs in list(HORIZON_BARS.keys()) + ["pilot_end"]:
        for metric in ("price_high_touch", "executable_tp_simulated", "touch_before_adverse_not_tp"):
            sub = [r for r in hit_detail if r["horizon_hours"] == hrs and r["metric"] == metric]
            if not sub:
                continue
            hit_summary.append(
                {
                    "horizon_hours": hrs,
                    "metric": metric,
                    "n_rows": len(sub),
                    "true_count": sum(1 for r in sub if r["value"]),
                    "rate_pct": round(100.0 * sum(1 for r in sub if r["value"]) / len(sub), 2),
                    "definition": _metric_definition(metric, hrs),
                }
            )

    # Variant C check
    var_c = [t for t in all_trades if t["variant"] == "V3E-C" and _executed(t)]

    ref_table = _reference_trade_table(all_trades, pmap_base, candles, lines_by_id)

    _write(OUT / "l1_v3ai_chart_review.csv", chart_rows)
    _write(OUT / "l1_v3ai_tp_selection_audit.csv", tp_audit_rows)
    _write(OUT / "l1_v3ai_hit_rate_validation.csv", hit_summary + [{"note": "detail rows", "count": len(hit_detail)}])
    _write(OUT / "l1_v3ai_trade_list.csv", trade_list_rows)
    _write(OUT / "l1_v3ai_block_vs_allow.csv", block_rows)

    report = _build_report(
        trade_list_rows,
        hit_summary,
        block_rows,
        var_c,
        ref_table,
        new_slots,
    )
    (OUT / "L1_V3AI_V3AH_FULL_AUDIT.md").write_text(report, encoding="utf-8")
    print(OUT / "L1_V3AI_V3AH_FULL_AUDIT.md")
    return 0


def _metric_definition(metric: str, hrs: Any) -> str:
    if metric == "price_high_touch":
        return f"First 5m bar with high>=target bottom, within {hrs}h from entry (or pilot_end)"
    if metric == "executable_tp_simulated":
        return f"simulate_exit_v3y returns TP with that target before SL/SBE"
    return "high touch before adverse exit but exit_kind!=TP"


def _reference_trade_table(
    trades: list[dict],
    pmap: dict,
    candles: list[dict],
    lines_by_id: dict,
) -> list[dict]:
    rows = []
    prow = pmap.get(REF_EID, {})
    dc = datetime.strptime(prow["_pool_decision_close"], "%Y-%m-%d %H:%M:%S UTC").replace(
        tzinfo=timezone.utc
    )
    entry = float(prow["entry_price"])
    for scenario in AUDIT_SCENARIOS:
        for var in (VARIANT_B,):
            t = next(
                (
                    x
                    for x in trades
                    if x["touch_event_id"] == REF_EID and x["scenario"] == scenario and x["variant"] == var
                ),
                None,
            )
            cap, dom, pol = _parse_scenario(scenario) if scenario != "BASELINE_V3AB" else (0, "", "")
            res = (
                resolve_realistic_tp(prow["_pool_members"], dc, entry, cap_dist_pct=cap, dominance=dom)  # type: ignore
                if cap and dom
                else None
            )
            rows.append(
                {
                    "scenario": scenario,
                    "tp_rule": res.tp_bottom if res and res.ok else "NO_QUALIFIED_TP",
                    "actual_tp": t.get("tp_price", "") if t else "",
                    "exit": t.get("exit_reason", "") if t else "not_traded",
                    "pnl": t.get("pnl_pct", "") if t else "",
                }
            )
    return rows


def _write(path: Path, rows: list[dict]) -> None:
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


def _build_report(
    trades: list[dict],
    hit_summary: list[dict],
    block_rows: list[dict],
    var_c: list,
    ref_table: list[dict],
    new_slots: dict,
) -> str:
    lines = [
        "# L1 V3AI — Vollständiger V3AH-Audit (READ-ONLY)",
        "",
        "Quelle: `realistic_pool_tp_v3ah/` — **keine** Änderungen an V3AH-Logik oder bestehenden CSVs.",
        "",
        "## 1. Trade-Übersicht (Modelle mit V3E-B)",
        "",
        "| Nr | Modell | Entry UTC | TP | Exit | PnL % |",
        "|---:|---|---|---|---|---:|",
    ]
    for row in trades:
        if row["variant"] != VARIANT_B:
            continue
        lines.append(
            f"| {row['trade_no']} | {row['model']} | {row['entry_utc']} | {row['tp_price'] or '—'} | "
            f"{row['exit_reason']} | {row['pnl_pct']} |"
        )
    lines.extend(["", "## 2. Variante C", "", f"Ausgeführte Trades V3E-C: **{len(var_c)}** (erwartet 0).", ""])
    lines.extend(["## 3. Referenztrade", ""])
    for r in ref_table:
        lines.append(f"- **{r['scenario']}**: Regel-TP `{r['tp_rule']}` · IST `{r['actual_tp']}` · {r['exit']} · {r['pnl']}%")
    lines.extend(["", "## 4. 100%-Touch — Ursache & korrigierte Metriken", ""])
    lines.extend(
        [
            "### Definition in `hit_rate.py` (V3AH)",
            "",
            "1. **Touch** = erste 5m-Kerze mit `high >= zone.bottom` ab `_entry_idx`.",
            "2. Scan bis **Pilot-Ende** (`simulation_end`, 2026-10-19), **nicht** bis Trade-Exit.",
            "3. Jede **Merge-Zone** pro Entry = eigener Kandidat → Mehrfachzählung über Entries.",
            "4. **Keine** ACTIVE-Pool-Prüfung am Touch-Zeitpunkt.",
            "5. `hit_rate_pct` = Anteil Kandidaten mit jemals erreichtem High — daher oft **100 %** bei langem Fenster.",
            "",
            "### Drei getrennte Kennzahlen (Neuberechnung V3AI)",
            "",
            "| Horizon | Metrik | N | True | Rate % |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for h in hit_summary:
        if h.get("note"):
            continue
        lines.append(
            f"| {h['horizon_hours']} | {h['metric']} | {h['n_rows']} | {h['true_count']} | {h['rate_pct']} |"
        )
    lines.extend(["", "## 5. BLOCK vs ALLOW", ""])
    lines.append(f"BLOCKiert (NO_QUALIFIED_TP, long erlaubt): **{len(block_rows)}** Pending-Rows.")
    for b in block_rows[:15]:
        lines.append(f"- `{b['touch_event_id']}` @ {b['entry_utc']} ({b['block_scenario']})")
    if len(block_rows) > 15:
        lines.append(f"- … +{len(block_rows) - 15} weitere")
    lines.extend(["", "## 6. Charts zuerst prüfen", ""])
    prio = [
        ("2026-08-26 20:30 UTC", "Referenz Entry; CAP6 TP 08:45 vs SBE 13:50"),
        ("2026-08-27 08:45 UTC", "TP-Touch Violettzone CAP6"),
        ("2026-08-27 13:50 UTC", "Supportbruch-Bestätigung Referenz"),
    ]
    for utc, why in prio:
        lines.append(f"- **{utc}** — {why}")
    lines.extend(
        [
            "",
            "## Artefakte",
            "",
            "- `l1_v3ai_chart_review.csv`",
            "- `l1_v3ai_tp_selection_audit.csv`",
            "- `l1_v3ai_hit_rate_validation.csv`",
            "- `l1_v3ai_trade_list.csv`",
            "- `l1_v3ai_block_vs_allow.csv`",
            "",
            "**STOP**",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
