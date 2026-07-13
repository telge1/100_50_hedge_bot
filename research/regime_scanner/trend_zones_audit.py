#!/usr/bin/env python3
"""Phase-A diagnostic audit: causal 30m S/R zones (no production policy changes).

Replays APTUSDT 30m structure events into TrendZoneTracker across width / merge /
episode / activation / rejection / break variants. Does not modify
trend_structure / trend_state_machine / trend_state_policy.
"""
from __future__ import annotations

import csv
import hashlib
import json
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
    episode_variant,
    merge_variant,
    rejection_variant,
    width_variant,
)

OUT = Path("research/regime_scanner/results/trend_zones_audit")
MACHINE = Path("research/regime_scanner/trend_state_machine.py")
STRUCTURE = Path("research/regime_scanner/trend_structure.py")
POLICY = Path("research/regime_scanner/trend_state_policy.py")

# Requested window; candles may start later — documented in README.
REPLAY_START = "2025-12-20T00:00:00+00:00"
REPLAY_END = "2026-03-28T00:00:00+00:00"
FOCUS_START = "2026-02-25T00:00:00+00:00"
FOCUS_END = "2026-03-27T23:59:59+00:00"
FOCUS_LO = 1.02
FOCUS_HI = 1.03


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
    print(msg, flush=True)


def _write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = fields or list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _flat(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (list, tuple)):
            out[k] = "|".join(str(x) for x in v)
        elif isinstance(v, dict):
            out[k] = json.dumps(v, sort_keys=True)
        else:
            out[k] = v
    return out


def load_5m(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    raw = load_symbol_candles("APTUSDT")
    raw = raw.copy()
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True)
    # Warmup: load a few days before start when available
    warm = start - pd.Timedelta(days=3)
    slice_ = raw[(raw["timestamp"] >= warm) & (raw["timestamp"] < end)].copy()
    return slice_.reset_index(drop=True)


def build_30m(frame_5m: pd.DataFrame, end: pd.Timestamp) -> tuple[pd.DataFrame, list[ConfirmedPivot]]:
    scfg = default_regime_scanner_config().with_timeframe("30m")
    agg = aggregate_candles(frame_5m, "30m", end)
    if agg.empty:
        raise RuntimeError("empty 30m aggregate")
    ind = compute_indicator_frame(agg, config=scfg)
    ind = ind.copy()
    ind["timestamp"] = pd.to_datetime(ind["timestamp"], utc=True)
    ind["close_time"] = ind["timestamp"] + timeframe_timedelta("30m")
    # Fully closed buckets only (close_time <= end)
    ind = ind.loc[ind["close_time"] <= end].reset_index(drop=True)
    pivots = find_confirmed_pivots(ind, config=scfg)
    return ind, pivots


def precompute_structure(
    ind: pd.DataFrame, pivots: list[ConfirmedPivot]
) -> list[tuple[pd.Series, list[StructureEvent], MarketStructureState, float | None, bool]]:
    """Causal walk over all closed 30m bars.

    Returns rows with ``in_zone_window`` True when the bar open is in the
    requested audit window (warmup bars still advance structure).
    """
    cfg = default_trend_structure_config()
    state = MarketStructureState(timeframe="30m")
    zone_start = _ts(REPLAY_START)  # documented lower bound; data may start later
    out: list[tuple[pd.Series, list[StructureEvent], MarketStructureState, float | None, bool]] = []
    for i in range(len(ind)):
        row = ind.iloc[i]
        close_time = _ts(row["close_time"])
        atr = float(row["atr"]) if "atr" in ind.columns and pd.notna(row.get("atr")) else None
        state, evs = update_market_structure(
            state,
            candle=row,
            pivots=pivots,
            decision_time=close_time,
            atr=atr,
            cfg=cfg,
        )
        # Per-bar snapshot of the only structure fields the zone tracker reads.
        snap = MarketStructureState(
            timeframe="30m",
            last_confirmed_swing_high=state.last_confirmed_swing_high,
            last_confirmed_swing_low=state.last_confirmed_swing_low,
            last_updated_at=state.last_updated_at,
        )
        # Skip bars whose open is before the requested window when data exists earlier.
        # Zone tracking covers all closed bars with open >= max(data_start, REPLAY_START).
        in_window = bool(_ts(row["timestamp"]) >= zone_start and close_time <= _ts(REPLAY_END))
        out.append((row, list(evs), snap, atr, in_window))
    return out


def _candle_dict(row: pd.Series) -> dict[str, Any]:
    return {
        "timestamp": row["timestamp"],
        "close_time": row["close_time"],
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]) if "volume" in row.index and pd.notna(row.get("volume")) else 0.0,
    }


def replay_zones(
    bars: list[tuple[pd.Series, list[StructureEvent], MarketStructureState, float | None, bool]],
    cfg: ZoneConfig,
) -> TrendZoneTracker:
    tr = TrendZoneTracker(cfg)
    for row, evs, ms, atr, in_window in bars:
        if not in_window:
            continue
        tr.update(_candle_dict(row), evs, ms, atr)
    return tr


def baseline_config() -> ZoneConfig:
    """Research baseline for detailed artifacts (not a production decision)."""
    cfg = width_variant("W4")
    cfg = merge_variant("M1", cfg)
    cfg = episode_variant("E0", cfg)
    cfg = activation_variant("A1", cfg)
    cfg = rejection_variant("R0", cfg)
    cfg = break_variant("B0", cfg)
    # Audit must retain full history — production max_zones is unrelated.
    return ZoneConfig(**{**cfg.to_dict(), "max_zones": 2048})


def summarize_tracker(tr: TrendZoneTracker, variant_id: str) -> dict[str, Any]:
    zones = tr.zones
    n = len(zones)
    active = sum(1 for z in zones if z.state == "active")
    forming = sum(1 for z in zones if z.state == "forming")
    broken = sum(1 for z in zones if z.state == "broken")
    invalidated = sum(1 for z in zones if z.state == "invalidated")
    episodes = sum(z.touch_episode_count for z in zones)
    contacts = sum(z.contact_count for z in zones)
    rejections = sum(z.confirmed_rejection_count for z in zones)
    breaks = sum(z.successful_break_count for z in zones)
    failed = sum(z.failed_break_count for z in zones)
    flips = sum(1 for z in zones if z.flip_candidate)
    merges = len(tr.merge_log)
    # Quality proxies (diagnostic only)
    contact_per_episode = (contacts / episodes) if episodes else None
    episode_per_zone = (episodes / n) if n else None
    rejection_per_episode = (rejections / episodes) if episodes else None
    # Zones with only 1 contact and never broken → stale
    stale = sum(
        1
        for z in zones
        if z.touch_episode_count <= 1 and z.confirmed_rejection_count == 0 and z.state in {"forming", "active"}
    )
    # Over-merge proxy: very wide bands (> 1.5% of center)
    wide = sum(1 for z in zones if z.center_price > 0 and (z.width_abs / z.center_price) > 0.015)
    # Under-merge proxy: many overlapping same-role active/forming pairs
    overlap_pairs = 0
    live = [z for z in zones if z.state in {"forming", "active"}]
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if a.role == b.role and a.overlaps_band(b.lower_bound, b.upper_bound):
                overlap_pairs += 1
    return {
        "variant_id": variant_id,
        "n_zones": n,
        "n_active": active,
        "n_forming": forming,
        "n_broken": broken,
        "n_invalidated": invalidated,
        "n_merges": merges,
        "sum_contacts": contacts,
        "sum_touch_episodes": episodes,
        "sum_rejections": rejections,
        "sum_breaks": breaks,
        "sum_failed_breaks": failed,
        "n_flip_candidates": flips,
        "contact_per_episode": contact_per_episode,
        "episode_per_zone": episode_per_zone,
        "rejection_per_episode": rejection_per_episode,
        "n_stale_zones": stale,
        "n_wide_zones_gt_1_5pct": wide,
        "n_overlap_pairs_live": overlap_pairs,
        "config": tr.cfg.to_dict(),
        "config_key": tr.cfg.variant_key,
    }


def score_summary(s: dict[str, Any]) -> float:
    """Higher is better diagnostic balance (not production fitness)."""
    score = 0.0
    # Prefer fewer stale and fewer leftover overlaps
    score -= 2.0 * float(s.get("n_stale_zones") or 0)
    score -= 3.0 * float(s.get("n_overlap_pairs_live") or 0)
    score -= 1.5 * float(s.get("n_wide_zones_gt_1_5pct") or 0)
    # Prefer some rejections and breaks (zones that "do something")
    score += 0.5 * float(s.get("sum_rejections") or 0)
    score += 0.3 * float(s.get("sum_breaks") or 0)
    # Penalize extreme episode inflation (sideways overcount)
    cpe = s.get("contact_per_episode")
    if cpe is not None:
        # Want contacts clustered into fewer episodes: higher cpe is good up to a point
        score += min(float(cpe), 8.0) * 0.4
    epz = s.get("episode_per_zone")
    if epz is not None and float(epz) > 12:
        score -= (float(epz) - 12) * 0.5
    # Prefer moderate zone counts (not hundreds of noise zones)
    n = float(s.get("n_zones") or 0)
    if n < 5:
        score -= 5
    elif n > 200:
        score -= (n - 200) * 0.05
    return score


def zone_overlaps_focus(z: Any) -> bool:
    return not (z.upper_bound < FOCUS_LO or z.lower_bound > FOCUS_HI)


def build_focus_timeline(tr: TrendZoneTracker) -> list[dict[str, Any]]:
    """Timeline for zones overlapping ~1.02–1.03 in the focus window."""
    focus_ids: set[str] = set()
    for z in tr.zones:
        if zone_overlaps_focus(z) or FOCUS_LO <= z.center_price <= FOCUS_HI:
            focus_ids.add(z.zone_id)
        # Also catch historical centers from source prices in band
        for px in z.source_prices:
            if FOCUS_LO <= float(px) <= FOCUS_HI:
                focus_ids.add(z.zone_id)
                break
        # Birth during focus window near band
        if _ts(FOCUS_START) <= _ts(z.created_at) <= _ts(FOCUS_END):
            if abs(z.center_price - 1.025) <= 0.02:
                focus_ids.add(z.zone_id)

    rows: list[dict[str, Any]] = []

    def _add(kind: str, src: dict[str, Any]) -> None:
        zid = src.get("zone_id")
        if zid not in focus_ids:
            return
        # Deduplicate noisy kinds already covered by specialized logs
        if kind in {"confirmed_rejection", "break", "touch_episode_start"} and src.get("event_kind") in {
            "confirmed_rejection",
            "break",
            "touch_episode_start",
        }:
            # lifecycle duplicates rejection/break/touch specialized logs — skip lifecycle copies
            if "reason" not in src and kind == "confirmed_rejection" and "R0" not in str(src.get("reason_codes", "")):
                pass
        avail = src.get("event_available_timestamp") or src.get("decision_time")
        if avail:
            t = _ts(avail)
            if kind == "retest_contact" and (t < _ts(FOCUS_START) or t > _ts(FOCUS_END)):
                return
            if t < _ts(FOCUS_START) - pd.Timedelta(days=14) or t > _ts(FOCUS_END) + pd.Timedelta(days=2):
                if kind not in {"birth", "merge", "activate", "cluster_expand", "anchor_merge"} and (
                    t < _ts(FOCUS_START) or t > _ts(FOCUS_END)
                ):
                    return
        rows.append(
            {
                "event_kind": kind,
                "event_available_timestamp": avail,
                "candle_timestamp": src.get("candle_timestamp") or src.get("event_time"),
                "zone_id": zid,
                "role": src.get("role"),
                "state": src.get("state"),
                "lower_bound": src.get("lower_bound") or src.get("new_lower") or src.get("old_lower"),
                "upper_bound": src.get("upper_bound") or src.get("new_upper") or src.get("old_upper"),
                "inputs": src.get("inputs") or src.get("event_type") or src.get("reason_codes"),
                "reason_codes": src.get("reason_codes") or src.get("reason") or kind,
                "future_leakage": False,
                "raw": json.dumps({k: v for k, v in src.items() if k != "raw"}, sort_keys=True, default=str),
            }
        )

    # Prefer specialized logs; lifecycle only for kinds not covered elsewhere
    lifecycle_keep = {
        "birth",
        "anchor_merge",
        "activate",
        "bos_choch_context",
        "flip_candidate_after_break",
    }
    for a in tr.anchor_log:
        kind = "merge" if a.get("action") == "merged_into" else "birth"
        _add(kind, a)
    for m in tr.merge_log:
        _add("cluster_expand", m)
    for t in tr.touch_log:
        _add("touch_episode", t)
    for r in tr.rejection_log:
        _add("confirmed_rejection", r)
    for b in tr.break_log:
        _add("break", b)
    for f in tr.flip_log:
        _add(str(f.get("event_kind") or "flip"), f)
    for life in tr.lifecycle_log:
        ek = str(life.get("event_kind") or "lifecycle")
        if ek in lifecycle_keep:
            _add(ek, life)

    # Final status snapshot per focus zone
    for z in tr.zones:
        if z.zone_id not in focus_ids:
            continue
        rows.append(
            {
                "event_kind": "final_status",
                "event_available_timestamp": _iso(REPLAY_END),
                "candle_timestamp": _iso(z.last_contact_at),
                "zone_id": z.zone_id,
                "role": z.role,
                "state": z.state,
                "lower_bound": z.lower_bound,
                "upper_bound": z.upper_bound,
                "inputs": f"episodes={z.touch_episode_count};rej={z.confirmed_rejection_count};flip={z.flip_candidate}",
                "reason_codes": "end_of_replay_snapshot",
                "future_leakage": False,
                "raw": json.dumps(z.to_dict(), sort_keys=True, default=str),
            }
        )

    rows.sort(key=lambda r: (str(r.get("event_available_timestamp") or ""), str(r.get("event_kind"))))
    return rows


def collect_negatives(
    tr: TrendZoneTracker,
    bars: list[tuple[pd.Series, list[StructureEvent], MarketStructureState, float | None]],
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    # Sideways / many contacts one episode
    for z in tr.zones:
        if z.touch_episode_count == 1 and z.contact_count >= 6:
            examples.append(
                {
                    "neg_case": "many_candles_same_touch_episode",
                    "zone_id": z.zone_id,
                    "role": z.role,
                    "state": z.state,
                    "lower_bound": z.lower_bound,
                    "upper_bound": z.upper_bound,
                    "contact_count": z.contact_count,
                    "touch_episode_count": z.touch_episode_count,
                    "note": "sideways / multi-bar stay counted as one episode",
                }
            )
        if z.successful_break_count == 0 and z.failed_break_count >= 1 and z.confirmed_rejection_count >= 1:
            examples.append(
                {
                    "neg_case": "immediate_false_breakout_recovered",
                    "zone_id": z.zone_id,
                    "role": z.role,
                    "state": z.state,
                    "failed_break_count": z.failed_break_count,
                    "confirmed_rejection_count": z.confirmed_rejection_count,
                    "note": "failed break reinforced zone without successful break",
                }
            )
        if (
            z.state in {"forming", "active"}
            and z.touch_episode_count <= 1
            and z.confirmed_rejection_count == 0
            and z.contact_count <= 2
        ):
            examples.append(
                {
                    "neg_case": "stale_zone_little_reaction",
                    "zone_id": z.zone_id,
                    "role": z.role,
                    "state": z.state,
                    "created_at": _iso(z.created_at),
                    "contact_count": z.contact_count,
                    "note": "born but little later reaction",
                }
            )
        if z.touch_episode_count >= 4 and z.confirmed_rejection_count == 0 and z.successful_break_count == 0:
            examples.append(
                {
                    "neg_case": "zone_traded_through_without_clear_reaction",
                    "zone_id": z.zone_id,
                    "role": z.role,
                    "state": z.state,
                    "touch_episode_count": z.touch_episode_count,
                    "note": "multiple visits without rejection or break",
                }
            )

    # Single wick contact without close in band: scan a few active zones
    for z in tr.zones[:40]:
        wick_only = 0
        for row, _, _, _, in_window in bars:
            if not in_window:
                continue
            hi, lo, cl = float(row["high"]), float(row["low"]), float(row["close"])
            contact = not (hi < z.lower_bound or lo > z.upper_bound)
            close_in = z.lower_bound <= cl <= z.upper_bound
            if contact and not close_in:
                wick_only += 1
        if wick_only >= 3 and z.successful_break_count == 0:
            examples.append(
                {
                    "neg_case": "wick_only_contacts",
                    "zone_id": z.zone_id,
                    "role": z.role,
                    "wick_only_bars": wick_only,
                    "note": "wicks pierce band without close-through break",
                }
            )

    # Near but separate zones (centers close, no merge under M1 would be different — under baseline M1 they may merge)
    live = [z for z in tr.zones if z.state in {"forming", "active"}]
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            if a.role != b.role:
                continue
            dist = abs(a.center_price - b.center_price)
            if 0 < dist <= 0.01 and not a.overlaps_band(b.lower_bound, b.upper_bound):
                examples.append(
                    {
                        "neg_case": "two_near_but_separate_zones",
                        "zone_a": a.zone_id,
                        "zone_b": b.zone_id,
                        "center_a": a.center_price,
                        "center_b": b.center_price,
                        "dist": dist,
                        "note": "centers within 0.01 but bands do not overlap",
                    }
                )

    # Cap volume
    return examples[:200]


def export_tracker_artifacts(tr: TrendZoneTracker, out: Path, prefix: str = "") -> None:
    def p(name: str) -> Path:
        return out / f"{prefix}{name}" if prefix else out / name

    _write_csv(p("zone_lifecycle_events.csv"), [_flat(r) for r in tr.lifecycle_log])
    _write_csv(p("all_zones.csv"), [_flat(z.to_dict()) for z in tr.zones])
    _write_csv(p("zone_anchor_events.csv"), [_flat(r) for r in tr.anchor_log])
    _write_csv(p("zone_merge_audit.csv"), [_flat(r) for r in tr.merge_log])
    _write_csv(p("touch_episode_audit.csv"), [_flat(r) for r in tr.touch_log])
    _write_csv(p("rejection_audit.csv"), [_flat(r) for r in tr.rejection_log])
    _write_csv(p("breakout_audit.csv"), [_flat(r) for r in tr.break_log])
    _write_csv(p("flip_candidate_audit.csv"), [_flat(r) for r in tr.flip_log])


def main() -> None:
    out = OUT
    out.mkdir(parents=True, exist_ok=True)
    hashes_before = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
    }
    _p(f"hashes_before={hashes_before}")

    start, end = _ts(REPLAY_START), _ts(REPLAY_END)
    _p("loading 5m candles…")
    frame_5m = load_5m(start, end)
    data_min = _iso(frame_5m["timestamp"].min())
    data_max = _iso(frame_5m["timestamp"].max())
    _p(f"5m range available in load: {data_min} → {data_max} (n={len(frame_5m)})")

    _p("building 30m + pivots…")
    ind, pivots = build_30m(frame_5m, end)
    _p(f"30m closed bars in window: {len(ind)}  pivots={len(pivots)}")

    _p("precomputing structure events (once)…")
    bars = precompute_structure(ind, pivots)
    _p(f"structure bars={len(bars)}")

    # Determinism check
    base = baseline_config()
    tr1 = replay_zones(bars, base)
    tr2 = replay_zones(bars, base)
    det_ok = [z.to_dict() for z in tr1.zones] == [z.to_dict() for z in tr2.zones]
    _p(f"determinism_replay_ok={det_ok}")

    # Variant matrix: one-dimension sweeps around baseline
    def _cap(cfg: ZoneConfig) -> ZoneConfig:
        return ZoneConfig(**{**cfg.to_dict(), "max_zones": 2048})

    variants: list[tuple[str, ZoneConfig]] = [("BASE_W4_M1_E0_A1_R0_B0", base)]
    for w in ("W0", "W1", "W2", "W3", "W4", "W5"):
        cfg = width_variant(w)
        cfg = merge_variant("M1", cfg)
        cfg = episode_variant("E0", cfg)
        cfg = activation_variant("A1", cfg)
        cfg = rejection_variant("R0", cfg)
        cfg = break_variant("B0", cfg)
        variants.append((f"Wsweep_{w}", _cap(cfg)))
    for m in ("M0", "M1", "M2"):
        cfg = merge_variant(m, width_variant("W4"))
        cfg = episode_variant("E0", cfg)
        cfg = activation_variant("A1", cfg)
        cfg = rejection_variant("R0", cfg)
        cfg = break_variant("B0", cfg)
        variants.append((f"Msweep_{m}", _cap(cfg)))
    for e in ("E0", "E1", "E2"):
        cfg = episode_variant(e, merge_variant("M1", width_variant("W4")))
        cfg = activation_variant("A1", cfg)
        cfg = rejection_variant("R0", cfg)
        cfg = break_variant("B0", cfg)
        variants.append((f"Esweep_{e}", _cap(cfg)))
    for a in ("A0", "A1", "A2", "A3"):
        cfg = activation_variant(a, episode_variant("E0", merge_variant("M1", width_variant("W4"))))
        cfg = rejection_variant("R0", cfg)
        cfg = break_variant("B0", cfg)
        variants.append((f"Asweep_{a}", _cap(cfg)))
    for r in ("R0", "R1", "R2", "R3"):
        cfg = rejection_variant(r, activation_variant("A1", episode_variant("E0", merge_variant("M1", width_variant("W4")))))
        cfg = break_variant("B0", cfg)
        variants.append((f"Rsweep_{r}", _cap(cfg)))
    for b in ("B0", "B1", "B2", "B3"):
        cfg = break_variant(b, rejection_variant("R0", activation_variant("A1", episode_variant("E0", merge_variant("M1", width_variant("W4"))))))
        variants.append((f"Bsweep_{b}", _cap(cfg)))

    # Deduplicate identical keys
    seen_keys: set[str] = set()
    unique_variants: list[tuple[str, ZoneConfig]] = []
    for vid, cfg in variants:
        key = cfg.variant_key
        if key in seen_keys and vid != "BASE_W4_M1_E0_A1_R0_B0":
            continue
        seen_keys.add(key)
        unique_variants.append((vid, cfg))

    summaries: list[dict[str, Any]] = []
    trackers: dict[str, TrendZoneTracker] = {}
    for vid, cfg in unique_variants:
        _p(f"replay {vid}…")
        tr = replay_zones(bars, cfg)
        trackers[vid] = tr
        s = summarize_tracker(tr, vid)
        s["score"] = score_summary(s)
        summaries.append(s)

    # Best per family
    def best_of(prefix: str, label_key: str) -> dict[str, Any] | None:
        fam = [s for s in summaries if s["variant_id"].startswith(prefix)]
        if not fam:
            return None
        best = max(fam, key=lambda x: float(x["score"]))
        return {"family": label_key, "best_variant_id": best["variant_id"], "score": best["score"], **{k: best[k] for k in ("n_zones", "n_stale_zones", "n_overlap_pairs_live", "sum_rejections", "sum_breaks", "contact_per_episode")}}

    bests = {
        "width": best_of("Wsweep_", "width"),
        "merge": best_of("Msweep_", "merge"),
        "episode": best_of("Esweep_", "episode"),
        "activation": best_of("Asweep_", "activation"),
        "rejection": best_of("Rsweep_", "rejection"),
        "break": best_of("Bsweep_", "break"),
        "baseline": next(s for s in summaries if s["variant_id"].startswith("BASE_")),
    }

    # Detailed artifacts for baseline
    base_tr = trackers["BASE_W4_M1_E0_A1_R0_B0"]
    export_tracker_artifacts(base_tr, out)
    focus_rows = build_focus_timeline(base_tr)
    _write_csv(out / "aptusdt_102_103_timeline.csv", [_flat(r) for r in focus_rows])
    neg = collect_negatives(base_tr, bars)
    _write_csv(out / "negative_examples.csv", [_flat(r) for r in neg])

    # Also export best width tracker timeline if different
    if bests["width"] and bests["width"]["best_variant_id"] in trackers:
        bw = trackers[bests["width"]["best_variant_id"]]
        export_tracker_artifacts(bw, out, prefix="best_width_")

    summary_rows = []
    for s in summaries:
        row = {k: v for k, v in s.items() if k != "config"}
        row["config_json"] = json.dumps(s["config"], sort_keys=True)
        summary_rows.append(row)
    _write_csv(out / "variant_summary.csv", summary_rows)

    config_variants = {
        "note": "Phase A diagnostic only. Band width frozen at zone birth (ATR/pct at creation); later ATR does not reshape existing zones.",
        "atr_freeze_policy": "birth_atr_frozen",
        "replay_requested": {"start": REPLAY_START, "end": REPLAY_END},
        "data_available_5m": {"min": data_min, "max": data_max},
        "baseline": base.to_dict(),
        "variants": {vid: cfg.to_dict() for vid, cfg in unique_variants},
        "bests": bests,
        "determinism_ok": det_ok,
        "hashes_before": hashes_before,
    }
    _write_json(out / "config_variants.json", config_variants)

    hashes_after = {
        "trend_structure.py": _md5(STRUCTURE),
        "trend_state_machine.py": _md5(MACHINE),
        "trend_state_policy.py": _md5(POLICY),
    }
    assert hashes_before == hashes_after, "protected files changed"

    # Decision heuristic
    base_s = bests["baseline"]
    n_zones = int(base_s["n_zones"])
    stale = int(base_s["n_stale_zones"])
    overlaps = int(base_s["n_overlap_pairs_live"])
    if n_zones > 120 or stale > 40:
        decision = "L"
        decision_note = "Anker erzeugen zu viel Rauschen; Inputs müssen reduziert werden."
    elif n_zones < 3 or int(base_s["sum_rejections"]) == 0:
        decision = "M"
        decision_note = "Bestehende Structure-Events reichen für stabile Zonen nicht aus."
    elif not det_ok:
        decision = "K"
        decision_note = "Architektur grundsätzlich ok, aber Determinismus/Parameter noch nicht belastbar."
    elif bests["width"] and bests["episode"] and bests["break"]:
        # Architecture works; one-dimension sweeps give directional preference but not full factorial calibration
        decision = "K"
        decision_note = (
            "Skeleton und Audit funktionieren; Ein-Achsen-Sweeps liefern Richtungspräferenzen, "
            "aber keine vollständige Produktionskalibrierung (kein vollständiges Faktor-Gitter)."
        )
    else:
        decision = "K"
        decision_note = "Architektur funktioniert, Parameterdaten noch nicht ausreichend."

    # Focus zone narrative for README
    focus_zones = [z for z in base_tr.zones if zone_overlaps_focus(z) or FOCUS_LO <= z.center_price <= FOCUS_HI]
    focus_brief = [
        {
            "zone_id": z.zone_id,
            "role": z.role,
            "state": z.state,
            "bounds": [z.lower_bound, z.upper_bound],
            "created_at": _iso(z.created_at),
            "confirmed_at": _iso(z.confirmed_at),
            "broken_at": _iso(z.broken_at),
            "episodes": z.touch_episode_count,
            "rejections": z.confirmed_rejection_count,
            "flip_candidate": z.flip_candidate,
            "retest_contact_at": _iso(z.retest_contact_at),
            "retest_rejection_at": _iso(z.retest_rejection_at),
        }
        for z in focus_zones
    ]

    readme = f"""# Trend Zones Audit — Phase A (causal 30m S/R)

**Date:** 2026-07-10
**Decision:** **{decision}** — {decision_note}

## Scope

- New research modules only: `trend_zones.py`, `trend_zones_audit.py`, `tests/test_trend_zones.py`
- No production policy / entry / early / strong changes
- `trend_structure.py`, `trend_state_machine.py`, `trend_state_policy.py` **unchanged**
- V6+V2, G6, HTF, Bottoming/Topping untouched

## Protected hashes (before = after)

| File | MD5 |
|---|---|
| trend_structure.py | `{hashes_after['trend_structure.py']}` |
| trend_state_machine.py | `{hashes_after['trend_state_machine.py']}` |
| trend_state_policy.py | `{hashes_after['trend_state_policy.py']}` |

## Replay window

- Requested: `{REPLAY_START}` → `{REPLAY_END}` (APTUSDT 30m, closed buckets only)
- 5m data available in load: `{data_min}` → `{data_max}`
- Note: candle store starts after 2025-12-20; audit uses available history from first closed 30m bar ≥ requested start.

## ATR / width freeze policy

**Band half-width is computed once at zone birth from the ATR (and/or pct) available on that closed bar and then frozen.**
Later ATR changes do **not** reshape existing `lower_bound` / `upper_bound`. Merges may expand bounds from a *new* anchor’s frozen half-width only.

## Tracker interface

```python
class TrendZoneTracker:
    def update(self, candle, structure_events, market_structure, atr) -> ZoneContext: ...
```

Inputs per fully closed 30m candle: OHLCV (+ `close_time`), new `StructureEvent`s, `MarketStructureState`, ATR.

## Baseline config

`W4` (max(0.10%, 0.20×ATR)) + `M1` (band overlap merge) + `E0` (2 bars fully outside to re-arm) + `A1` (active after 2nd independent episode) + `R0` (close back on expected outside) + `B0` (single close beyond opposite edge).

Deterministic replay: `{det_ok}`

## Variant preferences (one-axis sweeps, diagnostic score)

| Family | Best variant id | Notes |
|---|---|---|
| Width | `{bests['width']['best_variant_id'] if bests['width'] else 'n/a'}` | score={bests['width']['score'] if bests['width'] else 'n/a'} |
| Merge | `{bests['merge']['best_variant_id'] if bests['merge'] else 'n/a'}` | |
| Episode | `{bests['episode']['best_variant_id'] if bests['episode'] else 'n/a'}` | |
| Activation | `{bests['activation']['best_variant_id'] if bests['activation'] else 'n/a'}` | |
| Rejection | `{bests['rejection']['best_variant_id'] if bests['rejection'] else 'n/a'}` | |
| Break | `{bests['break']['best_variant_id'] if bests['break'] else 'n/a'}` | |

Full table: `variant_summary.csv`. Config dump: `config_variants.json`.

## Focus zone ~1.02–1.03 (2026-02-25 … 2026-03-27)

Zones overlapping the band under baseline:

```json
{json.dumps(json_safe(focus_brief), indent=2)}
```

Event-level timeline: `aptusdt_102_103_timeline.csv`
(fields: event_available_timestamp, candle_timestamp, bounds, inputs, reason_codes, future_leakage=false).

## Negatives

See `negative_examples.csv` (wick-only, sideways same-episode, false breakout recoveries, near-separate zones, stale zones, trade-through without reaction).

## Known limits

- No full factorial W×M×E×A×R×B calibration
- Flip is diagnostic only (`flip_candidate` / retest timestamps); no confirmed flip production rule
- BOS/CHoCH never birth zones in Phase A
- Synthetic equal_high/equal_low birth for first unlabeled confirmed pivots
- Score function is a research heuristic, not a trading objective

## Phase B recommendation

1. Lock width/merge/episode from the preferred one-axis winners and re-run a small cross grid
2. Keep zones **read-only** relative to Early/Strong; do not feed zones into entry yet
3. Only after flip confirmation rules are audited, consider optional SM context tags
4. Reduce birth anchors if decision drifts toward L (drop equal_* synth / sweep-only births)

## Artifacts

- config_variants.json
- zone_lifecycle_events.csv
- all_zones.csv
- zone_anchor_events.csv
- zone_merge_audit.csv
- touch_episode_audit.csv
- rejection_audit.csv
- breakout_audit.csv
- flip_candidate_audit.csv
- aptusdt_102_103_timeline.csv
- negative_examples.csv
- variant_summary.csv
- README.md (this file)
"""
    (out / "README.md").write_text(readme, encoding="utf-8")
    _write_json(
        out / "decision.json",
        {
            "decision": decision,
            "note": decision_note,
            "bests": bests,
            "hashes": hashes_after,
            "determinism_ok": det_ok,
            "focus_zones": focus_brief,
        },
    )
    _p(f"DONE decision={decision} artifacts→{out}")


if __name__ == "__main__":
    main()
