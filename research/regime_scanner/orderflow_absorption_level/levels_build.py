"""Causal level inventory: protected (C3.4B) + external swing pivots."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import RegimeScannerConfig
from research.regime_scanner.market_structure_c3_4b import (
    RESEARCH_MATRIX,
    ProtectedRuntime,
    ProtectedStructureConfig,
    step_protected_structure_state,
)
from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig
from research.regime_scanner.swings import find_confirmed_pivots


def _ts_str(v: Any) -> str:
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return ""
    ts = pd.Timestamp(v)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def _level_id(
    *,
    symbol: str,
    sequence_id: Any,
    level_type: str,
    side: str,
    confirmation_index: int,
    level_price: float,
) -> str:
    raw = (
        f"{symbol}|{sequence_id}|{level_type}|{side}|"
        f"{confirmation_index}|{level_price:.10g}"
    )
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _cfg_protected(variant: str) -> ProtectedStructureConfig:
    for entry in RESEARCH_MATRIX:
        if entry["name"] == variant:
            return ProtectedStructureConfig.from_matrix_entry(entry)
    return ProtectedStructureConfig.from_matrix_entry(RESEARCH_MATRIX[0])


def _prepare_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    out = df.reset_index(drop=True).copy()
    if "timestamp" not in out.columns:
        out["timestamp"] = out["bucket_start"]
    if "bar_index" not in out.columns:
        out["bar_index"] = np.arange(len(out), dtype=int)
    return out


def build_external_swing_levels(
    seq_df: pd.DataFrame,
    *,
    symbol: str,
    sequence_id: Any,
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    """Build swing levels with close-break invalidation (no future rewrite)."""
    if seq_df.empty or "external_swing" not in cfg.level_types:
        return []
    candles = seq_df.copy()
    if "timestamp" not in candles.columns:
        candles["timestamp"] = candles["bucket_start"]
    pivot_cfg = RegimeScannerConfig(pivot_left=cfg.pivot_left, pivot_right=cfg.pivot_right)
    pivots = find_confirmed_pivots(
        candles,
        config=pivot_cfg,
        pivot_left=cfg.pivot_left,
        pivot_right=cfg.pivot_right,
    )
    closes = pd.to_numeric(candles["close"], errors="coerce").to_numpy(dtype=float)
    n = len(candles)
    rows: list[dict[str, Any]] = []
    for p in pivots:
        side = "resistance" if p.pivot_type == "high" else "support"
        conf_i = int(p.confirmation_index)
        price = float(p.price)
        inv_i: int | None = None
        inv_reason: str | None = None
        for j in range(conf_i + 1, n):
            c = closes[j]
            if not np.isfinite(c):
                continue
            if side == "support" and c < price:
                inv_i, inv_reason = j, "close_break"
                break
            if side == "resistance" and c > price:
                inv_i, inv_reason = j, "close_break"
                break
        lid = _level_id(
            symbol=symbol,
            sequence_id=sequence_id,
            level_type="external_swing",
            side=side,
            confirmation_index=conf_i,
            level_price=price,
        )
        rows.append(
            {
                "level_id": lid,
                "symbol": symbol,
                "sequence_id": sequence_id,
                "level_type": "external_swing",
                "side": side,
                "level_price": price,
                "extreme_index": int(p.pivot_index),
                "confirmation_index": conf_i,
                "extreme_timestamp": str(p.pivot_timestamp),
                "confirmation_timestamp": str(p.confirmation_timestamp),
                "active_from": conf_i + 1,  # first bar where confirmation_index < t
                "invalidated_at": inv_i,
                "invalidation_reason": inv_reason,
                "source_function": "find_confirmed_pivots",
                "repaint_safe": True,
            }
        )
    return rows


def build_protected_levels(
    seq_df: pd.DataFrame,
    *,
    symbol: str,
    sequence_id: Any,
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    """Replay C3.4B; emit protected high/low when newly confirmed."""
    if seq_df.empty or "protected" not in cfg.level_types:
        return []
    frame = _prepare_ohlc(seq_df)
    if "atr_14" not in frame.columns:
        # Caller should enrich; fallback simple TR mean if missing.
        prev = frame["close"].astype(float).shift(1)
        tr = pd.concat(
            [
                (frame["high"].astype(float) - frame["low"].astype(float)).abs(),
                (frame["high"].astype(float) - prev).abs(),
                (frame["low"].astype(float) - prev).abs(),
            ],
            axis=1,
        ).max(axis=1)
        frame["atr_14"] = tr.rolling(14, min_periods=1).mean()

    pcfg = _cfg_protected(cfg.protected_variant)
    rt = ProtectedRuntime()
    prev_state = "structure_unknown"
    highs = frame["high"].astype(float).tolist()
    lows = frame["low"].astype(float).tolist()

    open_levels: dict[str, dict[str, Any]] = {}  # key high|low -> inventory row in progress
    finished: list[dict[str, Any]] = []

    def _close_open(side_key: str, inv_i: int, reason: str) -> None:
        row = open_levels.pop(side_key, None)
        if row is None:
            return
        row["invalidated_at"] = inv_i
        row["invalidation_reason"] = reason
        finished.append(row)

    for i in range(len(frame)):
        src = frame.iloc[i].to_dict()
        prepared = {
            **src,
            "bar_index": int(src.get("bar_index", i)),
            "highs_window": highs[: i + 1],
            "lows_window": lows[: i + 1],
            "indicator_clean_regime_state": "neutral",
            "timestamp": src.get("timestamp") or src.get("bucket_start"),
        }
        new_state, rt, _diag = step_protected_structure_state(
            prev_state, rt, prepared, None, pcfg
        )

        for side_key, pl in (("high", rt.protected_high), ("low", rt.protected_low)):
            if pl is None:
                if side_key in open_levels:
                    _close_open(side_key, i, "structure_clear")
                continue
            side = "resistance" if side_key == "high" else "support"
            conf_i = int(pl.confirmed_bar)
            price = float(pl.level)
            cur = open_levels.get(side_key)
            identity = (conf_i, round(price, 10), int(pl.extreme_bar))
            if cur is not None:
                cur_id = (
                    int(cur["confirmation_index"]),
                    round(float(cur["level_price"]), 10),
                    int(cur["extreme_index"]),
                )
                if cur_id != identity:
                    _close_open(side_key, i, "replaced")
                    cur = None
            if cur is None:
                lid = _level_id(
                    symbol=symbol,
                    sequence_id=sequence_id,
                    level_type="protected",
                    side=side,
                    confirmation_index=conf_i,
                    level_price=price,
                )
                open_levels[side_key] = {
                    "level_id": lid,
                    "symbol": symbol,
                    "sequence_id": sequence_id,
                    "level_type": "protected",
                    "side": side,
                    "level_price": price,
                    "extreme_index": int(pl.extreme_bar),
                    "confirmation_index": conf_i,
                    "extreme_timestamp": _ts_str(pl.extreme_timestamp),
                    "confirmation_timestamp": _ts_str(pl.confirmed_timestamp),
                    "active_from": conf_i + 1,
                    "invalidated_at": None,
                    "invalidation_reason": None,
                    "source_function": "step_protected_structure_state",
                    "repaint_safe": True,
                }

        # Close-break invalidation while still tracking (extra clarity for inventory)
        close = float(src["close"]) if np.isfinite(float(src["close"])) else float("nan")
        if np.isfinite(close):
            ph = open_levels.get("high")
            if ph is not None and close > float(ph["level_price"]):
                # C3.4B may keep level until BOS; inventory marks first close beyond
                if ph.get("_close_break_marked") is not True:
                    ph["_close_break_marked"] = True
                    ph["_first_close_break"] = i
            pl_ = open_levels.get("low")
            if pl_ is not None and close < float(pl_["level_price"]):
                if pl_.get("_close_break_marked") is not True:
                    pl_["_close_break_marked"] = True
                    pl_["_first_close_break"] = i

        prev_state = new_state

    for side_key in list(open_levels.keys()):
        row = open_levels.pop(side_key)
        # Prefer structure clear/replace; else leave open (None) or note close break
        if row.pop("_close_break_marked", False) and row.get("invalidated_at") is None:
            # Keep active until structure clears; record diagnostic only via reason if ended
            pass
        row.pop("_first_close_break", None)
        finished.append(row)

    return finished


def build_level_inventory(
    df: pd.DataFrame,
    *,
    symbol: str,
    cfg: LevelAbsorptionConfig,
) -> list[dict[str, Any]]:
    """Build full inventory for one symbol frame (resets on sequence_id change)."""
    if df.empty:
        return []
    frame = df.reset_index(drop=True).copy()
    if "sequence_id" not in frame.columns:
        frame["sequence_id"] = 0
    rows: list[dict[str, Any]] = []
    for seq_id, g in frame.groupby("sequence_id", sort=True):
        g = g.sort_values("bucket_start")
        global_idx = g.index.to_numpy()
        local = g.reset_index(drop=True).copy()
        local["bar_index"] = np.arange(len(local), dtype=int)
        swings = build_external_swing_levels(
            local, symbol=symbol, sequence_id=seq_id, cfg=cfg
        )
        protected = build_protected_levels(
            local, symbol=symbol, sequence_id=seq_id, cfg=cfg
        )
        for r in swings + protected:
            for key in ("extreme_index", "confirmation_index", "active_from", "invalidated_at"):
                v = r.get(key)
                if v is None:
                    continue
                vi = int(v)
                if 0 <= vi < len(global_idx):
                    r[key] = int(global_idx[vi])
                elif key == "active_from" and vi >= len(global_idx):
                    r[key] = int(global_idx[-1]) + 1
            rows.append(r)
    rows.sort(
        key=lambda r: (
            str(r["level_type"]),
            str(r["side"]),
            int(r["confirmation_index"]),
            str(r["level_id"]),
        )
    )
    return rows


def level_visible_at(level: dict[str, Any], anchor_index: int) -> bool:
    """Strict causality: confirmation_index < anchor_index; not yet invalidated."""
    conf = int(level["confirmation_index"])
    if not (conf < int(anchor_index)):
        return False
    inv = level.get("invalidated_at")
    if inv is not None and int(inv) <= int(anchor_index):
        return False
    return True


def active_levels_at(
    inventory: list[dict[str, Any]],
    anchor_index: int,
    *,
    sequence_id: Any | None = None,
) -> list[dict[str, Any]]:
    out = []
    for lv in inventory:
        if sequence_id is not None and lv.get("sequence_id") != sequence_id:
            continue
        if level_visible_at(lv, anchor_index):
            out.append(lv)
    return out
