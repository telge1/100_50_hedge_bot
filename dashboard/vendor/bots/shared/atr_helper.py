from __future__ import annotations

"""
ATR helper for adaptive burn distance.

This module is intentionally **standalone**:
- Uses Bybit's public market/kline endpoint (no auth required)
- Writes a small JSON state file per symbol:
    data/state/atr_burn_<SYMBOL>.json

The burn distance is expressed in percent of price and follows the rule:

    raw_pct = (ATR * 0.8 / price) * 100

    if raw_pct < 0.5%   -> burn_distance_pct = 0.5
    if 0.5%..1.2%       -> burn_distance_pct = raw_pct
    if raw_pct > 1.2%   -> burn_distance_pct = 1.2

Bots can read that JSON and, when enabled, override their long_tp_percentage
with burn_distance_pct for ATR-based burn behaviour.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

import requests

logger = logging.getLogger(__name__)


Timeframe = Literal["1", "3", "5", "15", "30", "60", "120", "240", "360", "720", "D", "M", "W"]


@dataclass
class AtrBurnState:
    symbol: str
    timeframe: Timeframe
    atr: float
    price: float
    burn_distance_pct: float
    updated_at: float
    stale: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "atr": self.atr,
            "price": self.price,
            "burn_distance_pct": self.burn_distance_pct,
            "formula": "burn_pct = clamp(ATR * 0.8 / price * 100, 0.5, 1.2)",
            "updated_at": self.updated_at,
            "stale": self.stale,
        }


def _project_root() -> Path:
    # bots/shared/atr_helper.py -> project root = parent.parent.parent
    return Path(__file__).resolve().parent.parent.parent


def _state_path(symbol: str) -> Path:
    sym = str(symbol or "").strip().upper()
    state_dir = _project_root() / "data" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / f"atr_burn_{sym}.json"


def _fetch_klines(symbol: str, interval: Timeframe, limit: int = 50) -> list[dict]:
    """
    Fetch recent OHLC candles from Bybit (public market API).

    We deliberately do a very small, time-bounded request to avoid
    blocking dashboard/bots for too long.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol must not be empty")

    url = "https://api.bybit.com/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": sym,
        "interval": interval,
        "limit": str(limit),
    }
    resp = requests.get(url, params=params, timeout=5)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit kline error: {data.get('retCode')} {data.get('retMsg')}")
    rows: Sequence[Sequence[str]] = (data.get("result") or {}).get("list") or []
    # Bybit returns newest first; we want chronological order
    klines = list(reversed(rows))
    out: list[dict] = []
    for row in klines:
        # [0]=startTime(ms), 1=open,2=high,3=low,4=close,...
        try:
            out.append(
                {
                    "ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                }
            )
        except (IndexError, ValueError, TypeError):
            continue
    if not out:
        raise RuntimeError("No valid kline data returned from Bybit")
    return out


def _compute_atr(candles: Sequence[dict], period: int = 14) -> float:
    """
    Compute classic ATR over the last `period` candles.
    """
    if len(candles) <= period:
        raise ValueError(f"Not enough candles for ATR({period}) – got {len(candles)}")

    trs: list[float] = []
    prev_close = float(candles[0]["close"])
    for c in candles[1:]:
        high = float(c["high"])
        low = float(c["low"])
        close = float(c["close"])
        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        trs.append(tr)
        prev_close = close

    if len(trs) < period:
        raise ValueError(f"Not enough TR samples for ATR({period}) – got {len(trs)}")

    last_trs = trs[-period:]
    return sum(last_trs) / float(period)


def _clamp_burn_distance_pct(atr: float, price: float) -> float:
    """
    Map ATR + price to burn distance in percent using the agreed rule.
    """
    if price <= 0 or atr <= 0:
        raise ValueError(f"Invalid ATR/price combination: atr={atr}, price={price}")
    raw_pct = (atr * 0.8 / price) * 100.0
    if raw_pct < 0.5:
        return 0.5
    if raw_pct > 1.2:
        return 1.2
    return raw_pct


def compute_distance_pct(
    *,
    atr: float,
    price: float,
    multiplier: float,
    min_pct: float,
    max_pct: float | None,
) -> float:
    """
    Generic helper: converts ATR + price into a distance in percent.

        raw_pct = (ATR * multiplier / price) * 100
        pct = clamp(raw_pct, min_pct, max_pct)
    """
    if price <= 0 or atr <= 0:
        raise ValueError(f"Invalid ATR/price combination: atr={atr}, price={price}")
    mult = float(multiplier)
    if mult <= 0:
        raise ValueError(f"multiplier must be > 0 (got {multiplier})")
    mn = float(min_pct)
    mx = None if max_pct is None else float(max_pct)
    if mn <= 0:
        raise ValueError(f"Invalid clamp min_pct={min_pct}")
    if mx is not None and (mx <= 0 or mx < mn):
        raise ValueError(f"Invalid clamp range: min_pct={min_pct}, max_pct={max_pct}")
    raw_pct = (atr * mult / price) * 100.0
    if raw_pct < mn:
        return mn
    if mx is not None and raw_pct > mx:
        return mx
    return raw_pct


def compute_distance_pct_from_atr_state(
    state: AtrBurnState,
    *,
    multiplier: float,
    min_pct: float,
    max_pct: float | None,
) -> float:
    """
    Convenience wrapper for bots/dashboard: compute a distance pct from a persisted ATR state.
    """
    if state is None:
        raise ValueError("state must not be None")
    return compute_distance_pct(
        atr=float(state.atr),
        price=float(state.price),
        multiplier=multiplier,
        min_pct=min_pct,
        max_pct=max_pct,
    )


def update_atr_burn_state(symbol: str, timeframe: Timeframe = "5") -> AtrBurnState:
    """
    Fetch recent candles for `symbol`, compute ATR and burn_distance_pct,
    and persist it to data/state/atr_burn_<SYMBOL>.json.

    This is designed to be fast and safe to call synchronously from the
    dashboard before starting bots.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("symbol must not be empty")

    try:
        candles = _fetch_klines(sym, timeframe, limit=50)
        atr = _compute_atr(candles, period=14)
        last_close = float(candles[-1]["close"])
        burn_pct = _clamp_burn_distance_pct(atr, last_close)
        state = AtrBurnState(
            symbol=sym,
            timeframe=timeframe,
            atr=atr,
            price=last_close,
            burn_distance_pct=burn_pct,
            updated_at=time.time(),
            stale=False,
        )
        path = _state_path(sym)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, indent=2, sort_keys=True)
        except Exception as exc:
            logger.warning("Could not write ATR-burn state to %s: %s", path, exc)
        logger.info(
            "[ATR-BURN] Updated state for %s (%s): ATR=%.6f price=%.6f burn_pct=%.4f",
            sym,
            timeframe,
            atr,
            last_close,
            burn_pct,
        )
        return state
    except Exception as exc:
        logger.error("[ATR-BURN] Failed to update state for %s: %s", sym, exc, exc_info=True)
        # Try to mark existing state as stale, if present
        path = _state_path(sym)
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
            else:
                data = {}
            data.setdefault("symbol", sym)
            data["stale"] = True
            data["updated_at"] = time.time()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
        except Exception:
            pass
        raise


def load_atr_burn_state(symbol: str) -> AtrBurnState | None:
    """
    Helper for bots to read the latest ATR-burn state, if any.
    """
    path = _state_path(symbol)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        return AtrBurnState(
            symbol=str(data.get("symbol") or symbol or "").strip().upper(),
            timeframe=str(data.get("timeframe") or "5"),
            atr=float(data.get("atr", 0.0) or 0.0),
            price=float(data.get("price", 0.0) or 0.0),
            burn_distance_pct=float(data.get("burn_distance_pct", 0.0) or 0.0),
            updated_at=float(data.get("updated_at", 0.0) or 0.0),
            stale=bool(data.get("stale", False)),
        )
    except Exception as exc:
        logger.warning("Could not read ATR-burn state for %s: %s", symbol, exc)
        return None

