#!/usr/bin/env python3
"""Phase-B audit — RAM-safe stepped runner.

Steps (run one at a time):
  0  precompute structure bars → cache
  1  baseline replay + focus timelines + core CSVs
  2  width + merge sweeps (one variant, then GC)
  3  activation + rejection + break + contact-window sweeps
  4  decision + README from summaries

Example:
  PYTHONPATH=. python3 -u research/regime_scanner/trend_zones_phase_b_audit.py --step 0
  PYTHONPATH=. python3 -u research/regime_scanner/trend_zones_phase_b_audit.py --step 1
  ...
  PYTHONPATH=. python3 -u research/regime_scanner/trend_zones_phase_b_audit.py --step all

No production policy changes. Does not modify structure/machine/policy.
"""
from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import pickle
import resource
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.indicators import compute_indicator_frame
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import ConfirmedPivot, find_confirmed_pivots
from research.regime_scanner.timeframes import aggregate_candles, timeframe_timedelta
from research.regime_scanner.trend_structure import (
    MarketStructureState,
    StructureEvent,
    default_trend_structure_config,
    update_market_structure,
)
from research.regime_scanner.trend_zones import (
    TrendZoneTracker,
    ZoneConfig,
    activation_variant,
    break_variant,
    contact_window_variant,
    merge_variant,
    rejection_variant,
    width_variant,
)

OUT = Path("research/regime_scanner/results/trend_zones_phase_b")
CACHE = OUT / "_cache"
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")

REPLAY_START = "2025-12-20T00:00:00+00:00"
REPLAY_END = "2026-03-28T00:00:00+00:00"
FOCUS_A = (1.02, 1.03)
FOCUS_B = (0.815, 0.82)


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _p(msg: str) -> None:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"{msg}  [rss_peak≈{rss:.0f}MB]", flush=True)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            flat = {}
            for k, v in r.items():
                if isinstance(v, (list, tuple)):
                    flat[k] = "|".join(str(x) for x in v)
                elif isinstance(v, dict):
                    flat[k] = json.dumps(v, sort_keys=True)
                else:
                    flat[k] = v
            w.writerow(flat)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _append_summary_row(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flat = {k: v for k, v in row.items() if k != "config"}
    flat["config_json"] = json.dumps(row.get("config", {}), sort_keys=True)
    write_header = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(flat.keys()), extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(flat)


def lean_cfg(cfg: ZoneConfig) -> ZoneConfig:
    return ZoneConfig(
        **{
            **cfg.to_dict(),
            "max_zones": 2048,
            "log_pressure_every_bar": False,
            "log_approach_every_bar": False,
            "log_lifecycle": False,  # outcomes/anchors/breaks still logged
        }
    )


def baseline() -> ZoneConfig:
    cfg = width_variant("W2")
    cfg = merge_variant("M1", cfg)
    cfg = activation_variant("A1", cfg)
    cfg = rejection_variant("R1", cfg)
    cfg = break_variant("B4", cfg)
    cfg = contact_window_variant("C2", cfg)
    return lean_cfg(ZoneConfig(**{**cfg.to_dict(), "approach_atr": 0.50}))


def load_5m(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    warm = start - pd.Timedelta(days=3)
    return raw[(raw["timestamp"] >= warm) & (raw["timestamp"] < end)].reset_index(drop=True)


def build_30m(frame_5m: pd.DataFrame, end: pd.Timestamp) -> tuple[pd.DataFrame, list[ConfirmedPivot]]:
    scfg = default_regime_scanner_config().with_timeframe("30m")
    agg = aggregate_candles(frame_5m, "30m", end)
    ind = compute_indicator_frame(agg, config=scfg).copy()
    ind["timestamp"] = pd.to_datetime(ind["timestamp"], utc=True)
    ind["close_time"] = ind["timestamp"] + timeframe_timedelta("30m")
    if "ema_9_slope_3_pct" in ind.columns:
        ind["ema9_slope"] = ind["ema_9_slope_3_pct"]
    else:
        ind["ema9_slope"] = pd.NA
    if "ema_20_slope_3_pct" in ind.columns:
        ind["ema20_slope"] = ind["ema_20_slope_3_pct"]
    else:
        ind["ema20_slope"] = pd.NA
    ind = ind.loc[ind["close_time"] <= end].reset_index(drop=True)
    pivots = find_confirmed_pivots(ind, config=scfg)
    return ind, pivots


def precompute(
    ind: pd.DataFrame, pivots: list[ConfirmedPivot]
) -> list[tuple[dict[str, Any], list[StructureEvent], MarketStructureState, float | None, bool]]:
    """Store candle as plain dict to keep pickle smaller/safer."""
    cfg = default_trend_structure_config()
    state = MarketStructureState(timeframe="30m")
    zone_start = _ts(REPLAY_START)
    out = []
    for i in range(len(ind)):
        row = ind.iloc[i]
        close_time = _ts(row["close_time"])
        atr = float(row["atr"]) if "atr" in ind.columns and pd.notna(row.get("atr")) else None
        state, evs = update_market_structure(
            state, candle=row, pivots=pivots, decision_time=close_time, atr=atr, cfg=cfg
        )
        snap = MarketStructureState(
            timeframe="30m",
            last_confirmed_swing_high=state.last_confirmed_swing_high,
            last_confirmed_swing_low=state.last_confirmed_swing_low,
            last_updated_at=state.last_updated_at,
        )
        in_window = bool(_ts(row["timestamp"]) >= zone_start and close_time <= _ts(REPLAY_END))
        candle = {
            "timestamp": row["timestamp"],
            "close_time": row["close_time"],
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row["volume"]) if "volume" in row.index and pd.notna(row.get("volume")) else 0.0,
            "ema9_slope": float(row["ema9_slope"]) if pd.notna(row.get("ema9_slope")) else None,
            "ema20_slope": float(row["ema20_slope"]) if pd.notna(row.get("ema20_slope")) else None,
        }
        out.append((candle, list(evs), snap, atr, in_window))
    return out


def replay(bars, cfg: ZoneConfig) -> TrendZoneTracker:
    tr = TrendZoneTracker(lean_cfg(cfg))
    for candle, evs, ms, atr, in_window in bars:
        if not in_window:
            continue
        tr.update(candle, evs, ms, atr)
    return tr


def summarize(tr: TrendZoneTracker, vid: str) -> dict[str, Any]:
    zones = tr.zones
    n = len(zones)
    mega = sum(1 for z in zones if z.birth_width_abs > 0 and z.width_abs > z.birth_width_abs * 1.5)
    max_exp_ratio = (
        max(((z.width_abs / z.birth_width_abs) if z.birth_width_abs > 0 else 1.0) for z in zones)
        if zones
        else 1.0
    )
    from collections import Counter

    oc = Counter(e.outcome for e in tr.contact_episodes if e.outcome)
    return {
        "variant_id": vid,
        "n_zones": n,
        "n_active": sum(1 for z in zones if z.state == "active"),
        "n_forming": sum(1 for z in zones if z.state == "forming"),
        "n_broken": sum(1 for z in zones if z.state == "broken"),
        "n_mega_gt_1_5x_birth": mega,
        "max_width_over_birth": max_exp_ratio,
        "max_cumulative_expansion": max((z.cumulative_expansion for z in zones), default=0.0),
        "n_merges": len(tr.merge_log),
        "n_merge_rejects": len(tr.merge_reject_log),
        "separate_zone_created_count": tr.separate_zone_created_count,
        "n_contact_episodes": len(tr.contact_episodes),
        "n_rejection": oc.get("REJECTION_CONFIRMED", 0),
        "n_breakout": oc.get("BREAKOUT_CONFIRMED", 0),
        "n_false_break": oc.get("FALSE_BREAKOUT", 0),
        "n_ambiguous": oc.get("AMBIGUOUS", 0),
        "n_still_inside": oc.get("STILL_INSIDE_ZONE", 0),
        "n_expired": oc.get("EXPIRED_WITHOUT_REACTION", 0),
        "config_key": tr.cfg.variant_key,
        "config": tr.cfg.to_dict(),
    }


def score(s: dict[str, Any]) -> float:
    sc = 0.0
    n = float(s["n_zones"])
    if n < 200:
        sc += 50
    elif n < 350:
        sc += 20
    else:
        sc -= (n - 350) * 0.1
    sc -= 20 * float(s["n_mega_gt_1_5x_birth"])
    sc -= 30 * max(0.0, float(s["max_width_over_birth"]) - 1.35)
    sc += 0.3 * float(s["n_rejection"])
    sc += 0.4 * float(s["n_breakout"])
    sc += 0.2 * float(s["n_false_break"])
    sc -= 0.15 * float(s["n_ambiguous"])
    return sc


def zones_in_band(tr: TrendZoneTracker, lo: float, hi: float):
    out = []
    for z in tr.zones:
        if not (z.upper_bound < lo or z.lower_bound > hi):
            out.append(z)
        elif any(lo <= float(p) <= hi for p in z.source_prices):
            out.append(z)
        elif lo <= z.center_price <= hi:
            out.append(z)
    return out


def reaction_timeline(tr: TrendZoneTracker, lo: float, hi: float) -> list[dict[str, Any]]:
    ids = {z.zone_id for z in zones_in_band(tr, lo, hi)}
    rows: list[dict[str, Any]] = []
    for a in tr.anchor_log:
        if a.get("zone_id") in ids:
            rows.append({**a, "event_kind": a.get("action", "anchor")})
    for m in tr.merge_log:
        if m.get("zone_id") in ids:
            rows.append({**m, "event_kind": "merge_expand"})
    for r in tr.merge_reject_log:
        if r.get("zone_id") in ids:
            rows.append({**r, "event_kind": "merge_reject"})
    for t in tr.touch_log:
        if t.get("zone_id") in ids:
            rows.append({**t, "event_kind": "contact_episode"})
    for o in tr.outcome_log:
        if o.get("zone_id") in ids:
            rows.append({**o, "event_kind": f"outcome:{o.get('outcome')}"})
    for b in tr.break_log:
        if b.get("zone_id") in ids:
            rows.append({**b, "event_kind": "zone_broken"})
    for f in tr.false_break_log:
        if f.get("zone_id") in ids:
            rows.append({**f, "event_kind": "false_break"})
    for f in tr.flip_log:
        if f.get("zone_id") in ids:
            rows.append({**f, "event_kind": f.get("event_kind", "flip")})
    for z in tr.zones:
        if z.zone_id in ids:
            rows.append(
                {
                    "event_kind": "final_status",
                    "event_available_timestamp": _iso(REPLAY_END),
                    "zone_id": z.zone_id,
                    "role": z.role,
                    "state": z.state,
                    "lower_bound": z.lower_bound,
                    "upper_bound": z.upper_bound,
                    "birth_lower": z.birth_lower,
                    "birth_upper": z.birth_upper,
                    "cumulative_expansion": z.cumulative_expansion,
                    "touch_episode_count": z.touch_episode_count,
                    "flip_candidate": z.flip_candidate,
                    "retest_contact_at": _iso(z.retest_contact_at),
                }
            )
    rows.sort(key=lambda r: str(r.get("event_available_timestamp") or ""))
    return rows


def load_bars():
    path = CACHE / "bars.pkl"
    if not path.exists():
        raise SystemExit("Missing cache — run --step 0 first")
    with path.open("rb") as fh:
        return pickle.load(fh)


def step0() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    hashes = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
    }
    _write_json(OUT / "hashes_before.json", hashes)
    _p(f"hashes={hashes}")
    start, end = _ts(REPLAY_START), _ts(REPLAY_END)
    frame = load_5m(start, end)
    _p(f"5m n={len(frame)}")
    ind, pivots = build_30m(frame, end)
    _p(f"30m n={len(ind)} pivots={len(pivots)}")
    bars = precompute(ind, pivots)
    with (CACHE / "bars.pkl").open("wb") as fh:
        pickle.dump(bars, fh, protocol=pickle.HIGHEST_PROTOCOL)
    # keep last-bar EMA for pressure join
    last = bars[-1][0] if bars else {}
    _write_json(CACHE / "meta.json", {"n_bars": len(bars), "last_ema": last, "hashes": hashes})
    # clear summary for fresh run
    for name in ("variant_grid_summary.csv", "rejection_variant_comparison.csv", "breakout_variant_comparison.csv"):
        p = OUT / name
        if p.exists():
            p.unlink()
    _p("step0 done — structure cached")


def step1() -> None:
    bars = load_bars()
    base = baseline()
    _p("baseline replay…")
    tr = replay(bars, base)
    s = summarize(tr, "BASE_W2_M1_A1_R1_B4_C2")
    s["score"] = score(s)
    _p(
        f"baseline zones={s['n_zones']} mega={s['n_mega_gt_1_5x_birth']} "
        f"max_ratio={s['max_width_over_birth']:.3f} episodes={s['n_contact_episodes']} "
        f"rej={s['n_rejection']} brk={s['n_breakout']} false={s['n_false_break']}"
    )
    _write_csv(OUT / "denoised_zone_summary.csv", [z.to_dict() for z in tr.zones])
    _write_csv(OUT / "zone_birth_audit.csv", list(tr.anchor_log))
    _write_csv(OUT / "zone_merge_rejections.csv", list(tr.merge_reject_log) + list(tr.merge_log))
    _write_csv(OUT / "contact_episodes.csv", [e.to_dict() for e in tr.contact_episodes])
    _write_csv(OUT / "contact_outcomes.csv", list(tr.outcome_log))
    _write_csv(OUT / "false_breakout_audit.csv", list(tr.false_break_log))
    pressure = tr.pressure_snapshots(REPLAY_END)
    meta = json.loads((CACHE / "meta.json").read_text())
    last_ema = meta.get("last_ema") or {}
    for row in pressure:
        row["ema9_slope"] = last_ema.get("ema9_slope")
        row["ema20_slope"] = last_ema.get("ema20_slope")
    _write_csv(OUT / "zone_pressure_audit.csv", pressure)
    _write_csv(OUT / "aptusdt_102_103_reaction_timeline.csv", reaction_timeline(tr, *FOCUS_A))
    _write_csv(OUT / "aptusdt_0815_0820_reaction_timeline.csv", reaction_timeline(tr, *FOCUS_B))
    focus_a = [
        {
            "zone_id": z.zone_id,
            "role": z.role,
            "state": z.state,
            "birth_bounds": [z.birth_lower, z.birth_upper],
            "current_bounds": [z.lower_bound, z.upper_bound],
            "cumulative_expansion": z.cumulative_expansion,
            "episodes": z.touch_episode_count,
            "broken_at": _iso(z.broken_at),
            "flip_candidate": z.flip_candidate,
            "retest_contact_at": _iso(z.retest_contact_at),
        }
        for z in zones_in_band(tr, *FOCUS_A)
    ]
    focus_b = [
        {
            "zone_id": z.zone_id,
            "role": z.role,
            "state": z.state,
            "birth_bounds": [z.birth_lower, z.birth_upper],
            "current_bounds": [z.lower_bound, z.upper_bound],
            "cumulative_expansion": z.cumulative_expansion,
            "episodes": z.touch_episode_count,
            "broken_at": _iso(z.broken_at),
            "flip_candidate": z.flip_candidate,
            "retest_contact_at": _iso(z.retest_contact_at),
        }
        for z in zones_in_band(tr, *FOCUS_B)
    ]
    _write_json(CACHE / "baseline_summary.json", {k: v for k, v in s.items() if k != "config"} | {"config": s["config"]})
    _write_json(CACHE / "focus_a.json", focus_a)
    _write_json(CACHE / "focus_b.json", focus_b)
    _append_summary_row(OUT / "variant_grid_summary.csv", s)
    det_tr = replay(bars, base)
    det = [z.to_dict() for z in tr.zones] == [z.to_dict() for z in det_tr.zones]
    _write_json(CACHE / "determinism.json", {"ok": det})
    _p(f"step1 done focus_a={len(focus_a)} focus_b={len(focus_b)} det={det}")
    del tr, det_tr, bars
    gc.collect()


def _run_variants(variants: list[tuple[str, ZoneConfig]], family_csv: Path | None = None) -> None:
    bars = load_bars()
    for vid, cfg in variants:
        _p(f"replay {vid}")
        tr = replay(bars, cfg)
        s = summarize(tr, vid)
        s["score"] = score(s)
        _append_summary_row(OUT / "variant_grid_summary.csv", s)
        if family_csv is not None:
            _append_summary_row(family_csv, s)
        _p(
            f"  → zones={s['n_zones']} mega={s['n_mega_gt_1_5x_birth']} "
            f"rej={s['n_rejection']} brk={s['n_breakout']} score={s['score']:.1f}"
        )
        del tr
        gc.collect()
    del bars
    gc.collect()


def step2() -> None:
    variants: list[tuple[str, ZoneConfig]] = []
    for w in ("W0", "W1", "W2", "W3"):
        c = merge_variant("M1", width_variant(w))
        c = activation_variant("A1", c)
        c = rejection_variant("R1", c)
        c = break_variant("B4", c)
        variants.append((f"W_{w}", lean_cfg(c)))
    for m in ("M0", "M1", "M2", "M3"):
        c = merge_variant(m, width_variant("W2"))
        c = activation_variant("A1", c)
        c = rejection_variant("R1", c)
        c = break_variant("B4", c)
        variants.append((f"M_{m}", lean_cfg(c)))
    _run_variants(variants)
    _p("step2 done")


def step3() -> None:
    rej: list[tuple[str, ZoneConfig]] = []
    for r in ("R0", "R1", "R2", "R3", "R4_2", "R4_3", "R4_4"):
        c = rejection_variant(r, activation_variant("A1", merge_variant("M1", width_variant("W2"))))
        c = break_variant("B4", c)
        rej.append((f"R_{r}", lean_cfg(c)))
    _run_variants(rej, OUT / "rejection_variant_comparison.csv")

    brk: list[tuple[str, ZoneConfig]] = []
    for b in ("B0", "B1", "B2", "B3", "B4"):
        c = break_variant(
            b, rejection_variant("R1", activation_variant("A1", merge_variant("M1", width_variant("W2"))))
        )
        brk.append((f"B_{b}", lean_cfg(c)))
    _run_variants(brk, OUT / "breakout_variant_comparison.csv")

    act: list[tuple[str, ZoneConfig]] = []
    for a in ("A0", "A1", "A2"):
        c = activation_variant(a, merge_variant("M1", width_variant("W2")))
        c = rejection_variant("R1", c)
        c = break_variant("B4", c)
        act.append((f"A_{a}", lean_cfg(c)))
    for cw in ("C1", "C2", "C3"):
        c = contact_window_variant(
            cw,
            break_variant("B4", rejection_variant("R1", activation_variant("A1", merge_variant("M1", width_variant("W2"))))),
        )
        act.append((f"C_{cw}", lean_cfg(c)))
    _run_variants(act)
    _p("step3 done")


def step4() -> None:
    summary_path = OUT / "variant_grid_summary.csv"
    if not summary_path.exists():
        raise SystemExit("Missing variant_grid_summary.csv — run steps 1–3 first")
    rows = list(csv.DictReader(summary_path.open()))
    base = json.loads((CACHE / "baseline_summary.json").read_text())
    focus_a = json.loads((CACHE / "focus_a.json").read_text())
    focus_b = json.loads((CACHE / "focus_b.json").read_text())
    det = json.loads((CACHE / "determinism.json").read_text()).get("ok", False)
    hashes_before = json.loads((OUT / "hashes_before.json").read_text())
    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
    }
    assert hashes_before == hashes_after

    def best(prefix: str) -> dict | None:
        fam = [r for r in rows if r["variant_id"].startswith(prefix)]
        if not fam:
            return None
        return max(fam, key=lambda x: float(x["score"]))

    bests = {
        "width": best("W_"),
        "merge": best("M_"),
        "activation": best("A_"),
        "rejection": best("R_"),
        "break": best("B_"),
        "contact_window": best("C_"),
        "baseline": base,
    }

    n_zones = int(base["n_zones"])
    mega = int(base["n_mega_gt_1_5x_birth"])
    max_ratio = float(base["max_width_over_birth"])
    a_stable = (
        all(
            float(z["cumulative_expansion"])
            <= (float(z["birth_bounds"][1]) - float(z["birth_bounds"][0])) * 0.5 + 1e-9
            for z in focus_a
        )
        if focus_a
        else False
    )
    b_found = len(focus_b) > 0

    if n_zones >= 567 * 0.85 or mega > 5 or max_ratio > 2.0:
        decision, note = "L", "Zonen weiterhin zu laut oder Mega-Zonen vorhanden."
    elif not b_found or not focus_a:
        decision, note = "M", "Structure-Anker reichen für Pflichtfälle nicht aus."
    elif n_zones < 250 and mega == 0 and max_ratio <= 1.35 and a_stable and b_found and int(base["n_breakout"]) >= 1 and int(base["n_rejection"]) >= 5:
        decision, note = "J", "Stabile Zonen und klare Rejection-/Breakout-Klassifikation unter Baseline erreicht."
    else:
        decision, note = (
            "K",
            "Zonen entrauscht und Outcomes getrennt; Rejection-/Breakout-Schwellen noch Ein-Achsen-Heuristik.",
        )

    outcome_priority = (
        "FALSE_BREAKOUT > BREAKOUT_CONFIRMED > REJECTION_CONFIRMED > "
        "STILL_INSIDE_ZONE > EXPIRED_WITHOUT_REACTION > AMBIGUOUS"
    )
    readme = f"""# Trend Zones Phase B — Denoise + Contact Outcomes

**Decision: {decision}** — {note}

## How this audit was run (RAM-safe)

Stepped runner (`--step 0..4`). Per-bar pressure/approach logs disabled.
One variant at a time + `gc.collect()`.

## Protected hashes

| File | MD5 |
|---|---|
| trend_structure.py | `{hashes_after['trend_structure.py']}` |
| trend_state_machine.py | `{hashes_after['trend_state_machine.py']}` |
| trend_state_policy.py | `{hashes_after['trend_state_policy.py']}` |

## Baseline

`W2 + M1 + A1 + R1 + B4 + C2` · determinism={det}

- zones: **{n_zones}** (Phase A: 567)
- mega (>1.5× birth): **{mega}**
- max width/birth: **{max_ratio:.3f}**
- episodes: {base['n_contact_episodes']}
- outcomes: rej={base['n_rejection']} break={base['n_breakout']} false={base['n_false_break']} amb={base['n_ambiguous']}

## Outcome priority

{outcome_priority}

## Focus A 1.02–1.03

```json
{json.dumps(json_safe(focus_a), indent=2)}
```

## Focus B 0.815–0.82

```json
{json.dumps(json_safe(focus_b), indent=2)}
```

## Variant preferences

| Family | Best |
|---|---|
| Width | {(bests['width'] or {}).get('variant_id', 'n/a')} |
| Merge | {(bests['merge'] or {}).get('variant_id', 'n/a')} |
| Activation | {(bests['activation'] or {}).get('variant_id', 'n/a')} |
| Rejection | {(bests['rejection'] or {}).get('variant_id', 'n/a')} |
| Break | {(bests['break'] or {}).get('variant_id', 'n/a')} |
| Contact window | {(bests['contact_window'] or {}).get('variant_id', 'n/a')} |

## Success checklist

1. zones << 567: {'PASS' if n_zones < 567 * 0.85 else 'FAIL'} ({n_zones})
2. no mega zones: {'PASS' if mega == 0 else 'FAIL'}
3. 1.02–1.03 stable: {'PASS' if a_stable and focus_a else 'FAIL'}
4. 0.815–0.82 found: {'PASS' if b_found else 'FAIL'}
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    _write_json(
        OUT / "decision.json",
        {
            "decision": decision,
            "note": note,
            "bests": bests,
            "focus_a": focus_a,
            "focus_b": focus_b,
            "hashes": hashes_after,
            "determinism": det,
            "outcome_priority": outcome_priority,
            "baseline_metrics": base,
        },
    )
    _p(f"DONE decision={decision} n_zones={n_zones}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--step",
        required=True,
        help="0|1|2|3|4|all",
    )
    args = ap.parse_args()
    step = str(args.step).lower()
    order = ["0", "1", "2", "3", "4"]
    if step == "all":
        for s in order:
            _p(f"===== STEP {s} =====")
            globals()[f"step{s}"]()
            gc.collect()
        return
    if step not in order:
        raise SystemExit(f"unknown step {step}")
    globals()[f"step{step}"]()


if __name__ == "__main__":
    main()
