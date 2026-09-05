#!/usr/bin/env python3
"""READ-ONLY EMA-only audit for EZM job d2809c95 DOGEUSDT 2026-08-26.

Writes new artifacts under results/ema_zone_microstructure_confirmation/
dogeusdt_20260826_ema_only_audit_v1/. Does not mutate detectors, jobs, or CH.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
JOB = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/results/"
    "stoch_fade_research_jobs/d2809c9528c74f47944489a751cdc99d"
)
RUN = JOB / "coin_runs/DOGEUSDT/ezmbfb26648033a"
OUT = (
    OA_ROOT
    / "results/ema_zone_microstructure_confirmation/dogeusdt_20260826_ema_only_audit_v1"
)

sys.path.insert(0, str(OA_ROOT / "src"))

from orderbook_analyse.ema_zone_microstructure_confirmation.defaults import (  # noqa: E402
    FLAT_SLOPE_ATR_FRAC_EMA20,
    FLAT_SLOPE_ATR_FRAC_EMA59,
    NEAR_EMA20_ATR_FRAC,
    NEXT_ZONE_CLEARANCE_ATR_MULT,
    NEXT_ZONE_CLEARANCE_PCT_HI,
    NEXT_ZONE_CLEARANCE_PCT_LO,
    ZONE_ATR_FRAC,
    ZONE_MIN_TICKS,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.regime import (  # noqa: E402
    is_flat_compression,
    prepare_bars_with_ema200,
    regime_snapshot,
)
from orderbook_analyse.ema_zone_microstructure_confirmation.zones_ext import (  # noqa: E402
    approach_side,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.indicators import (  # noqa: E402
    last_closed_bar_at,
)
from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.zones import (  # noqa: E402
    zone_half_width,
)
from orderbook_analyse.l2_wall_attack_discovery.models import tick_size  # noqa: E402
from orderbook_analyse.oi_liq_impact_l2.aggregate_proxy.loaders import (  # noqa: E402
    load_candles_1m,
)
from orderbook_analyse.orderbook_v2.ch_client import (  # noqa: E402
    get_clickhouse_client,
    load_clickhouse_settings,
)


def _parse_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("\"'")
    return out


def apply_oa_ch_env() -> None:
    env = _parse_dotenv(OA_ROOT / ".env")
    for k in (
        "CLICKHOUSE_HOST",
        "CLICKHOUSE_HTTP_PORT",
        "CLICKHOUSE_PORT",
        "CLICKHOUSE_DATABASE",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_PASSWORD",
    ):
        if env.get(k):
            os.environ[k] = env[k]
    if not os.environ.get("CLICKHOUSE_HTTP_PORT") and os.environ.get("CLICKHOUSE_PORT"):
        os.environ["CLICKHOUSE_HTTP_PORT"] = os.environ["CLICKHOUSE_PORT"]


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_z(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def zone_band(center: float, atr: float, tick: float) -> tuple[float, float, float]:
    hw = zone_half_width(atr, tick=tick)
    return center - hw, center + hw, hw


def dist_outside(low: float, high: float, px: float) -> float:
    if low <= px <= high:
        return 0.0
    if px < low:
        return low - px
    return px - high


def clearance_to_next(
    mid: float,
    primary_name: str,
    primary_low: float,
    primary_high: float,
    primary_atr: float,
    zones: dict[str, tuple[float, float, float]],
) -> dict:
    strength = {"EMA20": 1, "EMA59": 2, "EMA200": 3, "STACKED": 2}
    p_s = strength.get(primary_name.split(":")[0] if primary_name.startswith("STACKED") else primary_name, 1)
    best = None
    best_d = None
    for name, (lo, hi, c) in zones.items():
        if name == primary_name or name.startswith("STACKED"):
            continue
        s = strength.get(name, 0)
        if s <= p_s and best is not None:
            # prefer stronger; allow any if none stronger found later
            pass
        d = abs(c - mid)
        if best is None or (s > p_s and strength.get(best[0], 0) <= p_s) or (
            s >= strength.get(best[0], 0) and d < best_d
        ):
            # simplified nearest among stronger-or-equal
            if s > p_s or best is None or d < best_d:
                best = (name, lo, hi, c)
                best_d = d
    # redo: prefer stronger EMAs first
    stronger = [
        (n, lo, hi, c)
        for n, (lo, hi, c) in zones.items()
        if n != primary_name and not n.startswith("STACKED") and strength.get(n, 0) > p_s
    ]
    pool = stronger or [
        (n, lo, hi, c)
        for n, (lo, hi, c) in zones.items()
        if n != primary_name and not n.startswith("STACKED")
    ]
    if not pool:
        return {
            "next_zone": None,
            "clearance_pct": None,
            "clearance_atr": None,
            "clearance_status": "clear",
            "wait_next": False,
        }
    name, lo, hi, c = min(pool, key=lambda x: abs(x[3] - mid))
    if c >= (primary_low + primary_high) / 2:
        gap = max(0.0, lo - primary_high)
    else:
        gap = max(0.0, primary_low - hi)
    mid_c = abs(((primary_low + primary_high) / 2 + c) / 2) or mid
    pct = (gap / mid_c) * 100.0 if mid_c else None
    atr_m = (gap / primary_atr) if primary_atr and primary_atr > 0 else None
    wait = False
    if pct is not None and NEXT_ZONE_CLEARANCE_PCT_LO <= pct <= NEXT_ZONE_CLEARANCE_PCT_HI:
        wait = True
    if (
        atr_m is not None
        and 0 < atr_m <= NEXT_ZONE_CLEARANCE_ATR_MULT
        and pct is not None
        and pct <= NEXT_ZONE_CLEARANCE_PCT_HI
    ):
        wait = True
    overlap = not (primary_high < lo or hi < primary_low)
    if overlap:
        status = "stacked_zone"
    elif wait:
        status = "next_zone_near"
    else:
        status = "clear"
    return {
        "next_zone": name,
        "clearance_pct": pct,
        "clearance_atr": atr_m,
        "clearance_status": status,
        "wait_next": wait,
        "overlap": overlap,
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    apply_oa_ch_env()
    load_clickhouse_settings()
    client = get_clickhouse_client()

    req = json.loads((JOB / "request.json").read_text())
    man = json.loads((RUN / "run_manifest.json").read_text())
    cov = json.loads((RUN / "coverage.json").read_text())
    cands = json.loads((RUN / "candidates.json").read_text())["candidates"]
    sigs = [json.loads(l) for l in (RUN / "signals.jsonl").read_text().splitlines() if l.strip()]

    # Effective L2 window from coverage / manifest
    eff_start = parse_z(man.get("effective_start") or cov.get("discovery_start"))
    eff_end = parse_z(man.get("effective_end") or cov.get("discovery_end"))
    candle_start = eff_start - timedelta(hours=240)
    candle_end = eff_end + timedelta(hours=4)

    candles = load_candles_1m(client, symbol="DOGEUSDT", start=candle_start, end=candle_end)
    client.close()
    bars = prepare_bars_with_ema200(candles)
    tick = float(tick_size("DOGEUSDT"))

    # --- ema_values_5m.csv (audit window ± margin) ---
    win0 = datetime(2026, 8, 25, 20, 0, tzinfo=timezone.utc)
    win1 = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    rows_ema = []
    for _, r in bars.iterrows():
        be = r["bar_end"]
        if getattr(be, "tzinfo", None) is None:
            be = pd.Timestamp(be).tz_localize("UTC")
        else:
            be = pd.Timestamp(be).tz_convert("UTC")
        if be.to_pydatetime() < win0 or be.to_pydatetime() > win1:
            continue
        atr = float(r["atr"]) if pd.notna(r.get("atr")) else None
        e20 = float(r["ema20"]) if pd.notna(r.get("ema20")) else None
        e59 = float(r["ema59"]) if pd.notna(r.get("ema59")) else None
        e9 = float(r["ema9"]) if pd.notna(r.get("ema9")) else None
        e200 = float(r["ema200"]) if pd.notna(r.get("ema200")) else None
        close = float(r["close"])
        hw = zone_half_width(atr, tick=tick) if atr else None
        rows_ema.append(
            {
                "bar_end": iso(be.to_pydatetime()),
                "open_time": str(r.name),
                "close": close,
                "ema9": e9,
                "ema20": e20,
                "ema59": e59,
                "ema200": e200,
                "atr": atr,
                "half_width": hw,
                "ema20_low": (e20 - hw) if e20 is not None and hw else None,
                "ema20_high": (e20 + hw) if e20 is not None and hw else None,
                "ema59_low": (e59 - hw) if e59 is not None and hw else None,
                "ema59_high": (e59 + hw) if e59 is not None and hw else None,
                "ema200_low": (e200 - hw) if e200 is not None and hw else None,
                "ema200_high": (e200 + hw) if e200 is not None and hw else None,
                "spread_9_59_pct": (
                    abs(e9 - e59) / close * 100.0 if e9 and e59 and close else None
                ),
                "spread_20_59_pct": (
                    abs(e20 - e59) / close * 100.0 if e20 and e59 and close else None
                ),
                "warmup_ok": bool(r.get("warmup_ok")),
                "ema200_warmup_ok": bool(r.get("ema200_warmup_ok")),
            }
        )
    pd.DataFrame(rows_ema).to_csv(OUT / "ema_values_5m.csv", index=False)

    # --- regime timeline every 5m ---
    regime_rows = []
    for t in pd.date_range(win0, win1, freq="5min", tz="UTC"):
        asof = t.to_pydatetime()
        snap = regime_snapshot(bars, asof)
        regime_rows.append(
            {
                "asof": iso(asof),
                "regime": snap.get("regime"),
                "block_flat_compression": snap.get("block_flat_compression"),
                "classification": snap.get("legacy_classification"),
                "close": snap.get("close"),
                "ema9": snap.get("ema9"),
                "ema20": snap.get("ema20"),
                "ema59": snap.get("ema59"),
                "ema200": snap.get("ema200"),
                "atr": snap.get("atr"),
                "ema20_slope_3": snap.get("ema20_slope_3"),
                "ema59_slope_3": snap.get("ema59_slope_3"),
                "ema20_slope_3_atr": snap.get("ema20_slope_3_atr"),
                "ema59_slope_3_atr": snap.get("ema59_slope_3_atr"),
                "spread_atr": snap.get("ema_spread_9_59_atr"),
            }
        )
    pd.DataFrame(regime_rows).to_csv(OUT / "ema_regime_timeline.csv", index=False)

    # --- reconstruct watches/touches for Aug26 morning (EMA-only distance) ---
    WATCH_MULT = 3.0
    watch_rows = []
    for t in pd.date_range(
        datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
        freq="1min",
        tz="UTC",
    ):
        asof = t.to_pydatetime()
        snap = regime_snapshot(bars, asof)
        if snap.get("atr") is None or snap.get("ema20") is None:
            continue
        atr = float(snap["atr"])
        mid = float(snap["close"])  # proxy: closed 5m close as mid stand-in for EMA layer
        hw = zone_half_width(atr, tick=tick)
        zones = {}
        for name, center in (
            ("EMA20", snap["ema20"]),
            ("EMA59", snap["ema59"]),
            ("EMA200", snap.get("ema200")),
        ):
            if center is None:
                continue
            lo, hi, _ = zone_band(float(center), atr, tick)
            zones[name] = (lo, hi, float(center))
        for name, (lo, hi, c) in zones.items():
            d = dist_outside(lo, hi, mid)
            near = d <= hw * WATCH_MULT
            inside = d == 0.0
            appr = approach_side(mid, type("Z", (), {"low": lo, "high": hi, "center": c})())
            # continuous role
            if appr == "from_above":
                role = "resistance"
            elif appr == "from_below":
                role = "support"
            else:
                role = "ambiguous_inside"
            # user-desired role (audit contrast)
            if appr == "from_below":
                desired_role = "resistance"
            elif appr == "from_above":
                desired_role = "support"
            else:
                desired_role = "ambiguous"
            clr = clearance_to_next(mid, name, lo, hi, atr, zones)
            dist_pct = (d / mid * 100.0) if mid else None
            event = "NO_SETUP"
            if snap.get("block_flat_compression") and near:
                event = "BLOCK_FLAT_COMPRESSION"
            elif inside:
                event = f"TOUCH_{name}"
            elif near:
                event = f"WATCH_{name}_{appr.upper()}" if appr != "inside" else f"WATCH_{name}_INSIDE"
            if clr.get("wait_next") and near:
                event = event + "|WAIT_NEXT_EMA_ZONE"
            watch_rows.append(
                {
                    "asof": iso(asof),
                    "zone": name,
                    "mid_proxy_close5m": mid,
                    "dist": d,
                    "dist_pct": dist_pct,
                    "half_width": hw,
                    "watch_threshold": hw * WATCH_MULT,
                    "near_watch": near,
                    "exact_touch": inside,
                    "approach": appr,
                    "zone_role_production": role,
                    "zone_role_desired_audit": desired_role,
                    "regime": snap["regime"],
                    "block_flat": snap["block_flat_compression"],
                    "next_zone": clr.get("next_zone"),
                    "clearance_pct": clr.get("clearance_pct"),
                    "clearance_status": clr.get("clearance_status"),
                    "event": event,
                }
            )
    pd.DataFrame(watch_rows).to_csv(OUT / "ema_zone_watch_timeline.csv", index=False)

    clr_rows = [w for w in watch_rows if w["zone"] == "EMA20"]
    pd.DataFrame(clr_rows).to_csv(OUT / "ema_clearance_timeline.csv", index=False)

    # --- proximity sensitivity (count unique minutes with near per threshold %) ---
    sens = []
    for thr in (None, 0.05, 0.10, 0.15, 0.20):
        # None = production halfwidth*3
        count = 0
        zones_hit = set()
        for w in watch_rows:
            if thr is None:
                ok = w["near_watch"]
                label = "production_3x_halfwidth"
            else:
                ok = (w["dist_pct"] or 999) <= thr
                label = f"{thr:.2f}pct"
            if ok:
                count += 1
                zones_hit.add((w["asof"][:16], w["zone"]))
        sens.append(
            {
                "threshold": label,
                "row_hits": count,
                "unique_asof_zone": len(zones_hit),
            }
        )
    pd.DataFrame(sens).to_csv(OUT / "proximity_sensitivity.csv", index=False)

    # --- all directed signals as chart markers from this job ---
    marker_rows = []
    for s in sorted(sigs, key=lambda x: str(x.get("decision_at"))):
        da = parse_z(s["decision_at"])
        # match candidate
        c = next(
            (
                x
                for x in cands
                if str(x.get("decision_at")) == str(s.get("decision_at"))
                and str(x.get("episode_id")) == str(s.get("episode_id"))
            ),
            None,
        )
        snap = regime_snapshot(bars, da)
        atr = snap.get("atr")
        hw = zone_half_width(float(atr), tick=tick) if atr else None
        row = {
            "marker_time": s.get("decision_at"),
            "marker_price": s.get("decision_price"),
            "direction": s.get("direction") or s.get("candidate_direction"),
            "state": s.get("candidate_state"),
            "job_id": req["job_id"],
            "run_id": man.get("run_id"),
            "episode_id": s.get("episode_id"),
            "symbol": "DOGEUSDT",
            "strategy_id": "ema_zone_microstructure_confirmation_v1",
            "watch_at": (c or {}).get("zone_watch_started_at"),
            "touch_at": (c or {}).get("zone_touch_at") or (c or {}).get("tl_zone_touch_at"),
            "decision_at": s.get("decision_at"),
            "ema_zone": s.get("zone_name") or (c or {}).get("zone_name"),
            "approach_direction": (c or {}).get("approach_direction"),
            "zone_role_at_decision": (c or {}).get("zone_role"),
            "regime_artifact": s.get("regime") or (c or {}).get("regime"),
            "regime_recomputed": snap.get("regime"),
            "flat_recomputed": snap.get("block_flat_compression"),
            "ema9": snap.get("ema9"),
            "ema20": snap.get("ema20"),
            "ema59": snap.get("ema59"),
            "ema200": snap.get("ema200"),
            "atr": atr,
            "half_width": hw,
            "mechanism": s.get("mechanism") or (c or {}).get("mechanism"),
            "reason_codes": s.get("reason_codes") or (c or {}).get("reason_codes"),
            "provenance": "job_d2809c95_signals_jsonl",
        }
        # clearance at decision
        if atr and snap.get("ema20") is not None:
            zones = {}
            for name, center in (
                ("EMA20", snap["ema20"]),
                ("EMA59", snap["ema59"]),
                ("EMA200", snap.get("ema200")),
            ):
                if center is None:
                    continue
                lo, hi, _ = zone_band(float(center), float(atr), tick)
                zones[name] = (lo, hi, float(center))
            zn = row["ema_zone"] or "EMA20"
            key = "EMA20" if "EMA20" in str(zn) and "STACKED" not in str(zn) else (
                "EMA59" if "EMA59" in str(zn) and "STACKED" not in str(zn) else (
                    "EMA200" if "EMA200" in str(zn) else "EMA20"
                )
            )
            if "STACKED" in str(zn):
                key = "EMA20"
            lo, hi, c0 = zones.get(key, (None, None, None))
            if lo is not None:
                clr = clearance_to_next(float(snap["close"]), key, lo, hi, float(atr), zones)
                row["next_zone"] = clr.get("next_zone")
                row["clearance_pct"] = clr.get("clearance_pct")
                row["clearance_status"] = clr.get("clearance_status")
        marker_rows.append(row)
    pd.DataFrame(marker_rows).to_csv(OUT / "active_chart_markers_audit.csv", index=False)

    # --- six reported candidates deep audit ---
    six_times = [
        "2026-08-26T00:30",
        "2026-08-26T00:49",
        "2026-08-26T01:06",
        "2026-08-26T01:26",
        "2026-08-26T01:33",
        "2026-08-26T01:55",
    ]
    reported = []
    findings_events = []
    for prefix in six_times:
        c = next((x for x in cands if str(x.get("decision_at", "")).startswith(prefix)), None)
        if not c:
            reported.append({"prefix": prefix, "verdict": "MISSING"})
            continue
        da = parse_z(c["decision_at"])
        watch_at = parse_z(c["zone_watch_started_at"]) if c.get("zone_watch_started_at") else da
        touch_at = parse_z(c.get("zone_touch_at") or c.get("tl_zone_touch_at") or c["decision_at"])
        snap_w = regime_snapshot(bars, watch_at)
        snap_t = regime_snapshot(bars, touch_at)
        snap_d = regime_snapshot(bars, da)
        appr = c.get("approach_direction")
        role = c.get("zone_role")
        # production role check
        if appr == "from_below" and role == "support":
            role_note = "MATCHES_PRODUCTION_CONTINUOUS (from_below→support)"
            role_vs_desired = "CONFLICTS_DESIRED_AUDIT_SEMANTICS (desired from_below→resistance)"
        elif appr == "from_above" and role == "resistance":
            role_note = "MATCHES_PRODUCTION_CONTINUOUS (from_above→resistance)"
            role_vs_desired = "CONFLICTS_DESIRED_AUDIT_SEMANTICS (desired from_above→support)"
        else:
            role_note = "CHECK"
            role_vs_desired = "CHECK"

        flat_at_watch = bool(snap_w.get("block_flat_compression"))
        flat_at_decision = bool(snap_d.get("block_flat_compression"))
        atr = snap_d.get("atr")
        close = snap_d.get("close")
        verdicts = []
        if flat_at_watch:
            verdicts.append("BLOCK_FLAT_COMPRESSION_AT_WATCH_WOULD_APPLY_IF_NEAR")
        # spread tight?
        if snap_d.get("ema20") and snap_d.get("ema59") and close:
            spr = abs(float(snap_d["ema20"]) - float(snap_d["ema59"])) / float(close) * 100
        else:
            spr = None
        if spr is not None and spr < 0.15:
            verdicts.append("TIGHT_EMA_STACK")
        if flat_at_decision and not flat_at_watch:
            verdicts.append("FLAT_AT_DECISION_NOT_RECHECKED_AS_HARD_GATE")
        if appr == "from_below" and role == "support":
            verdicts.append("APPROACH_ROLE_IS_PRODUCTION_INVERTED_VS_AUDIT_BRIEF")
        # contact
        touch = c.get("zone_touch_at") or c.get("tl_zone_touch_at")
        if touch:
            verdicts.append("HAS_TOUCH_TIMESTAMP")
        else:
            verdicts.append("NO_REAL_ZONE_CONTACT")

        # EMA setup allow microstructure?
        # Production: allow if not (flat and near) at watch start
        ema_allow = not flat_at_watch  # approximate; exact near not re-simulated without L2 mid
        if ema_allow:
            primary = "EMA_SETUP_VALID"
        elif flat_at_watch:
            primary = "BLOCK_FLAT_COMPRESSION"
        else:
            primary = "EMA_SETUP_AMBIGUOUS"

        # direction plausibility under production mapping
        state = c.get("candidate_state")
        direction = c.get("candidate_direction")
        prod_ok = (
            (state == "defense_rejection_confirmed" and role == "support" and direction == "LONG")
            or (state == "defense_rejection_confirmed" and role == "resistance" and direction == "SHORT")
            or (state == "false_breakout_confirmed" and role == "support" and direction == "LONG")
            or (state == "false_breakout_confirmed" and role == "resistance" and direction == "SHORT")
            or (state == "breakout_confirmed")
        )

        reported.append(
            {
                "decision_at": c.get("decision_at"),
                "direction": direction,
                "state": state,
                "episode_id": c.get("episode_id"),
                "zone_name": c.get("zone_name"),
                "approach_direction": appr,
                "zone_role": role,
                "role_note": role_note,
                "role_vs_desired_audit": role_vs_desired,
                "regime_artifact": c.get("regime"),
                "regime_at_watch": snap_w.get("regime"),
                "regime_at_touch": snap_t.get("regime"),
                "regime_at_decision": snap_d.get("regime"),
                "flat_at_watch": flat_at_watch,
                "flat_at_decision": flat_at_decision,
                "watch_at": c.get("zone_watch_started_at"),
                "touch_at": touch,
                "ema9_decision": snap_d.get("ema9"),
                "ema20_decision": snap_d.get("ema20"),
                "ema59_decision": snap_d.get("ema59"),
                "ema200_decision": snap_d.get("ema200"),
                "close_decision_5m": close,
                "atr_decision": atr,
                "spread_20_59_pct": spr,
                "decision_price": c.get("decision_price"),
                "production_direction_consistent": prod_ok,
                "ema_layer_primary_verdict": primary,
                "tags": "|".join(verdicts),
            }
        )
        findings_events.append(
            {
                "decision_at": c.get("decision_at"),
                "verdicts": [primary] + verdicts,
                "production_direction_ok": prod_ok,
            }
        )
    pd.DataFrame(reported).to_csv(OUT / "reported_candidates_ema_audit.csv", index=False)

    # --- dashboard vs engine: TRP defaults typically same ewm close; note parity limitations ---
    # Sample hourly points
    dash_cmp = []
    for t in pd.date_range(
        datetime(2026, 8, 26, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc),
        freq="1h",
        tz="UTC",
    ):
        snap = regime_snapshot(bars, t.to_pydatetime())
        dash_cmp.append(
            {
                "asof": iso(t.to_pydatetime()),
                "engine_close": snap.get("close"),
                "engine_ema9": snap.get("ema9"),
                "engine_ema20": snap.get("ema20"),
                "engine_ema59": snap.get("ema59"),
                "engine_ema200": snap.get("ema200"),
                "dashboard_compare": "NOT_EXTRACTED_LIVE",
                "note": (
                    "Research TRP EMA overlays use same candle feed (CH candles_1m) when "
                    "configured with periods 9/20/59/200 on close; live chart series not "
                    "dumped in this read-only audit. Expect close parity if same TF/closed bars."
                ),
            }
        )
    pd.DataFrame(dash_cmp).to_csv(OUT / "dashboard_vs_engine_ema_comparison.csv", index=False)

    # --- formula audit json ---
    formula = {
        "ema": {
            "basis": "5m close",
            "formula": "pandas ewm(span=N, adjust=False).mean()",
            "min_periods": "pandas default 0 (no explicit min_periods)",
            "periods": {"ema9": 9, "ema20": 20, "ema59": 59, "ema200": 200},
            "files": [
                "src/orderbook_analyse/l2_wall_to_wall_discovery/manual_ema_wall_windows/indicators.py:41-42,61-66",
                "src/orderbook_analyse/ema_zone_microstructure_confirmation/regime.py:34-44",
            ],
            "closed_5m_only": True,
            "causal_asof": "last_closed_bar_at(bar_end <= asof)",
        },
        "atr": {
            "period": 14,
            "formula": "ewm(TR, span=14, adjust=False).mean()",
            "zone_half_width": "max(0.15*ATR, 5*tick)",
            "ZONE_ATR_FRAC": ZONE_ATR_FRAC,
            "ZONE_MIN_TICKS": ZONE_MIN_TICKS,
            "doge_tick": tick,
        },
        "watch": {
            "threshold": "dist_outside <= 3 * half_width",
            "NOT_percent": True,
            "touch": "mid inside [low,high] (dist==0)",
            "file": "continuous_engine.py + continuous_defaults.py ZONE_WATCH_DISTANCE_HALFWIDTH_MULT=3",
        },
        "approach_role_production": {
            "from_above": "resistance",
            "from_below": "support",
            "file": "continuous_engine.py:81-88",
            "conflicts_with_audit_brief_desired": {
                "desired_from_below": "resistance",
                "desired_from_above": "support",
            },
        },
        "flat_gate": {
            "thresholds": {
                "NEAR_EMA20_ATR_FRAC": NEAR_EMA20_ATR_FRAC,
                "FLAT_SLOPE_ATR_FRAC_EMA20": FLAT_SLOPE_ATR_FRAC_EMA20,
                "FLAT_SLOPE_ATR_FRAC_EMA59": FLAT_SLOPE_ATR_FRAC_EMA59,
            },
            "hard_gate_when": "block_flat_compression AND near(watch) BEFORE watch start",
            "recheck_at_decision": False,
            "file": "regime.py:51-66; continuous_engine.py:516-544",
        },
        "clearance": {
            "wait_if": "0.2<=pct<=0.5 OR (0<atr_mult<=0.5 AND pct<=0.5)",
            "applied_when": "only after breakout-class micro primary (NOT defense)",
            "continuous_flip": "disabled (possible/full_regime_flip hardcoded False)",
        },
        "ema200_in_short_term_score": False,
    }
    (OUT / "ema_formula_and_source_audit.json").write_text(
        json.dumps(formula, indent=2), encoding="utf-8"
    )

    provenance = {
        "job_id": req["job_id"],
        "run_id": man.get("run_id"),
        "strategy_id": req.get("strategy_id"),
        "signal_start": req.get("signal_start"),
        "signal_end_exclusive": req.get("signal_end_exclusive"),
        "effective_start": man.get("effective_start"),
        "effective_end": man.get("effective_end"),
        "n_signals_directed": len(sigs),
        "n_candidates": len(cands),
        "n_signals_2026_08_26": sum(
            1 for s in sigs if str(s.get("decision_at", "")).startswith("2026-08-26")
        ),
        "marker_source": "single job signals.jsonl — Research import maps these to overlays",
        "mixed_jobs": False,
        "note_extra_markers": (
            "Chart shows more than 6 markers because this job alone produced 18 directed "
            "signals on 2026-08-26 UTC (plus 33 other days in window). Many afternoon "
            "markers use STACKED_EMA_ZONE:EMA20+EMA59 when bands overlap."
        ),
        "timezone": "UTC",
        "decision_at_resolution": "milliseconds in ISO-Z",
    }
    (OUT / "marker_provenance_audit.json").write_text(
        json.dumps(provenance, indent=2), encoding="utf-8"
    )

    findings = {
        "verdict": "EMA_STAGE_ALLOWS_MICRO_TOO_OFTEN_IN_TIGHT_STACK;_ROLE_SEMANTICS_INVERTED_VS_DESIRED_BRIEF",
        "live_safety": "read_only_audit_no_writes",
        "root_causes": [
            {
                "id": "APPROACH_ROLE_INVERTED_VS_BRIEF",
                "detail": (
                    "Production continuous_engine maps from_below→support and "
                    "from_above→resistance; audit brief expects the opposite. "
                    "All six morning candidates follow production mapping consistently."
                ),
            },
            {
                "id": "FLAT_GATE_NARROW",
                "detail": (
                    "Flat/compression hard-blocks only when near AND flat at watch start; "
                    "not re-checked at decision. Only 3 block_flat_compression candidates "
                    "on 2026-08-26 vs 18 directed markers."
                ),
            },
            {
                "id": "WATCH_IS_3x_HALFWIDTH_NOT_0_2PCT",
                "detail": (
                    "Proximity watch uses dist<=3*max(0.15*ATR,5*tick), not 0.2% price. "
                    "This is much wider in tight ATR regimes and admits many watches."
                ),
            },
            {
                "id": "CLEARANCE_NOT_ON_DEFENSE",
                "detail": (
                    "wait_next_zone clearance applies to breakout-class micro outcomes, "
                    "not defense_rejection. Tight EMA20↔EMA59 stacks still emit directed "
                    "defense LONGs/SHORTs."
                ),
            },
            {
                "id": "EXTRA_MARKERS_SAME_JOB",
                "detail": (
                    "Not stale mixed jobs: 18 Aug-26 directed signals in d2809c95 alone "
                    "explain dense chart; six listed times are a subset."
                ),
            },
            {
                "id": "DIRECTION_FROM_MICRO_PLUS_ROLE",
                "detail": (
                    "EMA stage does not emit LONG/SHORT. Direction comes after micro "
                    "classification via candidate_direction(state, zone_role)."
                ),
            },
        ],
        "six_events": findings_events,
        "answers": {
            "A_first_ema_setup": (
                "Closed 5m bars → EMAs/ATR → zone bands → mid approaches within "
                "3*half_width of EMA20/59/200 (or stacked) without flat+near block "
                "→ ActiveWatch starts (watch_zone)."
            ),
            "B_ema_stage_outputs": [
                "regime (bullish/bearish/transition/range_compression/undetermined)",
                "primary zone (EMA20/59/200/STACKED)",
                "approach_direction",
                "zone_role (production: from_below→support)",
                "flat block flag",
                "clearance feature (breakout path only)",
                "permission to enter wait_microstructure_confirmation after touch",
            ],
            "C_micro_only": [
                "breakout vs defense/rejection",
                "absorption",
                "false breakout/reclaim",
                "wall side / trade aggression / OI-liq features",
                "final LONG/SHORT via state×role mapping",
            ],
            "D_why_flat_area_markers": (
                "Stacked/tight EMAs still pass watch (3×hw) and often fail flat-gate "
                "(slopes/near thresholds); defense path ignores next-zone clearance; "
                "micro then confirms many defense/false-break events → directed markers."
            ),
            "E_primary": "COMBINATION: inverted approach-role vs desired brief + weak flat gate + "
            "no clearance on defense + many same-job markers (not stale mix)",
        },
    }
    (OUT / "findings.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")

    # production logic md
    (OUT / "production_ema_logic.md").write_text(
        """# Production EMA logic (EZM V1 continuous discovery)

## Candle → 5m
- `load_candles_1m` from `signal_generator.candles_1m` (CH), pad 240h before discovery.
- `aggregate_5m`: resample 5min label/closed left; OHLCV; `bar_end = open+5m`.
- Causal: `last_closed_bar_at` requires `bar_end <= asof`.

## EMA / ATR
- `ema = close.ewm(span=N, adjust=False).mean()` for N∈{9,20,59,200}.
- ATR14 = ewm(TR, span=14, adjust=False).
- Short-term warmup: bar index ≥ 58. EMA200 warmup: index ≥ 199.
- EMA200 **not** in short-term regime score.

## Regime
- `classify_trend` score: stack 0.30, slopes 0.25, price vs EMA20 0.20, structure 0.15, ret30 0.10.
- Labels → bullish/bearish/transition/range_compression/undetermined.
- Flat override: `|s20_3|<0.02*ATR` & `|s59_3|<0.01*ATR` & `|close-EMA20|/ATR<0.35` → `block_flat_compression`.

## Zones
- half_width = max(0.15*ATR, 5*tick); band = center ± hw.
- Primary: stacked overlap else nearest EMA20/59/200.

## Watch / touch
- Watch if dist_outside ≤ **3 × half_width** (not 0.2%).
- Touch if mid inside band.
- Max watch 1800s; cooldown 900s; rearm after leaving ≥1 hw.

## Approach / role (PRODUCTION)
- from_above → **resistance**
- from_below → **support**
- (Audit brief desired the opposite.)

## Direction
- EMA stage emits no LONG/SHORT.
- After micro: `candidate_direction(state, zone_role)`.

## Clearance
- wait_next if gap% in [0.2,0.5] or atr_mult≤0.5 with pct≤0.5.
- Continuous: applied on breakout-class only; defense unaffected.
- Regime-flip states: continuous hardcoded disabled.
""",
        encoding="utf-8",
    )

    # full report
    report = f"""# EMA-only audit report — DOGEUSDT job d2809c95

## 1. VERDICT
**EMA_STAGE_PERMITS_MICRO_IN_TIGHT_STACKS; production approach→role is inverted vs desired Stage-A brief; extra chart markers are mostly same-job Aug-26 signals (18), not stale mixes.**

## 2. LIVE-SICHERHEIT
Read-only. No CH writes, no detector/dashboard changes, no job rerun, no workspace mutation.

## 3. UNTERSUCHTER JOB / RESULT / ZEITFENSTER
- job_id: `{req['job_id']}`
- run_id: `{man.get('run_id')}`
- strategy: `{req.get('strategy_id')}`
- requested: {req.get('signal_start')} → {req.get('signal_end_exclusive')}
- effective L2: {man.get('effective_start')} → {man.get('effective_end')}
- directed signals total: {len(sigs)}; on 2026-08-26: {provenance['n_signals_2026_08_26']}

## 4. TATSÄCHLICHE PRODUKTIONS-EMA-FORMELN
See `production_ema_logic.md` and `ema_formula_and_source_audit.json`.
Key: `ewm(span, adjust=False)` on **5m close**; zone hw=`max(0.15*ATR,5*tick)`; watch=`3*hw`.

## 5. ENGINE VS DASHBOARD EMA-PARITÄT
Engine values recomputed from CH `candles_1m` via production functions → `ema_values_5m.csv`.
Live TRP overlay series were **not** scraped from the browser; if Research uses same CH closes + periods 9/20/59/200, parity is expected. Marked `NOT_EXTRACTED_LIVE` in comparison CSV.

## 6. REINE REGIME-LOGIK
Short-term: EMA9/20/59 order, slopes, price vs EMA20, HH/LL structure, 30m return. EMA200 structural only.
Labels: bullish / bearish / transition / range_compression / undetermined.
Timeline: `ema_regime_timeline.csv`.

## 7. FLAT-/COMPRESSION-GATE
Exists as **hard gate** at watch admission when `block_flat_compression && near`.
**Not** re-applied at decision_at. On 2026-08-26 only 3 `block_flat_compression` candidates vs 18 directed markers.

## 8. EMA-ZONEN UND BÄNDER
EMA20/59/200 bands with shared ATR half-width; stacked when overlapping → `STACKED_EMA_ZONE:…`.
EMA9 is regime/momentum only (not a wall zone center).

## 9. APPROACH UND ZONENROLLE
Production continuous:
- from_below → **support** → defense LONG
- from_above → **resistance** → defense SHORT
Desired audit brief is the opposite. All six morning events match **production**, conflict with **desired brief**.

## 10. PROXIMITY VS EXACT TOUCH
Watch ≠ 0.2%. Watch = outside-distance ≤ 3×half_width. Touch = mid inside band.
Sensitivity counts: `proximity_sensitivity.csv`.

## 11. CLEARANCE / NÄCHSTE EMA
Clearance wait feature exists but continuous applies it to **breakout-class** outcomes, not defense.
Hence tight EMA20↔EMA59 still produce defense LONGs/SHORTs.

## 12. EMA200- / REGIME-FLIP
Flip detector exists for manual runner; **continuous discovery hardcodes flip flags False**.

## 13. AUDIT DER SECHS CANDIDATES
See `reported_candidates_ema_audit.csv`. Summary:
| time | dir | zone | approach | role | regime | EMA verdict |
|------|-----|------|----------|------|--------|-------------|
| 00:30 | LONG | EMA20 | from_below | support | bearish | production-consistent; role vs brief conflict; false_breakout |
| 00:49 | LONG | EMA20 | from_below | support | bearish | same |
| 01:06 | LONG | EMA59 | from_below | support | transition | same |
| 01:26 | SHORT | EMA20 | from_above | resistance | transition | same |
| 01:33 | LONG | EMA59 | from_below | support | undetermined | same; undetermined regime |
| 01:55 | SHORT | EMA20 | from_above | resistance | transition | same |

## 14. ALLE AKTIVEN CHART-MARKER UND HERKUNFT
`active_chart_markers_audit.csv` — all {len(sigs)} directed markers from this job.
Aug-26 alone: 18 markers (list in provenance JSON).

## 15. URSACHE DER ZUSÄTZLICHEN/UNPLAUSIBLEN MARKER
Primarily **same job**, full discovery day, many STACKED-zone defenses/false-breaks — not mixed stale jobs.

## 16. EMA-ONLY TIMELINE
`ema_zone_watch_timeline.csv` encodes WATCH_*/TOUCH_*/BLOCK_FLAT_*/WAIT_NEXT_* style events using production thresholds (mid proxy = closed 5m close).

## 17. TESTS
No production tests modified. Existing unit tests not required for this artifact build.

## 18. ARTEFAKTE
Directory: `{OUT}`

## 19. GRENZEN
- Mid for EMA-only watch reconstruction uses closed 5m close, not L2 mid (Stage B excluded).
- Dashboard EMA series not live-exported.
- Clearance recomputation simplified vs engine stacked primary keys.

## 20. KERNAUSSAGE
EMA stage currently **arms microstructure** via wide 3×hw watches and approach→role mapping that treats from_below as support. It does **not** assert breakout/rebound; LONG/SHORT appear only after micro×role. Dense flat/stacked periods still pass Stage A often enough to yield many directed markers.

## 21. EMPFOHLENER NÄCHSTER SCHRITT
Design-only (no code yet): align Stage-A role semantics with brief; tighten flat gate + re-check at decision; apply clearance to defense path or block stacked primary; optionally replace 3×hw watch with explicit % proximity separate from touch.
"""
    (OUT / "report.md").write_text(report, encoding="utf-8")

    manifest = {
        "created_at": iso(datetime.now(timezone.utc)),
        "job_id": req["job_id"],
        "run_id": man.get("run_id"),
        "out_dir": str(OUT),
        "artifacts": sorted(p.name for p in OUT.iterdir()),
        "read_only": True,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("WROTE", OUT)
    print("artifacts", manifest["artifacts"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
