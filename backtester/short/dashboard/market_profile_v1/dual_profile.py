"""Dual TPO (bracket presence) + Volume-at-price profiles for market_profile_v1.

Each anchored window returns separate ``tpo`` and ``volume`` blocks. There is
no shared bin array and no OA volume-at-price relabeled as TPO.

Default path aggregates in ClickHouse (server-side volume bins + TPO bracket
high/low). Pulling raw ticks into Python is reserved for explicit ``trades=``
(tests / parity checks) — a BTCUSDT day is ~2.5M trades and dominated first load.
"""

from __future__ import annotations

import math
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
DEFAULT_BRACKET_MINUTES = 30
TRADES_FQN = "orderbook_analysis.public_trades_canonical"
QSET = {"max_execution_time": 300, "receive_timeout": 320}


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
    """Read-only trades for ``[start, end)`` with optional FINAL dedupe.

    Slow path — prefer :func:`build_dual_window_profile` without ``trades=``,
    which aggregates in ClickHouse instead of shipping ticks.
    """
    start = _utc(start)
    end = _utc(end)
    final = " FINAL" if use_final else ""
    rows = client.query(
        f"""
        SELECT trade_ts, trade_id, side, toFloat64(price), toFloat64(size), toFloat64(notional)
        FROM {TRADES_FQN}{final}
        WHERE symbol={{sym:String}}
          AND trade_ts >= toDateTime64({{a:String}}, 3, 'UTC')
          AND trade_ts < toDateTime64({{b:String}}, 3, 'UTC')
        ORDER BY trade_ts, trade_id
        """,
        parameters={"sym": symbol, "a": _dt_sql(start), "b": _dt_sql(end)},
        settings=QSET,
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


def _fetch_tpo_bracket_ranges(
    client: Any,
    symbol: str,
    session_start: datetime,
    anchor: datetime,
    *,
    bracket_minutes: int,
    use_final: bool,
) -> list[tuple[int, float, float, int]]:
    """One row per TPO bracket: (index, price_low, price_high, trade_count)."""
    session_start = _utc(session_start)
    anchor = _utc(anchor)
    period_s = int(bracket_minutes) * 60
    if period_s <= 0:
        raise ValueError("bracket_minutes must be positive")
    final = " FINAL" if use_final else ""
    s0 = int(session_start.timestamp())
    rows = client.query(
        f"""
        SELECT
          toInt64(floor((toUnixTimestamp64Milli(trade_ts) / 1000 - {{s0:Int64}}) / {{period:Int64}})) AS br,
          min(toFloat64(price)) AS lo,
          max(toFloat64(price)) AS hi,
          toInt64(count()) AS n
        FROM {TRADES_FQN}{final}
        PREWHERE symbol = {{sym:String}}
        WHERE trade_ts >= toDateTime64({{a:String}}, 3, 'UTC')
          AND trade_ts < toDateTime64({{b:String}}, 3, 'UTC')
        GROUP BY br
        ORDER BY br
        """,
        parameters={
            "sym": symbol,
            "a": _dt_sql(session_start),
            "b": _dt_sql(anchor),
            "s0": s0,
            "period": period_s,
        },
        settings=QSET,
    ).result_rows
    out: list[tuple[int, float, float, int]] = []
    for br, lo, hi, n in rows:
        if lo is None or hi is None:
            continue
        out.append((int(br), float(lo), float(hi), int(n or 0)))
    return out


def _build_tpo_from_bracket_ranges(
    bracket_ranges: list[tuple[int, float, float, int]],
    *,
    session_start: datetime,
    anchor: datetime,
    step: float,
    value_area_pct: float,
    target_bins: int,
    profile_session_id: str,
    bracket_minutes: int,
) -> dict[str, Any]:
    """Bracket-presence TPO from CH high/low per bracket (no raw ticks)."""
    ensure_oa_on_path()
    from orderbook_analyse.market_profile import (
        DEFAULT_HVN_FACTOR,
        DEFAULT_LVN_FACTOR,
        DEFAULT_NODE_MIN_SEPARATION_BINS,
        DEFAULT_SINGLE_PRINT_FRAC,
    )
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.loader import densify_bins
    from orderbook_analyse.market_profile.profile import compute_value_area, find_nodes

    bin_tpo_counts: dict[int, int] = {}
    full_count = 0
    partial_count = 0
    period_s = bracket_minutes * 60
    session_epoch = int(_utc(session_start).timestamp())
    anchor_epoch = int(_utc(anchor).timestamp())

    for br, lo, hi, n in bracket_ranges:
        if n <= 0 or not math.isfinite(lo) or not math.isfinite(hi):
            continue
        br_end = session_epoch + (br + 1) * period_s
        if br_end > anchor_epoch:
            partial_count += 1
        else:
            full_count += 1
        i0 = int(math.floor(lo / step))
        i1 = int(math.floor(hi / step))
        if i1 < i0:
            i0, i1 = i1, i0
        for idx in range(i0, i1 + 1):
            bin_tpo_counts[idx] = bin_tpo_counts.get(idx, 0) + 1

    if not bin_tpo_counts:
        return {
            "tpo_profile_status": "FAILED",
            "provenance": {"price_increment": step, "engine": "clickhouse_bracket_agg"},
            "tpoc": {},
            "value_area": {},
            "rows": [],
            "brackets": {"bracket_minutes": bracket_minutes, "total_count": 0},
            "hvn_candidates": [],
            "lvn_candidates": [],
        }

    raw_bins = [
        ProfileBin(
            bin_index=idx,
            price_low=idx * step,
            price_high=idx * step + step,
            price_mid=idx * step + step / 2.0,
            volume=float(cnt),
            buy_volume=0.0,
            sell_volume=0.0,
            trades=0,
            notional=0.0,
        )
        for idx, cnt in sorted(bin_tpo_counts.items())
    ]
    bins = densify_bins(raw_bins, step)
    value_area = compute_value_area(bins, value_area_pct)
    total_marks = sum(bin_tpo_counts.values())
    nodes = find_nodes(
        bins,
        hvn_factor=DEFAULT_HVN_FACTOR,
        lvn_factor=DEFAULT_LVN_FACTOR,
        min_separation_bins=DEFAULT_NODE_MIN_SEPARATION_BINS,
        single_print_frac=DEFAULT_SINGLE_PRINT_FRAC,
        poc_volume=value_area.poc_volume,
    )
    rows = [
        {
            "price_bin_index": b.bin_index,
            "price": b.price_mid,
            "tpo_count": float(bin_tpo_counts.get(b.bin_index, 0)),
        }
        for b in bins
        if bin_tpo_counts.get(b.bin_index, 0) > 0
    ]
    return {
        "tpo_profile_status": "COMPUTED_SEPARATELY",
        "provenance": {
            "price_increment": step,
            "engine": "clickhouse_bracket_agg",
            "bracket_minutes": bracket_minutes,
            "profile_session_id": profile_session_id,
            "target_bins": target_bins,
            "value_area_percentage": value_area_pct,
            "oa_volume_path_not_used_for_tpo": True,
        },
        "tpoc": {
            "tpoc_price": value_area.poc,
            "tpoc_tpo_count": value_area.poc_volume,
            "tpoc_bin_index": value_area.poc_bin_index,
        },
        "value_area": {
            "tpoc_vah": value_area.vah,
            "tpoc_val": value_area.val,
            "actual_value_area_share": value_area.volume_share,
            "value_area_percentage": value_area_pct,
            "total_tpo_marks": total_marks,
        },
        "brackets": {
            "bracket_minutes": bracket_minutes,
            "full_count": full_count,
            "partial_count": partial_count,
            "total_count": full_count + partial_count,
            "total_tpo_marks": total_marks,
        },
        "rows": rows,
        "hvn_candidates": [{"price": float(p)} for p in nodes.hvn],
        "lvn_candidates": [{"price": float(p)} for p in nodes.lvn],
    }


def _build_volume_from_ch(
    client: Any,
    symbol: str,
    session_start: datetime,
    anchor: datetime,
    *,
    step: float,
    value_area_pct: float,
    use_final: bool,
    thresholds: Any,
) -> dict[str, Any]:
    """Volume-at-price via OA ClickHouse aggregation (no raw ticks)."""
    ensure_oa_on_path()
    from orderbook_analyse.market_profile.loader import densify_bins, fetch_volume_at_price
    from orderbook_analyse.market_profile.profile import compute_value_area, find_nodes

    raw = fetch_volume_at_price(
        client, symbol, session_start, anchor, step, use_final=use_final
    )
    if not raw:
        return {
            "volume_profile_status": "FAILED",
            "provenance": {"price_increment": step, "engine": "clickhouse_volume_agg"},
            "vpoc": {},
            "value_area": {},
            "rows": [],
            "hvn_candidates": [],
            "lvn_candidates": [],
        }
    bins = densify_bins(raw, step)
    value_area = compute_value_area(bins, value_area_pct)
    nodes = find_nodes(
        bins,
        hvn_factor=thresholds.hvn_factor,
        lvn_factor=thresholds.lvn_factor,
        min_separation_bins=thresholds.node_min_separation_bins,
        single_print_frac=thresholds.single_print_frac,
        poc_volume=value_area.poc_volume,
    )
    rows = [
        {
            "price_bin_index": b.bin_index,
            "price_bin_low": b.price_low,
            "price_bin_high": b.price_high,
            "display_price": b.price_mid,
            "base_volume": b.volume,
            "taker_buy_base_volume": b.buy_volume,
            "taker_sell_base_volume": b.sell_volume,
            "delta_base_volume": b.buy_volume - b.sell_volume,
            "trade_count": b.trades,
            "quote_notional": b.notional,
        }
        for b in bins
        if b.volume > 0 or b.trades > 0
    ]
    return {
        "volume_profile_status": "COMPUTED_SEPARATELY",
        "provenance": {
            "price_increment": step,
            "engine": "clickhouse_volume_agg",
            "primary_volume_basis": "base_volume",
        },
        "vpoc": {
            "vpoc_price": value_area.poc,
            "vpoc_volume": value_area.poc_volume,
            "vpoc_bin_index": value_area.poc_bin_index,
        },
        "value_area": {
            "vvah": value_area.vah,
            "vval": value_area.val,
            "actual_value_area_share": value_area.volume_share,
            "value_area_percentage": value_area_pct,
        },
        "rows": rows,
        "hvn_candidates": [{"price": float(p)} for p in nodes.hvn],
        "lvn_candidates": [{"price": float(p)} for p in nodes.lvn],
    }


def _build_dual_from_ch(
    client: Any,
    symbol: str,
    window: Any,
    *,
    value_area_pct: float,
    target_bins: int,
    use_final: bool,
    thresholds: Any,
    candles_1m,
    include_bins: bool,
    bracket_minutes: int = DEFAULT_BRACKET_MINUTES,
) -> dict[str, Any] | None:
    ensure_oa_on_path()
    from orderbook_analyse.market_profile.loader import fetch_window_ohlc, resolve_price_step

    session_start = _utc(window.start)
    anchor = _utc(window.end)
    ohlc = fetch_window_ohlc(client, symbol, session_start, anchor)
    if ohlc is None:
        return None
    open_p, high, low, close_p = ohlc
    if high <= low:
        return None
    step = float(resolve_price_step(low, high, target_bins))

    bracket_ranges = _fetch_tpo_bracket_ranges(
        client,
        symbol,
        session_start,
        anchor,
        bracket_minutes=bracket_minutes,
        use_final=use_final,
    )
    tpo = _build_tpo_from_bracket_ranges(
        bracket_ranges,
        session_start=session_start,
        anchor=anchor,
        step=step,
        value_area_pct=value_area_pct,
        target_bins=target_bins,
        profile_session_id=window.window_id,
        bracket_minutes=bracket_minutes,
    )
    vol = _build_volume_from_ch(
        client,
        symbol,
        session_start,
        anchor,
        step=step,
        value_area_pct=value_area_pct,
        use_final=use_final,
        thresholds=thresholds,
    )
    return _assemble_dual_payload(
        symbol=symbol,
        window=window,
        tpo=tpo,
        vol=vol,
        open_p=open_p,
        high=high,
        low=low,
        close_p=close_p,
        step=step,
        client=client,
        session_start=session_start,
        anchor=anchor,
        target_bins=target_bins,
        value_area_pct=value_area_pct,
        thresholds=thresholds,
        candles_1m=candles_1m,
        include_bins=include_bins,
        ohlc=ohlc,
    )


def _assemble_dual_payload(
    *,
    symbol: str,
    window: Any,
    tpo: dict[str, Any],
    vol: dict[str, Any],
    open_p: float | None,
    high: float | None,
    low: float | None,
    close_p: float | None,
    step: float,
    client: Any,
    session_start: datetime,
    anchor: datetime,
    target_bins: int,
    value_area_pct: float,
    thresholds: Any,
    candles_1m,
    include_bins: bool,
    ohlc: tuple[float, float, float, float] | None,
) -> dict[str, Any] | None:
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
            ohlc=ohlc,
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

    price_low = low
    price_high = high

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
        "price_step": float(
            (tpo.get("provenance") or {}).get("price_increment")
            or (vol.get("provenance") or {}).get("price_increment")
            or step
            or 10.0
        ),
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
    """Build one window payload with separate TPO and Volume profiles.

    Default: ClickHouse aggregation (fast). Pass ``trades=`` to force the
    legacy tick path (tests / parity).
    """
    if trades is None:
        return _build_dual_from_ch(
            client,
            symbol,
            window,
            value_area_pct=value_area_pct,
            target_bins=target_bins,
            use_final=use_final,
            thresholds=thresholds,
            candles_1m=candles_1m,
            include_bins=include_bins,
        )

    from research.btc_ob_fight.tpo_profile import build_tpo_profile_from_trades
    from research.btc_ob_fight.volume_profile import (
        build_volume_profile_from_trades,
        dedupe_session_trades,
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

    open_p = close_p = None
    price_low = price_high = None
    if window_ohlc:
        open_p, price_high, price_low, close_p = window_ohlc
    elif session_trades:
        prices = [t["price"] for t in session_trades]
        price_low, price_high = min(prices), max(prices)
        open_p, close_p = session_trades[0]["price"], session_trades[-1]["price"]

    step = float(
        (tpo.get("provenance") or {}).get("price_increment")
        or (vol.get("provenance") or {}).get("price_increment")
        or 10.0
    )
    return _assemble_dual_payload(
        symbol=symbol,
        window=window,
        tpo=tpo,
        vol=vol,
        open_p=open_p,
        high=price_high,
        low=price_low,
        close_p=close_p,
        step=step,
        client=client,
        session_start=session_start,
        anchor=anchor,
        target_bins=target_bins,
        value_area_pct=value_area_pct,
        thresholds=thresholds,
        candles_1m=candles_1m,
        include_bins=include_bins,
        ohlc=window_ohlc,
    )
