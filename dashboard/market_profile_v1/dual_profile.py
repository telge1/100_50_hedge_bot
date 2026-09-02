"""Dual TPO (bracket presence) + Volume-at-price profiles for market_profile_v1.

Each anchored window returns separate ``tpo`` and ``volume`` blocks. There is
no shared bin array and no OA volume-at-price relabeled as TPO.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DASH = ROOT / "dashboard"
for p in (str(ROOT), str(DASH)):
    if p not in sys.path:
        sys.path.insert(0, p)

from research_charts.oa_import import ensure_oa_on_path  # noqa: E402

DUAL_CONTRACT_VERSION = "market_profile_v1_dual_tpo_volume_v1"
TPO_CONTRACT = "tpo_profile_facts_v1"
VOLUME_CONTRACT = "volume_profile_facts_v1"


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _dt_sql(dt: datetime) -> str:
    return _utc(dt).strftime("%Y-%m-%d %H:%M:%S")


def load_window_trades(
    client: Any,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    use_final: bool,
) -> list[dict[str, Any]]:
    """Read-only trades for ``[start, end)`` with optional FINAL dedupe."""
    start = _utc(start)
    end = _utc(end)
    table = "orderbook_analysis.public_trades_canonical"
    final = " FINAL" if use_final else ""
    rows = client.query(
        f"""
        SELECT trade_ts, trade_id, side, toFloat64(price), toFloat64(size), toFloat64(notional)
        FROM {table}{final}
        WHERE symbol={{sym:String}}
          AND trade_ts >= toDateTime64({{a:String}}, 3, 'UTC')
          AND trade_ts < toDateTime64({{b:String}}, 3, 'UTC')
        ORDER BY trade_ts, trade_id
        """,
        parameters={"sym": symbol, "a": _dt_sql(start), "b": _dt_sql(end)},
    ).result_rows
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        tid = str(r[1])
        if tid in seen:
            continue
        seen.add(tid)
        out.append(
            {
                "ts": _utc(r[0]),
                "trade_id": tid,
                "side": str(r[2]),
                "price": float(r[3]),
                "size": float(r[4]),
                "notional": float(r[5]),
            }
        )
    return out


def _tpo_bins_for_ui(tpo: dict[str, Any]) -> list[dict[str, Any]]:
    step = float((tpo.get("provenance") or {}).get("price_increment") or 10.0)
    out: list[dict[str, Any]] = []
    for row in tpo.get("rows") or []:
        idx = int(row["price_bin_index"])
        lo = idx * step
        hi = lo + step
        cnt = float(row.get("tpo_count") or 0)
        if cnt <= 0:
            continue
        out.append(
            {
                "bin_index": idx,
                "price_low": lo,
                "price_high": hi,
                "price_mid": float(row.get("price") or (lo + step / 2.0)),
                "tpo_count": cnt,
            }
        )
    return out


def _volume_bins_for_ui(vol: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in vol.get("rows") or []:
        base = float(row.get("base_volume") or 0)
        if base <= 0 and int(row.get("trade_count") or 0) <= 0:
            continue
        lo = float(row.get("price_bin_low") or 0)
        hi = float(row.get("price_bin_high") or lo)
        buy = float(row.get("taker_buy_base_volume") or 0)
        sell = float(row.get("taker_sell_base_volume") or 0)
        out.append(
            {
                "bin_index": int(row.get("price_bin_index") or 0),
                "price_low": lo,
                "price_high": hi,
                "price_mid": float(row.get("display_price") or (lo + hi) / 2.0),
                "base_volume": base,
                "buy_volume": buy,
                "sell_volume": sell,
                "delta": float(row.get("delta_base_volume") or (buy - sell)),
                "trades": int(row.get("trade_count") or 0),
            }
        )
    return out


def _ohlc_from_trades(trades: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    if not trades:
        return None
    prices = [float(t["price"]) for t in trades]
    return (
        float(trades[0]["price"]),
        max(prices),
        min(prices),
        float(trades[-1]["price"]),
    )


def _classify_shape_from_volume(
    client: Any,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    vol: dict[str, Any],
    *,
    target_bins: int,
    value_area_pct: float,
    thresholds: Any,
    ohlc: tuple[float, float, float, float] | None = None,
) -> Any:
    ensure_oa_on_path()
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.loader import fetch_window_ohlc
    from orderbook_analyse.market_profile.profile import compute_value_area, find_nodes
    from orderbook_analyse.market_profile.shape import classify_shape

    if ohlc is None:
        ohlc = fetch_window_ohlc(client, symbol, window_start, window_end)
    if ohlc is None:
        return None
    open_price, high, low, close_price = ohlc
    step = float((vol.get("provenance") or {}).get("price_increment") or 10.0)
    bins: list[ProfileBin] = []
    for row in vol.get("rows") or []:
        idx = int(row.get("price_bin_index") or 0)
        lo = float(row.get("price_bin_low") or idx * step)
        hi = float(row.get("price_bin_high") or lo + step)
        mid = float(row.get("display_price") or (lo + hi) / 2.0)
        base = float(row.get("base_volume") or 0)
        buy = float(row.get("taker_buy_base_volume") or 0)
        sell = float(row.get("taker_sell_base_volume") or 0)
        bins.append(
            ProfileBin(
                bin_index=idx,
                price_low=lo,
                price_high=hi,
                price_mid=mid,
                volume=base,
                buy_volume=buy,
                sell_volume=sell,
                trades=int(row.get("trade_count") or 0),
                notional=float(row.get("quote_notional") or 0),
            )
        )
    if not bins:
        return None
    bins.sort(key=lambda b: b.bin_index)
    value_area = compute_value_area(bins, value_area_pct)
    nodes = find_nodes(
        bins,
        hvn_factor=thresholds.hvn_factor,
        lvn_factor=thresholds.lvn_factor,
        min_separation_bins=thresholds.node_min_separation_bins,
        single_print_frac=thresholds.single_print_frac,
        poc_volume=value_area.poc_volume,
    )
    total_volume = sum(b.volume for b in bins)
    return classify_shape(
        value_area=value_area,
        nodes=nodes,
        price_low=low,
        price_high=high,
        open_price=open_price,
        close_price=close_price,
        total_volume=total_volume,
        bin_count=len(bins),
        bins=bins,
        thresholds=thresholds,
    )


def _naked_poc_flag(
    poc_price: float | None,
    window_end: datetime,
    candles_1m,
) -> bool | None:
    if poc_price is None or candles_1m is None or candles_1m.empty:
        return None
    import pandas as pd

    df = candles_1m.sort_values("open_time")
    end = pd.Timestamp(window_end)
    if end.tzinfo is not None:
        end = end.tz_convert("UTC").tz_localize(None)
    times = df["open_time"].to_numpy()
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    after = times >= end.to_datetime64()
    hit = after & (lows <= poc_price) & (highs >= poc_price)
    return not bool(hit.any())


def build_dual_window_profile(
    client: Any,
    symbol: str,
    window: Any,
    *,
    value_area_pct: float,
    target_bins: int,
    use_final: bool,
    thresholds: Any,
    candles_1m=None,
    include_bins: bool = True,
    trades: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Build one window payload with separate TPO and Volume profiles."""
    from research.btc_ob_fight.tpo_profile import build_tpo_profile_from_trades
    from research.btc_ob_fight.volume_profile import (
        build_volume_profile_from_trades,
        dedupe_session_trades,
    )

    if trades is None:
        trades = load_window_trades(
            client, symbol, window.start, window.end, use_final=use_final
        )
    if not trades:
        return None

    session_start = _utc(window.start)
    anchor = _utc(window.end)
    session_trades, session_cov = dedupe_session_trades(trades, session_start, anchor)
    window_ohlc = _ohlc_from_trades(session_trades)

    tpo = build_tpo_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=anchor,
        cl=client,
        symbol=symbol,
        value_area_pct=value_area_pct,
        target_bins=target_bins,
        profile_session_id=window.window_id,
        session_trades=session_trades,
        coverage_meta=session_cov,
        ohlc=window_ohlc,
    )
    vol = build_volume_profile_from_trades(
        trades,
        session_start=session_start,
        anchor=anchor,
        cl=client,
        symbol=symbol,
        value_area_pct=value_area_pct,
        target_bins=target_bins,
        profile_session_id=window.window_id,
        session_trades=session_trades,
        coverage_meta=session_cov,
        compute_prefix=False,
        ohlc=window_ohlc,
    )

    tpo_ok = tpo.get("tpo_profile_status") == "COMPUTED_SEPARATELY"
    vol_ok = vol.get("volume_profile_status") == "COMPUTED_SEPARATELY"
    if not tpo_ok and not vol_ok:
        return None

    shape_verdict = None
    if vol_ok:
        shape_verdict = _classify_shape_from_volume(
            client,
            symbol,
            session_start,
            anchor,
            vol,
            target_bins=target_bins,
            value_area_pct=value_area_pct,
            thresholds=thresholds,
            ohlc=window_ohlc,
        )
    if shape_verdict is None:
        ensure_oa_on_path()
        from orderbook_analyse.market_profile.contracts import ShapeVerdict

        shape_verdict = ShapeVerdict(
            kind="UNCLEAR",
            letter="?",
            poc_position=0.5,
            va_range_share=0.0,
            poc_concentration=0.0,
            directional_share=0.0,
            reasons=("insufficient_volume_profile_for_shape",),
        )

    tpoc = (tpo.get("tpoc") or {}) if tpo_ok else {}
    tva = (tpo.get("value_area") or {}) if tpo_ok else {}
    vpoc = (vol.get("vpoc") or {}) if vol_ok else {}
    vva = (vol.get("value_area") or {}) if vol_ok else {}

    poc_for_naked = tpoc.get("tpoc_price") if tpo_ok else vpoc.get("vpoc_price")
    naked = _naked_poc_flag(poc_for_naked, anchor, candles_1m)

    open_p = close_p = None
    price_low = price_high = None
    if window_ohlc:
        open_p, price_high, price_low, close_p = window_ohlc
    elif session_trades:
        prices = [t["price"] for t in session_trades]
        price_low, price_high = min(prices), max(prices)
        open_p, close_p = session_trades[0]["price"], session_trades[-1]["price"]

    tpo_nodes = {
        "hvn": [n["price"] for n in (tpo.get("hvn_candidates") or [])],
        "lvn": [n["price"] for n in (tpo.get("lvn_candidates") or [])],
        "single_print_ranges": [],
    }
    vol_nodes = {
        "hvn": [n["price"] for n in (vol.get("hvn_candidates") or [])],
        "lvn": [n["price"] for n in (vol.get("lvn_candidates") or [])],
        "single_print_ranges": [],
    }

    payload: dict[str, Any] = {
        "symbol": symbol,
        "window": window.to_dict(),
        "price_step": float((tpo.get("provenance") or {}).get("price_increment")
                            or (vol.get("provenance") or {}).get("price_increment")
                            or 10.0),
        "price_low": price_low,
        "price_high": price_high,
        "open_price": open_p,
        "close_price": close_p,
        "shape": shape_verdict.to_dict(),
        "naked_poc": naked,
        "dual_contract_version": DUAL_CONTRACT_VERSION,
        "tpo": {
            "contract_version": TPO_CONTRACT,
            "status": tpo.get("tpo_profile_status"),
            "provenance": tpo.get("provenance") or {},
            "value_area": {
                "poc": tpoc.get("tpoc_price"),
                "vah": tva.get("tpoc_vah"),
                "val": tva.get("tpoc_val"),
                "volume_share": tva.get("actual_value_area_share"),
            },
            "brackets": tpo.get("brackets") or {},
            "nodes": tpo_nodes,
        },
        "volume": {
            "contract_version": VOLUME_CONTRACT,
            "status": vol.get("volume_profile_status"),
            "provenance": vol.get("provenance") or {},
            "value_area": {
                "poc": vpoc.get("vpoc_price"),
                "vah": vva.get("vvah"),
                "val": vva.get("vval"),
                "volume_share": vva.get("actual_value_area_share"),
            },
            "nodes": vol_nodes,
        },
    }
    if include_bins:
        payload["tpo"]["bins"] = _tpo_bins_for_ui(tpo) if tpo_ok else []
        payload["volume"]["bins"] = _volume_bins_for_ui(vol) if vol_ok else []
    return payload
