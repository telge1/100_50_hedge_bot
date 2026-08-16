#!/usr/bin/env python3
"""AUDIT_51_COIN_SIGNAL_COLLISIONS_AND_REALISTIC_EXECUTION — research-only.

Regenerate all GLOBAL_FROZEN_TIER_A Tier-A signals (all SIGNAL_TFS) over the
ready ClickHouse window, then measure same-time / near-time / multi-TF
collisions and compare RAW vs SAME_TIME_DEDUP vs ONE_ACTIVE_POSITION variants.

No strategy changes. No DB writes. No dashboard changes.
"""

from __future__ import annotations

import csv
import json
import math
import os
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.config import get_clickhouse_settings  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.db.setup import setup_clickhouse  # noqa: E402
from signal_generator.pipeline.versions import GLOBAL_FROZEN_TIER_A  # noqa: E402
from signal_generator.research.one_m_entry_timing.timing import (  # noqa: E402
    _prepare_1m,
    _simulate_outcome,
    _utc,
)
from signal_generator.strategy.wave_fade.adapter import (  # noqa: E402
    bars_to_ohlcv_df,
    one_minute_books,
)
from signal_generator.strategy.wave_fade.edges import load_frozen_eff_edges  # noqa: E402
from signal_generator.strategy.wave_fade.parameters import (  # noqa: E402
    PRIMARY_FEE,
    SIGNAL_TFS,
    TPSL_BY_TF,
)
from signal_generator.strategy.wave_fade.signals import (  # noqa: E402
    build_symbol_signals,
    build_waves_from_ohlcv,
    resolve_entries,
)
from signal_generator.timeframes import (  # noqa: E402
    aggregate_1m_to_timeframe,
    bars_from_mappings,
)

UNIVERSE = ROOT / "config" / "universe_tradeable_51.json"
OUT = ROOT / "results" / "51_coin_signal_collision_audit"
CACHE = OUT / "raw_signals_cache.csv"

WINDOW_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
WINDOW_END = datetime(2026, 8, 11, tzinfo=timezone.utc)  # exclusive on entry
WARMUP_DAYS = 80  # enough for 4h EMA400
FEE = float(PRIMARY_FEE)
FOCUS = ("APTUSDT", "DOGEUSDT", "HYPEUSDT", "SOLUSDT")
NEAR_WINDOWS_MIN = (1, 5, 15, 30, 60)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _iso(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and math.isnan(ts)):
        return None
    if isinstance(ts, str):
        return ts
    t = _utc(ts)
    return t.isoformat().replace("+00:00", "Z")


def load_symbols() -> list[str]:
    raw = json.loads(UNIVERSE.read_text(encoding="utf-8"))
    return [str(s).upper() for s in raw["symbols"]]


def regenerate_symbol(
    *,
    sym: str,
    candle_repo: CandleRepository,
    edges: dict,
    load_start: datetime,
    window_start: datetime,
    window_end: datetime,
) -> list[dict[str, Any]]:
    raw = candle_repo.get_candles(sym, load_start, window_end) or []
    if len(raw) < 1000:
        print(f"  {sym}: insufficient candles n={len(raw)}", flush=True)
        return []

    bars = bars_from_mappings(raw)
    ohlcv_1m = bars_to_ohlcv_df(bars)
    open_times, opens = one_minute_books(ohlcv_1m)
    work = _prepare_1m(pd.DataFrame(raw))

    waves_by_tf: dict[str, pd.DataFrame] = {}
    for tf in SIGNAL_TFS:
        htf = aggregate_1m_to_timeframe(bars, tf, as_of=window_end, require_complete=True)
        ohlcv = bars_to_ohlcv_df(htf)
        waves_by_tf[tf] = build_waves_from_ohlcv(ohlcv, symbol=sym, timeframe=tf)

    all_sig = build_symbol_signals(sym, edges, waves_by_tf)
    if all_sig.empty:
        return []
    tier = all_sig[all_sig["is_tier_a"].astype(bool)].copy()
    if tier.empty:
        return []

    resolved = resolve_entries(tier, open_times, opens)
    resolved = resolved[resolved["entry_valid"].astype(bool)].copy()

    trades: list[dict[str, Any]] = []
    for _, row in resolved.iterrows():
        side = str(row["side"]).upper()
        tf = str(row["signal_tf"])
        entry_ts = _utc(row["entry_time"])
        if entry_ts < window_start or entry_ts >= window_end:
            continue
        entry_px = float(row["entry_price"])
        entry_i = int(work["timestamp"].searchsorted(pd.Timestamp(entry_ts), side="left"))
        if entry_i < 0 or entry_i >= len(work):
            continue
        if _utc(work.iloc[entry_i]["timestamp"]) > window_end:
            continue
        sim = _simulate_outcome(
            side=side, entry=entry_px, tf=tf, entry_i=entry_i, work=work, as_of=window_end
        )
        result = sim["result"]
        pnl = sim.get("pnl_pct")
        net = (float(pnl) - FEE) if pnl is not None and result in ("WIN", "LOSS") else None
        tp_pct, sl_pct = TPSL_BY_TF[tf]
        if side == "LONG":
            tp_price = entry_px * (1.0 + tp_pct / 100.0)
            sl_price = entry_px * (1.0 - sl_pct / 100.0)
        else:
            tp_price = entry_px * (1.0 - tp_pct / 100.0)
            sl_price = entry_px * (1.0 + sl_pct / 100.0)

        conf = row.get("confirmation_available_at")
        sid = f"{sym}|{tf}|{side}|{_iso(entry_ts)}|{_iso(conf)}"
        exit_ts = sim.get("exit_time")
        trades.append(
            {
                "signal_id": sid,
                "symbol": sym,
                "direction": side,
                "timeframe": tf,
                "signal_timestamp": _iso(conf),
                "entry_timestamp": _iso(entry_ts),
                "entry_price": entry_px,
                "tp_price": float(sim.get("tp_price") or tp_price),
                "sl_price": float(sim.get("sl_price") or sl_price),
                "exit_timestamp": exit_ts if isinstance(exit_ts, str) else _iso(exit_ts),
                "exit_price": sim.get("exit_price"),
                "outcome": sim.get("exit_reason") if result in ("WIN", "LOSS") else result,
                "result": result,
                "exit_reason": sim.get("exit_reason"),
                "pnl": pnl,
                "net": net,
                "mae": sim.get("mae_pct"),
                "mfe": sim.get("mfe_pct"),
                "duration_seconds": sim.get("duration_seconds"),
            }
        )
    return trades


def parse_ts(s: str | None) -> datetime | None:
    if not s or (isinstance(s, float) and math.isnan(s)):
        return None
    return _utc(pd.Timestamp(s))


def equity_stats(nets: list[float]) -> dict[str, Any]:
    if not nets:
        return {
            "trades": 0, "winners": 0, "sl": 0, "WR": None, "Net": 0.0,
            "Net_per_trade": None, "PF": None, "MaxDD": 0.0, "max_losing_streak": 0,
            "gross_profit": 0.0, "gross_loss": 0.0,
        }
    arr = np.asarray(nets, dtype=float)
    # outcomes passed separately usually; here nets only
    eq = np.cumsum(arr)
    peak = np.maximum.accumulate(eq)
    max_dd = float((eq - peak).min())
    gp = float(arr[arr > 0].sum()) if (arr > 0).any() else 0.0
    gl = float((-arr[arr < 0]).sum()) if (arr < 0).any() else 0.0
    pf = (gp / gl) if gl > 0 else None
    # losing streak on net sign
    streak = max_streak = 0
    for x in arr:
        if x < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return {
        "trades": len(nets),
        "Net": float(arr.sum()),
        "Net_per_trade": float(arr.mean()),
        "PF": float(pf) if pf is not None else None,
        "MaxDD": max_dd,
        "max_losing_streak": int(max_streak),
        "gross_profit": gp,
        "gross_loss": -gl,
    }


def perf_from_trades(trades: list[dict]) -> dict[str, Any]:
    closed = [t for t in trades if t.get("result") in ("WIN", "LOSS") and t.get("net") is not None]
    nets = [float(t["net"]) for t in closed]
    base = equity_stats(nets)
    winners = sum(1 for t in closed if t["result"] == "WIN")
    sl = sum(1 for t in closed if str(t.get("outcome") or t.get("exit_reason")) == "SL")
    n = len(closed)
    base.update(
        {
            "closed": n,
            "open": sum(1 for t in trades if t.get("result") == "OPEN"),
            "signals": len(trades),
            "winners": winners,
            "sl": sl,
            "WR": (100.0 * winners / n) if n else None,
        }
    )
    return base


def event_sort_key(t: dict) -> tuple:
    """Deterministic First-Come order (causal): signal_ts, entry_ts, tf, direction, id."""
    return (
        t.get("signal_timestamp") or "",
        t.get("entry_timestamp") or "",
        t.get("timeframe") or "",
        t.get("direction") or "",
        t.get("signal_id") or "",
    )


def variant_same_time_dedup(trades: list[dict]) -> list[dict]:
    """ONE_ENTRY_PER_SYMBOL_TIMESTAMP — first-come per (symbol, entry_timestamp)."""
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        by_key[(t["symbol"], t["entry_timestamp"])].append(t)
    kept = []
    for group in by_key.values():
        group_sorted = sorted(group, key=event_sort_key)
        kept.append(group_sorted[0])
    return sorted(kept, key=event_sort_key)


def variant_one_active(
    trades: list[dict],
    *,
    per_direction: bool = False,
) -> list[dict]:
    """Skip new entries while a position is open on symbol (or symbol+direction)."""
    ordered = sorted(trades, key=event_sort_key)
    # active: key -> exit_ts datetime
    active: dict[Any, datetime] = {}
    kept: list[dict] = []
    for t in ordered:
        entry = parse_ts(t["entry_timestamp"])
        if entry is None:
            continue
        key = (t["symbol"], t["direction"]) if per_direction else t["symbol"]
        # drop expired
        if key in active and active[key] <= entry:
            del active[key]
        if key in active:
            continue  # blocked
        kept.append(t)
        exit_ts = parse_ts(t.get("exit_timestamp"))
        if exit_ts is None:
            # OPEN until window end
            exit_ts = WINDOW_END
        # only occupy if closed or open
        if t.get("result") in ("WIN", "LOSS", "OPEN"):
            active[key] = exit_ts
    return kept


def build_exact_collision_groups(trades: list[dict]) -> list[dict]:
    groups_sym_ts: dict[tuple[str, str], list[dict]] = defaultdict(list)
    groups_sym_dir_ts: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for t in trades:
        groups_sym_ts[(t["symbol"], t["entry_timestamp"])].append(t)
        groups_sym_dir_ts[(t["symbol"], t["direction"], t["entry_timestamp"])].append(t)

    rows = []
    for (sym, ets), g in groups_sym_ts.items():
        if len(g) < 2:
            continue
        dirs = sorted({x["direction"] for x in g})
        tfs = sorted({x["timeframe"] for x in g})
        outcomes = [str(x.get("outcome") or x.get("result")) for x in g]
        uniq_out = sorted(set(outcomes))
        entries = [float(x["entry_price"]) for x in g]
        exit_ts = [x.get("exit_timestamp") for x in g]
        all_same_outcome = len(uniq_out) == 1
        same_sl_ts = (
            all_same_outcome
            and uniq_out[0] == "SL"
            and len(set(exit_ts)) == 1
        )
        entry_spread_bps = (
            10000.0 * (max(entries) - min(entries)) / np.mean(entries) if entries else None
        )
        # classification
        if len(dirs) == 1 and all_same_outcome and (entry_spread_bps or 0) < 5:
            if len(tfs) > 1:
                klass = "MULTI_TF_SAME_EVENT"
            else:
                klass = "DUPLICATE_MARKET_EVENT"
        elif len(dirs) > 1:
            klass = "AMBIGUOUS"
        else:
            klass = "AMBIGUOUS"

        side_mix = (
            "LONG/SHORT"
            if set(dirs) == {"LONG", "SHORT"}
            else ("LONG/LONG" if dirs == ["LONG"] else "SHORT/SHORT")
        )
        rows.append(
            {
                "group_key": f"{sym}|{ets}",
                "symbol": sym,
                "entry_timestamp": ets,
                "n_signals": len(g),
                "directions": ",".join(dirs),
                "side_mix": side_mix,
                "timeframes": ",".join(tfs),
                "unique_outcomes": ",".join(uniq_out),
                "all_same_outcome": all_same_outcome,
                "same_sl_timestamp": same_sl_ts,
                "entry_price_min": min(entries),
                "entry_price_max": max(entries),
                "entry_spread_bps": entry_spread_bps,
                "classification": klass,
                "signal_ids": "|".join(x["signal_id"] for x in sorted(g, key=event_sort_key)),
                "nets": "|".join(
                    str(x.get("net")) if x.get("net") is not None else "" for x in g
                ),
                "also_same_dir_group_size": max(
                    len(groups_sym_dir_ts[(sym, d, ets)]) for d in dirs
                ),
            }
        )
    return sorted(rows, key=lambda r: (-r["n_signals"], r["symbol"], r["entry_timestamp"]))


def near_time_stats(trades: list[dict]) -> list[dict]:
    """Per symbol+direction: fraction of signals that have a neighbor within W minutes."""
    by_sd: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        by_sd[(t["symbol"], t["direction"])].append(t)

    rows = []
    for (sym, direction), group in by_sd.items():
        sorted_g = sorted(group, key=lambda x: x["entry_timestamp"] or "")
        times = [parse_ts(x["entry_timestamp"]) for x in sorted_g]
        times = [t for t in times if t is not None]
        n = len(times)
        if n == 0:
            continue
        for w in NEAR_WINDOWS_MIN:
            involved = set()
            for i, t0 in enumerate(times):
                for j in range(i + 1, n):
                    dt = (times[j] - t0).total_seconds() / 60.0
                    if dt > w:
                        break
                    if dt >= 0:
                        involved.add(i)
                        involved.add(j)
            rows.append(
                {
                    "symbol": sym,
                    "direction": direction,
                    "window_minutes": w,
                    "signals": n,
                    "signals_with_near_neighbor": len(involved),
                    "near_rate": round(100.0 * len(involved) / n, 2) if n else None,
                    "pair_count": None,  # filled below optionally
                }
            )
            # pair count
            pairs = 0
            for i, t0 in enumerate(times):
                for j in range(i + 1, n):
                    dt = (times[j] - t0).total_seconds() / 60.0
                    if dt > w:
                        break
                    pairs += 1
            rows[-1]["pair_count"] = pairs
    return rows


def multi_tf_collisions(trades: list[dict], window_min: int = 15) -> list[dict]:
    """Groups where ≥2 distinct TFs fire same symbol+direction within window."""
    by_sd: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        by_sd[(t["symbol"], t["direction"])].append(t)

    out = []
    for (sym, direction), group in by_sd.items():
        sorted_g = sorted(group, key=lambda x: x["entry_timestamp"] or "")
        n = len(sorted_g)
        used = set()
        for i in range(n):
            if i in used:
                continue
            cluster = [sorted_g[i]]
            t0 = parse_ts(sorted_g[i]["entry_timestamp"])
            if t0 is None:
                continue
            for j in range(i + 1, n):
                t1 = parse_ts(sorted_g[j]["entry_timestamp"])
                if t1 is None:
                    continue
                if (t1 - t0).total_seconds() / 60.0 > window_min:
                    break
                cluster.append(sorted_g[j])
            tfs = {x["timeframe"] for x in cluster}
            if len(tfs) >= 2 and len(cluster) >= 2:
                for x in cluster:
                    used.add(sorted_g.index(x) if False else id(x))  # noqa: placeholder
                # better mark by signal_id
                outcomes = [str(x.get("outcome") or x.get("result")) for x in cluster]
                entries = [float(x["entry_price"]) for x in cluster]
                # would be simultaneously open? check overlap of [entry, exit]
                simultaneous = False
                for a in range(len(cluster)):
                    for b in range(a + 1, len(cluster)):
                        ea = parse_ts(cluster[a]["entry_timestamp"])
                        xa = parse_ts(cluster[a].get("exit_timestamp")) or WINDOW_END
                        eb = parse_ts(cluster[b]["entry_timestamp"])
                        xb = parse_ts(cluster[b].get("exit_timestamp")) or WINDOW_END
                        if ea and eb and ea < xb and eb < xa:
                            simultaneous = True
                out.append(
                    {
                        "symbol": sym,
                        "direction": direction,
                        "window_minutes": window_min,
                        "n_signals": len(cluster),
                        "timeframes": ",".join(sorted(tfs)),
                        "entry_timestamps": "|".join(x["entry_timestamp"] for x in cluster),
                        "outcomes": "|".join(outcomes),
                        "entry_price_min": min(entries),
                        "entry_price_max": max(entries),
                        "entry_spread_bps": 10000.0 * (max(entries) - min(entries)) / np.mean(entries),
                        "would_be_simultaneously_open": simultaneous,
                        "all_same_outcome": len(set(outcomes)) == 1,
                    }
                )
                # mark involved so we don't re-seed from mid-cluster excessively
                # simple: advance by taking next after cluster end — use signal ids
        # Rebuild without the broken used-set: greedy non-overlapping cluster seeds
    # Re-do cleanly:
    out = []
    for (sym, direction), group in by_sd.items():
        sorted_g = sorted(group, key=lambda x: x["entry_timestamp"] or "")
        i = 0
        while i < len(sorted_g):
            t0 = parse_ts(sorted_g[i]["entry_timestamp"])
            if t0 is None:
                i += 1
                continue
            cluster = [sorted_g[i]]
            j = i + 1
            while j < len(sorted_g):
                t1 = parse_ts(sorted_g[j]["entry_timestamp"])
                if t1 is None or (t1 - t0).total_seconds() / 60.0 > window_min:
                    break
                cluster.append(sorted_g[j])
                j += 1
            tfs = {x["timeframe"] for x in cluster}
            if len(tfs) >= 2 and len(cluster) >= 2:
                outcomes = [str(x.get("outcome") or x.get("result")) for x in cluster]
                entries = [float(x["entry_price"]) for x in cluster]
                simultaneous = False
                for a in range(len(cluster)):
                    for b in range(a + 1, len(cluster)):
                        ea = parse_ts(cluster[a]["entry_timestamp"])
                        xa = parse_ts(cluster[a].get("exit_timestamp")) or WINDOW_END
                        eb = parse_ts(cluster[b]["entry_timestamp"])
                        xb = parse_ts(cluster[b].get("exit_timestamp")) or WINDOW_END
                        if ea and eb and ea < xb and eb < xa:
                            simultaneous = True
                out.append(
                    {
                        "symbol": sym,
                        "direction": direction,
                        "window_minutes": window_min,
                        "n_signals": len(cluster),
                        "timeframes": ",".join(sorted(tfs)),
                        "entry_timestamps": "|".join(x["entry_timestamp"] for x in cluster),
                        "outcomes": "|".join(outcomes),
                        "entry_price_min": min(entries),
                        "entry_price_max": max(entries),
                        "entry_spread_bps": round(
                            10000.0 * (max(entries) - min(entries)) / np.mean(entries), 4
                        ),
                        "would_be_simultaneously_open": simultaneous,
                        "all_same_outcome": len(set(outcomes)) == 1,
                    }
                )
            i = j if j > i + 1 else i + 1
    return out


def triple_or_more_cases(exact_groups: list[dict], trades: list[dict]) -> list[dict]:
    by_id = {t["signal_id"]: t for t in trades}
    rows = []
    for g in exact_groups:
        if g["n_signals"] < 3:
            continue
        members = [by_id[sid] for sid in g["signal_ids"].split("|") if sid in by_id]
        all_sl = all(str(m.get("outcome")) == "SL" for m in members)
        rows.append(
            {
                "symbol": g["symbol"],
                "entry_timestamp": g["entry_timestamp"],
                "n_signals": g["n_signals"],
                "directions": g["directions"],
                "timeframes": g["timeframes"],
                "entries": "|".join(str(m["entry_price"]) for m in members),
                "exits": "|".join(str(m.get("exit_timestamp") or "") for m in members),
                "outcomes": "|".join(str(m.get("outcome") or "") for m in members),
                "sl_timestamps": "|".join(
                    str(m.get("exit_timestamp") or "")
                    for m in members
                    if str(m.get("outcome")) == "SL"
                ),
                "nets": "|".join(str(m.get("net") if m.get("net") is not None else "") for m in members),
                "all_sl": all_sl,
                "classification": g["classification"],
            }
        )
    return rows


def coin_impact(
    raw: list[dict],
    dedup: list[dict],
    exact_groups: list[dict],
) -> list[dict]:
    raw_by = defaultdict(list)
    ded_by = defaultdict(list)
    for t in raw:
        raw_by[t["symbol"]].append(t)
    for t in dedup:
        ded_by[t["symbol"]].append(t)
    coll_sigs = set()
    for g in exact_groups:
        for sid in g["signal_ids"].split("|"):
            coll_sigs.add(sid)

    rows = []
    for sym in sorted(raw_by.keys()):
        r = raw_by[sym]
        d = ded_by.get(sym, [])
        pr = perf_from_trades(r)
        pd_ = perf_from_trades(d)
        coll_n = sum(1 for t in r if t["signal_id"] in coll_sigs)
        raw_sl = pr["sl"]
        ded_sl = pd_["sl"]
        rows.append(
            {
                "symbol": sym,
                "raw_trades": pr["closed"],
                "dedup_trades": pd_["closed"],
                "collision_signals": coll_n,
                "collision_rate": round(100.0 * coll_n / len(r), 2) if r else None,
                "raw_SL": raw_sl,
                "dedup_SL": ded_sl,
                "SL_inflation": raw_sl - ded_sl,
                "raw_Net": pr["Net"],
                "dedup_Net": pd_["Net"],
                "delta_Net": pd_["Net"] - pr["Net"],
                "raw_WR": pr["WR"],
                "dedup_WR": pd_["WR"],
                "raw_MaxDD": pr["MaxDD"],
                "dedup_MaxDD": pd_["MaxDD"],
            }
        )
    return rows


def _worker_regen_symbol(sym: str) -> tuple[str, list[dict[str, Any]], str | None]:
    """Process-pool worker: own ClickHouse client per symbol."""
    try:
        edges = load_frozen_eff_edges()
        ch = setup_clickhouse(settings=get_clickhouse_settings())
        candle_repo = CandleRepository(ch)
        load_start = WINDOW_START - timedelta(days=WARMUP_DAYS)
        trades = regenerate_symbol(
            sym=sym,
            candle_repo=candle_repo,
            edges=edges,
            load_start=load_start,
            window_start=WINDOW_START,
            window_end=WINDOW_END,
        )
        ch.close()
        return sym, trades, None
    except Exception as exc:  # noqa: BLE001
        return sym, [], str(exc)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    symbols = load_symbols()
    assert len(symbols) == 51
    assert GLOBAL_FROZEN_TIER_A is True

    load_start = WINDOW_START - timedelta(days=WARMUP_DAYS)
    workers = max(1, min(6, (os.cpu_count() or 4)))

    # Load or regenerate
    if CACHE.exists() and "--force-regen" not in sys.argv:
        print(f"Loading cached signals from {CACHE}", flush=True)
        df = pd.read_csv(CACHE)
        all_trades = df.to_dict(orient="records")
        # normalize types
        for t in all_trades:
            if t.get("net") == "" or (isinstance(t.get("net"), float) and math.isnan(t["net"])):
                t["net"] = None
            elif t.get("net") is not None:
                t["net"] = float(t["net"])
    else:
        print(f"Loading frozen edges (parallel workers={workers})…", flush=True)
        all_trades: list[dict[str, Any]] = []
        errors: list[tuple[str, str]] = []
        done = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_worker_regen_symbol, sym): sym for sym in symbols}
            for fut in as_completed(futs):
                sym, trades, err = fut.result()
                done += 1
                if err:
                    errors.append((sym, err))
                    print(f"[{done}/{len(symbols)}] {sym} ERROR: {err}", flush=True)
                else:
                    print(f"[{done}/{len(symbols)}] {sym} signals={len(trades)}", flush=True)
                    all_trades.extend(trades)
        if errors:
            print(f"ERRORS: {errors}", flush=True)
            return 1
        all_trades.sort(key=event_sort_key)
        write_csv(CACHE, all_trades)

    write_csv(OUT / "raw_signals.csv", all_trades)
    print(f"Total raw Tier-A signals: {len(all_trades)}", flush=True)

    # Exact collisions
    exact = build_exact_collision_groups(all_trades)
    write_csv(OUT / "exact_collision_groups.csv", exact)

    involved = set()
    for g in exact:
        for sid in g["signal_ids"].split("|"):
            involved.add(sid)
    size_hist = Counter(g["n_signals"] for g in exact)
    side_mix = Counter(g["side_mix"] for g in exact)
    klass_c = Counter(g["classification"] for g in exact)

    # Triple+
    triples = triple_or_more_cases(exact, all_trades)
    write_csv(OUT / "triple_or_more_collision_cases.csv", triples)
    triple_all_sl = [t for t in triples if t["all_sl"]]

    # Near-time
    near = near_time_stats(all_trades)
    write_csv(OUT / "near_time_collisions.csv", near)
    near_global = []
    for w in NEAR_WINDOWS_MIN:
        subset = [r for r in near if r["window_minutes"] == w]
        sig = sum(r["signals"] for r in subset)
        inv = sum(r["signals_with_near_neighbor"] for r in subset)
        pairs = sum(r["pair_count"] or 0 for r in subset)
        near_global.append(
            {
                "window_minutes": w,
                "total_signals": sig,
                "signals_with_near_neighbor": inv,
                "near_rate_pct": round(100.0 * inv / sig, 2) if sig else None,
                "pair_count": pairs,
            }
        )

    # Multi-TF
    mtf = multi_tf_collisions(all_trades, window_min=15)
    write_csv(OUT / "multi_tf_collisions.csv", mtf)

    # Classification export
    write_csv(
        OUT / "collision_classification.csv",
        [
            {
                "group_key": g["group_key"],
                "symbol": g["symbol"],
                "n_signals": g["n_signals"],
                "classification": g["classification"],
                "side_mix": g["side_mix"],
                "timeframes": g["timeframes"],
                "all_same_outcome": g["all_same_outcome"],
            }
            for g in exact
        ],
    )

    # Execution variants
    raw = list(all_trades)
    same_time = variant_same_time_dedup(raw)
    one_sym = variant_one_active(raw, per_direction=False)
    one_dir = variant_one_active(raw, per_direction=True)

    variants = {
        "RAW_ALL_SIGNALS": raw,
        "ONE_ENTRY_PER_SYMBOL_TIMESTAMP": same_time,
        "ONE_ACTIVE_POSITION_PER_SYMBOL": one_sym,
        "ONE_ACTIVE_POSITION_PER_SYMBOL_DIRECTION": one_dir,
    }

    raw_perf = perf_from_trades(raw)
    variant_rows = []
    for name, trades in variants.items():
        p = perf_from_trades(trades)
        removed = [t for t in raw if t["signal_id"] not in {x["signal_id"] for x in trades}]
        rem_win = sum(1 for t in removed if t.get("result") == "WIN")
        rem_loss = sum(1 for t in removed if t.get("result") == "LOSS")
        rem_sl = sum(1 for t in removed if str(t.get("outcome")) == "SL")
        variant_rows.append(
            {
                "variant": name,
                "signals": p["signals"],
                "closed": p["closed"],
                "open": p["open"],
                "winners": p["winners"],
                "sl": p["sl"],
                "WR": p["WR"],
                "Net": p["Net"],
                "Net_per_trade": p["Net_per_trade"],
                "PF": p["PF"],
                "MaxDD": p["MaxDD"],
                "max_losing_streak": p["max_losing_streak"],
                "removed_trades": len([t for t in removed if t.get("result") in ("WIN", "LOSS")]),
                "removed_winners": rem_win,
                "removed_losers": rem_loss,
                "removed_SL": rem_sl,
                "delta_Net_vs_raw": p["Net"] - raw_perf["Net"],
                "delta_MaxDD_vs_raw": p["MaxDD"] - raw_perf["MaxDD"],
                "delta_SL_vs_raw": p["sl"] - raw_perf["sl"],
                "delta_closed_vs_raw": p["closed"] - raw_perf["closed"],
            }
        )
    write_csv(OUT / "execution_variant_comparison.csv", variant_rows)

    # Coin impact vs same-time dedup
    coin_rows = coin_impact(raw, same_time, exact)
    write_csv(OUT / "coin_collision_impact.csv", coin_rows)

    # Primary decision
    n_exact = len(exact)
    n_involved = len(involved)
    n_3plus = sum(1 for g in exact if g["n_signals"] >= 3)
    n_triple_sl = len(triple_all_sl)
    same_row = next(r for r in variant_rows if r["variant"] == "ONE_ENTRY_PER_SYMBOL_TIMESTAMP")
    one_row = next(r for r in variant_rows if r["variant"] == "ONE_ACTIVE_POSITION_PER_SYMBOL")

    # Heuristics (no tuning): if many exact collisions inflate SL materially
    collision_rate = (100.0 * n_involved / len(raw)) if raw else 0
    sl_infl = raw_perf["sl"] - same_row["sl"]
    active_removes_more = one_row["closed"] < same_row["closed"] * 0.9

    if n_exact == 0 and collision_rate < 1:
        primary = "RAW_SIGNAL_COUNTING_IS_ACCEPTABLE"
        rec = "RAW counting acceptable for this window; collisions negligible."
    elif n_exact > 0 and (sl_infl >= 10 or collision_rate >= 5) and not active_removes_more:
        primary = "SAME_TIME_DEDUP_REQUIRED"
        rec = (
            "Exact same-timestamp multi-signal events inflate trade/SL counts; "
            "use ONE_ENTRY_PER_SYMBOL_TIMESTAMP as realistic baseline semantics."
        )
    elif active_removes_more and (one_row["delta_Net_vs_raw"] is not None):
        # if one-active changes picture more than same-time
        if abs(one_row["closed"] - same_row["closed"]) > max(20, 0.05 * raw_perf["closed"]):
            primary = "ONE_ACTIVE_POSITION_PER_SYMBOL_REQUIRED"
            rec = (
                "Overlapping open positions on the same symbol are common; "
                "ONE_ACTIVE_POSITION_PER_SYMBOL is the realistic execution constraint."
            )
        else:
            primary = "SAME_TIME_DEDUP_REQUIRED"
            rec = "Same-time dedup addresses the main inflation; one-active is secondary."
    else:
        primary = "COLLISION_SEMANTICS_REQUIRE_FURTHER_REVIEW"
        rec = "Collisions exist but impact is mixed; review before choosing backtest semantics."

    # Refine with numbers
    if n_exact >= 20 and n_3plus >= 5 and n_triple_sl >= 3:
        if primary == "RAW_SIGNAL_COUNTING_IS_ACCEPTABLE":
            primary = "SAME_TIME_DEDUP_REQUIRED"

    # Focus coins
    focus_detail = {s: next((c for c in coin_rows if c["symbol"] == s), None) for s in FOCUS}

    meta = {
        "task": "AUDIT_51_COIN_SIGNAL_COLLISIONS_AND_REALISTIC_EXECUTION",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window": {"start": _iso(WINDOW_START), "end_exclusive": _iso(WINDOW_END)},
        "strategy": {
            "GLOBAL_FROZEN_TIER_A": True,
            "entry": "BASELINE_IMMEDIATE",
            "exit": "SL_FIRST",
            "BE50": False,
            "fee": FEE,
            "timeframes": list(SIGNAL_TFS),
        },
        "universe_count": len(symbols),
        "raw_signals": len(raw),
        "exact_collision_groups": n_exact,
        "signals_in_exact_collisions": n_involved,
        "size_histogram": dict(size_hist),
        "side_mix": dict(side_mix),
        "classification_counts": dict(klass_c),
        "triple_or_more_groups": len(triples),
        "triple_or_more_all_sl": n_triple_sl,
        "near_time_global": near_global,
        "multi_tf_collision_groups_15m_window": len(mtf),
        "primary_decision": primary,
        "recommendation": rec,
        "variants": variant_rows,
        "focus": focus_detail,
        "strategy_logic_changed": "NO",
        "db_changed": "NO",
        "dashboard_changed": "NO",
    }
    (OUT / "audit_metadata.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

    # summary.md
    top_coll = sorted(coin_rows, key=lambda r: -(r["collision_rate"] or 0))[:10]
    top_sl = sorted(coin_rows, key=lambda r: -(r["SL_inflation"] or 0))[:10]
    lines = [
        "# AUDIT_51_COIN_SIGNAL_COLLISIONS_AND_REALISTIC_EXECUTION",
        "",
        f"Primary: `{primary}`",
        "",
        f"Window: `{_iso(WINDOW_START)}` → `{_iso(WINDOW_END)}` · TFs: {', '.join(SIGNAL_TFS)}",
        f"Strategy: GLOBAL_FROZEN_TIER_A · BASELINE_IMMEDIATE · SL_FIRST · NO_BE50 · fee={FEE}",
        "",
        f"## Raw Tier-A signals: **{len(raw)}**",
        f"Exact collision groups: **{n_exact}**",
        f"Signals in exact collisions: **{n_involved}** ({collision_rate:.1f}%)",
        f"Size hist: {dict(size_hist)}",
        f"3+ groups: **{n_3plus}** · Triple+ all-SL: **{n_triple_sl}**",
        f"Multi-TF groups (15m near): **{len(mtf)}**",
        "",
        "## Near-time rates (global)",
        "",
    ]
    for ng in near_global:
        lines.append(
            f"- {ng['window_minutes']}m: near_rate={ng['near_rate_pct']}% "
            f"(pairs={ng['pair_count']})"
        )
    lines += ["", "## Execution variants", "", "| Variant | Closed | WR | Net | PF | MaxDD | ΔNet | ΔSL |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in variant_rows:
        pf_s = f"{r['PF']:.3f}" if r["PF"] is not None else "—"
        wr_s = f"{r['WR']:.1f}" if r["WR"] is not None else "—"
        lines.append(
            f"| {r['variant']} | {r['closed']} | {wr_s} | {r['Net']:.1f} | "
            f"{pf_s} | {r['MaxDD']:.1f} | {r['delta_Net_vs_raw']:.1f} | {r['delta_SL_vs_raw']} |"
        )
    lines += ["", "## Top collision_rate", ""]
    for c in top_coll:
        lines.append(f"- {c['symbol']}: rate={c['collision_rate']}% SL_infl={c['SL_inflation']} ΔNet={c['delta_Net']:.1f}")
    lines += ["", "## Recommendation", "", rec, "", "Mutations: NONE · Strategy changed: NO", ""]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"PRIMARY {primary}")
    print(f"raw={len(raw)} exact_groups={n_exact} involved={n_involved} triple_all_sl={n_triple_sl}")
    for r in variant_rows:
        print(
            f"  {r['variant']}: closed={r['closed']} WR={r['WR']} Net={r['Net']:.2f} "
            f"DD={r['MaxDD']:.2f} ΔNet={r['delta_Net_vs_raw']:.2f}"
        )
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
