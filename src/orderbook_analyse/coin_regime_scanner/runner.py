"""Orchestrate COIN_REGIME_SCANNER_V1 snapshot build."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.orderbook_v2_live.universe import FORBIDDEN_SYMBOLS, SYMBOLS_51

from .classify import build_coin_regime
from .config import DEFAULT_WARMUP_HOURS, MARKET_ANCHOR, MIN_WARMUP_HOURS, SCANNER_VERSION
from .features import close_return, last_row_features, merge_frame, slice_to_asof
from . import loaders as ch


def universe_symbols() -> list[str]:
    syms = [s for s in SYMBOLS_51 if s not in FORBIDDEN_SYMBOLS]
    if "XAUUSDT" in syms:
        syms = [s for s in syms if s != "XAUUSDT"]
    return list(syms)


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _symbol_frames(
    candles: pd.DataFrame,
    trades: pd.DataFrame,
    ob: pd.DataFrame,
    symbol: str,
    as_of: pd.Timestamp,
) -> tuple[pd.DataFrame, bool, bool]:
    c = candles.loc[candles["symbol"] == symbol] if not candles.empty else pd.DataFrame()
    c = slice_to_asof(c, "open_time", as_of)
    t = trades.loc[trades["symbol"] == symbol] if not trades.empty else pd.DataFrame()
    t = slice_to_asof(t, "minute", as_of) if not t.empty else t
    o = ob.loc[ob["symbol"] == symbol] if not ob.empty else pd.DataFrame()
    o = slice_to_asof(o, "minute", as_of) if not o.empty else o
    if c.empty:
        return pd.DataFrame(), False, False
    merged = merge_frame(c, t if not t.empty else None, o if not o.empty else None)
    # ret_5m
    closes = merged["close"].to_numpy(dtype=float)
    merged.attrs["ret_5m"] = close_return(closes, 5)
    return merged, (not t.empty), (not o.empty)


def _features_from_merged(merged: pd.DataFrame) -> dict[str, Any]:
    feat = last_row_features(merged)
    feat["ret_5m"] = float(merged.attrs.get("ret_5m", float("nan")))
    return feat


def run_scanner(
    *,
    as_of: datetime | None = None,
    warmup_hours: int = DEFAULT_WARMUP_HOURS,
    output_dir: Path | str,
    write_csv: bool = True,
    client: Any | None = None,
    symbols: list[str] | None = None,
) -> dict[str, Any]:
    if warmup_hours < MIN_WARMUP_HOURS:
        raise ValueError(f"warmup_hours must be >= {MIN_WARMUP_HOURS}")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    syms = symbols or universe_symbols()
    own = client is None
    client = client or ch.get_client()
    try:
        resolved = ch.resolve_as_of(client, syms, as_of)
        start = ch.warmup_start(resolved, warmup_hours)
        end_excl = ch.end_exclusive(resolved)

        fetch_syms = list(dict.fromkeys([*syms, MARKET_ANCHOR]))
        candles = ch.fetch_candles_1m_batch(client, fetch_syms, start, resolved)
        try:
            trades = ch.fetch_trades_1m_batch(client, fetch_syms, start, end_excl)
            trades_err = None
        except Exception as exc:  # noqa: BLE001
            trades = pd.DataFrame()
            trades_err = f"{type(exc).__name__}:{exc}"
        try:
            ob = ch.fetch_orderbook_1m_batch(client, fetch_syms, start, end_excl)
            ob_err = None
        except Exception as exc:  # noqa: BLE001
            ob = pd.DataFrame()
            ob_err = f"{type(exc).__name__}:{exc}"

        as_of_ts = pd.Timestamp(ch.to_naive_utc(resolved))
        as_of_iso = _iso(resolved)

        btc_merged, btc_tr, btc_ob = _symbol_frames(candles, trades, ob, MARKET_ANCHOR, as_of_ts)
        btc_feat = _features_from_merged(btc_merged) if not btc_merged.empty else None

        coins: list[dict[str, Any]] = []
        for sym in syms:
            merged, tr_ok, ob_ok = _symbol_frames(candles, trades, ob, sym, as_of_ts)
            if merged.empty or len(merged) < 60:
                coins.append(
                    build_coin_regime(
                        symbol=sym,
                        as_of=as_of_iso,
                        feat={},
                        btc_feat=btc_feat,
                        candles_ok=False,
                        ob_available=False,
                        trades_available=False,
                        missing_reason="insufficient_candles",
                    )
                )
                continue
            feat = _features_from_merged(merged)
            coins.append(
                build_coin_regime(
                    symbol=sym,
                    as_of=as_of_iso,
                    feat=feat,
                    btc_feat=btc_feat,
                    candles_ok=True,
                    ob_available=ob_ok and bool(feat.get("ob_ok")),
                    trades_available=tr_ok,
                    missing_reason=None,
                )
            )

        summary = _summarize(coins)
        payload = {
            "scanner_version": SCANNER_VERSION,
            "as_of": as_of_iso,
            "warmup_hours": warmup_hours,
            "warmup_start": _iso(start),
            "universe": "SYMBOLS_51",
            "n_symbols": len(syms),
            "market_anchor": MARKET_ANCHOR,
            "btc_available": btc_feat is not None,
            "load_errors": {"trades": trades_err, "orderbook": ob_err},
            "summary": summary,
            "coins": coins,
        }

        json_path = out_dir / "current_regime_snapshot.json"
        json_path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        if write_csv:
            csv_path = out_dir / "current_regime_snapshot.csv"
            _write_csv(csv_path, coins)
            payload["csv_path"] = str(csv_path)
        payload["json_path"] = str(json_path)
        return payload
    finally:
        if own and client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass


def _summarize(coins: list[dict[str, Any]]) -> dict[str, Any]:
    def counts(key: str) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in coins:
            v = str(c.get(key))
            out[v] = out.get(v, 0) + 1
        return dict(sorted(out.items()))

    gate_counts: dict[str, dict[str, int]] = {}
    for gname in ("range60_breakout_ob", "trend_flag_breakout", "absorption_reclaim"):
        gc: dict[str, int] = {}
        for c in coins:
            st = c.get("strategy_gates", {}).get(gname, {}).get("state", "missing")
            gc[st] = gc.get(st, 0) + 1
        gate_counts[gname] = dict(sorted(gc.items()))

    return {
        "candles_ok": sum(1 for c in coins if c.get("data_quality", {}).get("candles_ok")),
        "ob_ok": sum(1 for c in coins if c.get("data_quality", {}).get("ob_ok")),
        "vol_regime": counts("vol_regime"),
        "trend_regime": counts("trend_regime"),
        "range_regime": counts("range_regime"),
        "breakout_readiness": counts("breakout_readiness"),
        "strategy_gates": gate_counts,
    }


def _write_csv(path: Path, coins: list[dict[str, Any]]) -> None:
    fields = [
        "symbol",
        "as_of",
        "candles_ok",
        "ob_ok",
        "trades_ok",
        "vol_regime",
        "trend_regime",
        "range_regime",
        "momentum_regime",
        "market_alignment",
        "ob_regime",
        "breakout_readiness",
        "gate_range60",
        "gate_trend_flag",
        "gate_absorption",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for c in coins:
            dq = c.get("data_quality") or {}
            g = c.get("strategy_gates") or {}
            w.writerow(
                {
                    "symbol": c.get("symbol"),
                    "as_of": c.get("as_of"),
                    "candles_ok": dq.get("candles_ok"),
                    "ob_ok": dq.get("ob_ok"),
                    "trades_ok": dq.get("trades_ok"),
                    "vol_regime": c.get("vol_regime"),
                    "trend_regime": c.get("trend_regime"),
                    "range_regime": c.get("range_regime"),
                    "momentum_regime": c.get("momentum_regime"),
                    "market_alignment": c.get("market_alignment"),
                    "ob_regime": c.get("ob_regime"),
                    "breakout_readiness": c.get("breakout_readiness"),
                    "gate_range60": (g.get("range60_breakout_ob") or {}).get("state"),
                    "gate_trend_flag": (g.get("trend_flag_breakout") or {}).get("state"),
                    "gate_absorption": (g.get("absorption_reclaim") or {}).get("state"),
                }
            )
