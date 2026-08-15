"""Load frozen A6-Short and STP B2×E1 signal sources (no entry re-logic)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.c35c_signal_store.path_store import C35cPathStore
from research.regime_scanner.pullback_entry_c3_5c_robustness_audit import (
    assign_split,
    fixed_chrono_splits,
)
from research.regime_scanner.short_trend_pullback.audit import build_15m_frame
from research.regime_scanner.small_target_single_trade.config import (
    A6_OUTCOME_VERSION,
    A6_PARENT_LABEL,
    STRATEGY_A6,
    STRATEGY_STP,
)


def _floor_min(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, utc=True).dt.floor("min")


def load_a6_short_signals(
    store: C35cPathStore,
    *,
    parent_label: str = A6_PARENT_LABEL,
    outcome_version: str = A6_OUTCOME_VERSION,
) -> pd.DataFrame:
    children = store.find_child_runs(parent_label)
    rows: list[dict[str, Any]] = []
    for run in children:
        rid = str(run["run_id"])
        sym = str(run.get("symbol") or "").upper()
        sigs, outcomes, _, _ = store.load_signals_bundle(rid, outcome_version=outcome_version)
        for s in sigs:
            if str(s.get("direction") or "").lower() != "short":
                continue
            meta = s.get("metadata_json") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except json.JSONDecodeError:
                    meta = {}
            oc = outcomes.get(int(s["id"])) or {}
            rows.append(
                {
                    "strategy_source": STRATEGY_A6,
                    "signal_id": int(s["id"]),
                    "signal_key": s.get("signal_key"),
                    "symbol": sym,
                    "side": "short",
                    "trigger_timestamp": s.get("timestamp"),
                    "fill_timestamp": s.get("entry_time"),
                    "entry_price": float(s["entry_price"]),
                    "split": (meta.get("split") if isinstance(meta, dict) else None) or oc.get("split"),
                    "run_id": rid,
                }
            )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["trigger_timestamp"] = pd.to_datetime(df["trigger_timestamp"], utc=True)
    df["fill_timestamp"] = pd.to_datetime(df["fill_timestamp"], utc=True)
    # next-open: fill should be trigger + 15m typically
    return df.sort_values(["symbol", "fill_timestamp"]).reset_index(drop=True)


def load_stp_b2e1_signals(
    results_dir: Path,
    *,
    context: str = "B2",
    trigger: str = "E1",
) -> pd.DataFrame:
    path = Path(results_dir) / "signals_per_trade.csv"
    if not path.is_file():
        raise FileNotFoundError(f"missing STP signals: {path}")
    raw = pd.read_csv(path)
    sub = raw[(raw["context"].astype(str) == context) & (raw["trigger"].astype(str) == trigger)].copy()
    if sub.empty:
        raise RuntimeError(f"no STP rows for {context}×{trigger} in {path}")
    rows = []
    for i, r in sub.iterrows():
        rows.append(
            {
                "strategy_source": STRATEGY_STP,
                "signal_id": f"stp_{r.get('symbol')}_{r.get('fill_bar')}_{i}",
                "signal_key": f"stp|{r.get('symbol')}|{r.get('fill_timestamp')}|{r.get('fill_bar')}",
                "symbol": str(r["symbol"]).upper(),
                "side": "short",
                "trigger_timestamp": r.get("trigger_timestamp"),
                "fill_timestamp": r.get("fill_timestamp"),
                "entry_price": float(r["entry_price"]),
                "split": r.get("split"),
                "fill_bar_csv": r.get("fill_bar"),
                "trigger_bar_csv": r.get("trigger_bar"),
                "context_variant": context,
                "trigger_variant": trigger,
            }
        )
    df = pd.DataFrame(rows)
    df["trigger_timestamp"] = pd.to_datetime(df["trigger_timestamp"], utc=True)
    df["fill_timestamp"] = pd.to_datetime(df["fill_timestamp"], utc=True)
    return df.sort_values(["symbol", "fill_timestamp"]).reset_index(drop=True)


def attach_fill_bars(signals: pd.DataFrame, frame: pd.DataFrame) -> pd.DataFrame:
    """Map fill_timestamp → bar index on prepared 15m frame; verify open≈entry."""
    fts = pd.to_datetime(frame["timestamp"], utc=True)
    out = signals.copy()
    fill_bars = []
    entry_ok = []
    next_open_ok = []
    for _, r in out.iterrows():
        ts = pd.Timestamp(r["fill_timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        matches = np.where(fts == ts)[0]
        if len(matches) == 0:
            deltas = (fts - ts).abs()
            j = int(deltas.argmin())
            ok = bool(deltas.iloc[j] <= pd.Timedelta(minutes=1))
            fill_i = j if ok else -1
        else:
            fill_i = int(matches[0])
        fill_bars.append(fill_i)
        if fill_i < 0:
            entry_ok.append(False)
            next_open_ok.append(False)
            continue
        open_px = float(frame.iloc[fill_i]["open"])
        entry_ok.append(abs(open_px - float(r["entry_price"])) <= max(1e-6, abs(open_px) * 1e-6))
        trig = pd.Timestamp(r["trigger_timestamp"])
        if trig.tzinfo is None:
            trig = trig.tz_localize("UTC")
        else:
            trig = trig.tz_convert("UTC")
        # Next-open: trigger on prior closed bar (or exactly 15m before fill)
        ok_next = False
        if fill_i > 0:
            prev_ts = pd.Timestamp(fts.iloc[fill_i - 1])
            if prev_ts.tzinfo is None:
                prev_ts = prev_ts.tz_localize("UTC")
            ok_next = abs((trig - prev_ts).total_seconds()) <= 90 or abs((ts - trig).total_seconds() - 15 * 60) <= 90
        next_open_ok.append(ok_next)
    out["fill_bar"] = fill_bars
    out["entry_matches_open"] = entry_ok
    out["next_open_semantics_ok"] = next_open_ok
    return out


def ensure_splits(signals: pd.DataFrame, a0: pd.Timestamp, a1: pd.Timestamp) -> pd.DataFrame:
    splits = fixed_chrono_splits(a0, a1)
    out = signals.copy()
    if "split" not in out.columns or out["split"].isna().all():
        out["split"] = [
            {"development": "dev", "validation": "validation", "oos": "oos"}.get(
                assign_split(pd.Timestamp(ts), splits), assign_split(pd.Timestamp(ts), splits)
            )
            for ts in out["fill_timestamp"]
        ]
    else:
        out["split"] = out["split"].map(
            lambda x: {"development": "dev", "validation": "validation", "oos": "oos"}.get(str(x), str(x))
        )
    return out


def parity_report(a6: pd.DataFrame, stp: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for name, df in ((STRATEGY_A6, a6), (STRATEGY_STP, stp)):
        rows.append(
            {
                "strategy_source": name,
                "n_signals": int(len(df)),
                "n_coins": int(df["symbol"].nunique()) if len(df) else 0,
                "side_all_short": bool((df["side"] == "short").all()) if len(df) else False,
                "entry_match_rate": float(np.mean(df["entry_matches_open"])) if "entry_matches_open" in df and len(df) else None,
                "next_open_ok_rate": float(np.mean(df["next_open_semantics_ok"])) if "next_open_semantics_ok" in df and len(df) else None,
                "missing_fill_bar": int((df["fill_bar"] < 0).sum()) if "fill_bar" in df and len(df) else None,
            }
        )
    # per coin counts
    for name, df in ((STRATEGY_A6, a6), (STRATEGY_STP, stp)):
        if df.empty:
            continue
        for sym, n in df.groupby("symbol").size().items():
            rows.append({"strategy_source": name, "symbol": sym, "n_signals": int(n), "row_type": "by_coin"})
    return rows


def load_frames_for_symbols(symbols: list[str]) -> dict[str, tuple[pd.DataFrame, dict[str, Any], pd.Timestamp, pd.Timestamp]]:
    out = {}
    for sym in symbols:
        frame, meta, a0, a1 = build_15m_frame(sym)
        out[sym] = (frame, meta, a0, a1)
    return out
