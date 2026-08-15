"""Phase A/B: important C3.4B structure breaks on historical OB days."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.candle_sources import FeatherCandleSource
from research.regime_scanner.timeframes import aggregate_candles, ensure_utc_timestamp
from research.regime_scanner.trend_direction_at import (
    map_major_to_direction,
    run_c34b_on_ohlcv,
)

OB_DAYS: dict[str, tuple[str, ...]] = {
    "APTUSDT": (
        "2025-12-29",
        "2025-12-30",
        "2026-01-06",
        "2026-01-18",
        "2026-05-12",
        "2026-05-23",
    ),
    "DOGEUSDT": (
        "2026-01-06",
        "2026-01-15",
        "2026-02-20",
        "2026-02-28",
    ),
}

# Existing Freqtrade HTF feathers (read-only). Not via FeatherCandleSource —
# that source only allows 5m/15m/30m; we reuse the same C3.4B applicator.
HTF_FEATHER_ROOT = Path(
    "/home/telgenbuescher/projects/Signal_Generator_Ralf/data_htf_candle_staging/futures"
)
HTF_MINUTES = {"1h": 60, "4h": 240}

WARMUP_DAYS = 14
CLUSTER_MINUTES = 45
LEVEL_TOL_BPS = 15.0


def _iso(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = ensure_utc_timestamp(pd.Timestamp(ts))
    return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _day(ts: pd.Timestamp) -> str:
    return ensure_utc_timestamp(ts).strftime("%Y-%m-%d")


def load_symbol_5m(symbol: str) -> pd.DataFrame:
    src = FeatherCandleSource()
    df = src.load_candles(exchange="bybit", symbol=symbol, timeframe="5m", closed_only=True)
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def load_symbol_htf(symbol: str, timeframe: str) -> pd.DataFrame:
    """Load existing 1h/4h futures feathers (same C3.4B inputs as scanner stack)."""
    from research.backtests.candle_loader import symbol_to_feather_name

    if timeframe not in HTF_MINUTES:
        raise ValueError(f"unsupported HTF timeframe: {timeframe!r}")
    path = HTF_FEATHER_ROOT / symbol_to_feather_name(symbol, timeframe=timeframe)
    if not path.is_file():
        raise FileNotFoundError(path)
    import pyarrow.feather as feather

    raw = feather.read_table(path).to_pandas()
    if "date" in raw.columns and "timestamp" not in raw.columns:
        raw = raw.rename(columns={"date": "timestamp"})
    need = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [c for c in need if c not in raw.columns]
    if missing:
        raise ValueError(f"{path} missing columns: {missing}")
    df = raw[need].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def _apply_c34b_tf(ohlcv: pd.DataFrame, *, timeframe: str, minutes: int) -> pd.DataFrame:
    from research.regime_scanner.pullback_entry_c3_5 import enrich_indicators
    from research.regime_scanner.market_structure_c3_4b import (
        RESEARCH_MATRIX,
        ProtectedStructureConfig,
        apply_protected_structure,
    )

    need = ["timestamp", "open", "high", "low", "close", "volume"]
    feat = enrich_indicators(ohlcv[need].copy())
    cfg = ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])
    struct = apply_protected_structure(feat, cfg).copy()
    struct["candle_open_ts"] = pd.to_datetime(struct["timestamp"], utc=True)
    struct["candle_close_ts"] = struct["candle_open_ts"] + pd.Timedelta(minutes=minutes)
    struct["available_at"] = struct["candle_close_ts"]
    struct["timeframe"] = timeframe
    return struct


def run_structure_tf(ohlcv_5m: pd.DataFrame, timeframe: str, *, symbol: str | None = None) -> pd.DataFrame:
    """C3.4B protected_medium on 5m or existing HTF feathers (1h/4h)."""
    if timeframe == "5m":
        struct = run_c34b_on_ohlcv(ohlcv_5m[["timestamp", "open", "high", "low", "close", "volume"]])
        struct = struct.copy()
        struct["timeframe"] = "5m"
        return struct
    if timeframe in HTF_MINUTES:
        if symbol is None:
            raise ValueError("symbol required for HTF feather load")
        htf = load_symbol_htf(symbol, timeframe)
        if ohlcv_5m is not None and not ohlcv_5m.empty:
            t0 = ensure_utc_timestamp(ohlcv_5m["timestamp"].iloc[0]) - pd.Timedelta(days=5)
            t1 = ensure_utc_timestamp(ohlcv_5m["timestamp"].iloc[-1]) + pd.Timedelta(days=1)
            htf = htf[(htf["timestamp"] >= t0) & (htf["timestamp"] <= t1)].copy()
        if htf.empty:
            return pd.DataFrame()
        return _apply_c34b_tf(htf, timeframe=timeframe, minutes=HTF_MINUTES[timeframe])
    # Optional mid-TFs via existing causal aggregator (15m/30m only in scanner module)
    end = ensure_utc_timestamp(ohlcv_5m["timestamp"].iloc[-1]) + pd.Timedelta(minutes=5)
    htf = aggregate_candles(ohlcv_5m, timeframe, end)
    if htf is None or htf.empty:
        return pd.DataFrame()
    minutes = {"15m": 15, "30m": 30}[timeframe]
    return _apply_c34b_tf(htf, timeframe=timeframe, minutes=minutes)


def _rising(series: pd.Series) -> pd.Series:
    b = series.fillna(False).astype(bool)
    return b & ~b.shift(1, fill_value=False)


def extract_raw_events(struct: pd.DataFrame, *, symbol: str, timeframe: str, ob_days: set[str]) -> list[dict[str, Any]]:
    if struct.empty:
        return []
    df = struct.copy()
    df["timeframe"] = timeframe
    events: list[dict[str, Any]] = []

    # Rising edges for important flags
    df["_ext_up"] = _rising(df["external_bos_up"]) if "external_bos_up" in df else False
    df["_ext_down"] = _rising(df["external_bos_down"]) if "external_bos_down" in df else False
    df["_cb_up"] = _rising(df["close_break_protected_up"]) if "close_break_protected_up" in df else False
    df["_cb_down"] = _rising(df["close_break_protected_down"]) if "close_break_protected_down" in df else False

    # CHOCH: first bar where choch_side becomes up/down OR state enters *_choch
    prev_choch = df["choch_side"].astype(str).str.lower().shift(1)
    choch = df["choch_side"].astype(str).str.lower()
    df["_choch_up"] = (choch == "up") & (prev_choch != "up")
    df["_choch_down"] = (choch == "down") & (prev_choch != "down")
    prev_state = df["protected_structure_state"].astype(str).shift(1)
    state = df["protected_structure_state"].astype(str)
    df["_state_bull_choch"] = (state == "bullish_choch") & (prev_state != "bullish_choch")
    df["_state_bear_choch"] = (state == "bearish_choch") & (prev_state != "bearish_choch")

    for _, row in df.iterrows():
        avail = ensure_utc_timestamp(row["available_at"])
        day = _day(avail)
        if day not in ob_days:
            continue
        candle_open = ensure_utc_timestamp(row["candle_open_ts"])
        candle_close = ensure_utc_timestamp(row["candle_close_ts"])
        pl = float(row["protected_low"]) if pd.notna(row.get("protected_low")) else None
        ph = float(row["protected_high"]) if pd.notna(row.get("protected_high")) else None
        # level before break for close breaks: use protected_*_before if present
        pl_before = float(row["protected_low_before"]) if pd.notna(row.get("protected_low_before")) else pl
        ph_before = float(row["protected_high_before"]) if pd.notna(row.get("protected_high_before")) else ph

        candidates: list[tuple[str, str, float | None, str]] = []
        # (structure_type, direction, level, importance_reason)
        if bool(row.get("_state_bear_choch")) or bool(row.get("_choch_down")):
            lvl = pl_before if pl_before is not None else pl
            candidates.append(("CHOCH", "bearish", lvl, f"{timeframe} bearish CHOCH / protected-low challenge"))
        if bool(row.get("_state_bull_choch")) or bool(row.get("_choch_up")):
            lvl = ph_before if ph_before is not None else ph
            candidates.append(("CHOCH", "bullish", lvl, f"{timeframe} bullish CHOCH / protected-high challenge"))
        if bool(row.get("_ext_down")):
            lvl = pl_before if pl_before is not None else (float(row["active_external_break_level"]) if pd.notna(row.get("active_external_break_level")) else pl)
            candidates.append(("EXTERNAL_BOS", "bearish", lvl, f"{timeframe} external BOS down"))
        if bool(row.get("_ext_up")):
            lvl = ph_before if ph_before is not None else (float(row["active_external_break_level"]) if pd.notna(row.get("active_external_break_level")) else ph)
            candidates.append(("EXTERNAL_BOS", "bullish", lvl, f"{timeframe} external BOS up"))
        if bool(row.get("_cb_down")) and pl_before is not None:
            candidates.append(("PROTECTED_LOW_BREAK", "bearish", pl_before, f"{timeframe} close break of protected low"))
        if bool(row.get("_cb_up")) and ph_before is not None:
            candidates.append(("PROTECTED_HIGH_BREAK", "bullish", ph_before, f"{timeframe} close break of protected high"))

        for structure_type, direction, level, reason in candidates:
            if level is None:
                continue
            events.append(
                {
                    "symbol": symbol,
                    "date": day,
                    "direction": direction,
                    "structure_type": structure_type,
                    "timeframe": timeframe,
                    "level": float(level),
                    "candle_open": _iso(candle_open),
                    "candle_close": _iso(candle_close),
                    "available_at": _iso(avail),
                    "first_touch_ts": None,  # filled from OB later
                    "first_break_ts": None,  # market break from OB; scanner known at available_at
                    "confirmation_ts": _iso(avail),
                    "reclaim_ts": None,
                    "importance_reason": reason,
                    "historical_ob_available": True,
                    "status": "CANDIDATE",
                    "scanner_state": str(row.get("protected_structure_state")),
                    "major_direction": map_major_to_direction(row.get("major_direction")),
                    "protected_low": pl,
                    "protected_high": ph,
                    "close": float(row["close"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                }
            )
    return events


def cluster_events(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge same-market breaks within CLUSTER_MINUTES / LEVEL_TOL_BPS."""
    if not raw:
        return []
    rows = sorted(raw, key=lambda r: (r["symbol"], r["available_at"], r["timeframe"]))
    clusters: list[list[dict[str, Any]]] = []
    for ev in rows:
        placed = False
        t = pd.Timestamp(ev["available_at"])
        for cl in clusters:
            rep = cl[0]
            if rep["symbol"] != ev["symbol"] or rep["direction"] != ev["direction"]:
                continue
            if abs(float(rep["level"]) - float(ev["level"])) / float(rep["level"]) * 1e4 > LEVEL_TOL_BPS:
                continue
            t0 = pd.Timestamp(rep["available_at"])
            if abs((t - t0).total_seconds()) > CLUSTER_MINUTES * 60:
                continue
            cl.append(ev)
            placed = True
            break
        if not placed:
            clusters.append([ev])

    out: list[dict[str, Any]] = []
    for i, cl in enumerate(clusters, 1):
        # Prefer HTF member as representative; else earliest available_at
        rank_tf = {"4h": 0, "1h": 1, "30m": 2, "15m": 3, "5m": 4}
        cl_sorted = sorted(cl, key=lambda r: (rank_tf.get(r["timeframe"], 9), r["available_at"]))
        rep = dict(cl_sorted[0])
        types = sorted({m["structure_type"] for m in cl})
        tfs = sorted({m["timeframe"] for m in cl}, key=lambda x: rank_tf.get(x, 9))
        # Prefer protected break type in label if present
        if "PROTECTED_LOW_BREAK" in types:
            primary_type = "PROTECTED_LOW_BREAK"
        elif "PROTECTED_HIGH_BREAK" in types:
            primary_type = "PROTECTED_HIGH_BREAK"
        elif "CHOCH" in types:
            primary_type = "CHOCH"
        else:
            primary_type = types[0]
        eid = (
            f"{rep['symbol']}_{primary_type}_{rep['direction']}_"
            f"{rep['date'].replace('-', '')}_{float(rep['level']):.6g}_{tfs[0]}"
        )
        eid = eid.replace(".", "p")
        rep["event_id"] = eid
        rep["structure_type"] = primary_type
        rep["structure_types_clustered"] = "|".join(types)
        rep["timeframes_clustered"] = "|".join(tfs)
        rep["timeframe"] = tfs[0]  # highest TF
        rep["cluster_size"] = len(cl)
        rep["member_available_ats"] = "|".join(sorted({m["available_at"] for m in cl}))
        # earliest scanner available among members
        rep["available_at"] = min(m["available_at"] for m in cl)
        rep["candle_open"] = min(m["candle_open"] for m in cl)
        rep["candle_close"] = min(m["candle_close"] for m in cl)
        reasons = sorted({m["importance_reason"] for m in cl})
        rep["importance_reason"] = "; ".join(reasons)
        # scanner first_break known time = available_at (causal)
        rep["scanner_break_known_at"] = rep["available_at"]
        out.append(rep)
    return out


def build_inventory() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (clustered events, raw events)."""
    raw_all: list[dict[str, Any]] = []
    for symbol, days in OB_DAYS.items():
        ob_days = set(days)
        day_ts = [pd.Timestamp(d, tz="UTC") for d in days]
        load_start = min(day_ts) - pd.Timedelta(days=WARMUP_DAYS)
        load_end = max(day_ts) + pd.Timedelta(days=1)
        ohlcv = load_symbol_5m(symbol)
        ohlcv = ohlcv[(ohlcv["timestamp"] >= load_start) & (ohlcv["timestamp"] < load_end)].copy()
        for tf in ("5m", "1h", "4h"):
            struct = run_structure_tf(ohlcv, tf, symbol=symbol)
            raw_all.extend(extract_raw_events(struct, symbol=symbol, timeframe=tf, ob_days=ob_days))
    clustered = cluster_events(raw_all)
    return clustered, raw_all


def prioritize_events(events: list[dict[str, Any]], *, max_n: int = 15) -> list[dict[str, Any]]:
    """Score and select deep-dive events (8–15 if available)."""

    def window_covered(e: dict[str, Any]) -> bool:
        days = set(OB_DAYS.get(e["symbol"], ()))
        open_day = str(e["candle_open"])[:10]
        avail_day = str(e.get("date") or e["available_at"])[:10]
        return open_day in days and avail_day in days

    def score(e: dict[str, Any]) -> tuple:
        tf_score = {"4h": 100, "1h": 80, "30m": 50, "15m": 40, "5m": 20}.get(e["timeframe"], 10)
        type_score = {
            "PROTECTED_LOW_BREAK": 50,
            "PROTECTED_HIGH_BREAK": 50,
            "CHOCH": 40,
            "EXTERNAL_BOS": 35,
        }.get(e["structure_type"], 10)
        htf_bonus = 25 if "4h" in (e.get("timeframes_clustered") or "") or "1h" in (e.get("timeframes_clustered") or "") else 0
        cluster_bonus = min(15, int(e.get("cluster_size", 1)) * 3)
        cover_bonus = 40 if window_covered(e) else -80  # avoid midnight HTF bars needing prior-day OB
        return (tf_score + type_score + htf_bonus + cluster_bonus + cover_bonus, e["available_at"])

    ranked = sorted(events, key=score, reverse=True)
    # diversify: try both symbols, both directions
    selected: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    def add(e: dict[str, Any]) -> None:
        key = f"{e['symbol']}|{e['direction']}|{e['date']}|{round(float(e['level']), 6)}"
        if key in seen_keys:
            return
        seen_keys.add(key)
        ee = dict(e)
        ee["selection_rank"] = len(selected) + 1
        ee["status"] = "SELECTED"
        ee["ob_window_covered"] = window_covered(e)
        selected.append(ee)

    # Prefer fully covered HTF / protected breaks
    for e in ranked:
        if len(selected) >= max_n:
            break
        if not window_covered(e):
            continue
        if e["timeframe"] in {"4h", "1h"} or e["structure_type"].startswith("PROTECTED"):
            add(e)
    # Fill diversity gaps (covered only)
    for direction in ("bearish", "bullish"):
        for symbol in ("APTUSDT", "DOGEUSDT"):
            if any(s["direction"] == direction and s["symbol"] == symbol for s in selected):
                continue
            for e in ranked:
                if window_covered(e) and e["direction"] == direction and e["symbol"] == symbol:
                    add(e)
                    break
    # Fill remaining by score (covered first, then uncovered if needed)
    for e in ranked:
        if len(selected) >= max_n:
            break
        if window_covered(e):
            add(e)
    if len(selected) < min(8, len(events)):
        for e in ranked:
            if len(selected) >= max_n:
                break
            add(e)

    return selected[:max_n]
