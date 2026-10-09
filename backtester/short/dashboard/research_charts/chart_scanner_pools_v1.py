"""Read-only Live Chart adapter: 15m scanner pools from COMMON causal source."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .frozen_pool_kernel_v1 import FrozenPoolKernelError, resolve_decision_bar_index
from .lld_research_kernel import candle_stamps
from .short_pool_oracle_v1 import parse_decision_iso, rows_hash

SCANNER_COMMON_NAMESPACE = "SCANNER_COMMON"
SCANNER_POOL_LAYER_NAME = "Scanner Pools (COMMON)"
SCANNER_ENTRY_TIMEFRAME = "15m"


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _parse_decision_close(decision_close: datetime | str) -> datetime:
    if isinstance(decision_close, str):
        return _ensure_utc(parse_decision_iso(decision_close))
    return _ensure_utc(decision_close)


def decision_close_on_pane(
    stamps: list[datetime],
    instant: datetime,
    timeframe: str,
) -> datetime:
    """Map GO-TO instant or bar close to the causal scanner decision (bar close)."""
    from pool_scan.clock import bar_close

    instant = _ensure_utc(instant)
    try:
        idx = resolve_decision_bar_index(stamps, instant, timeframe)
        return bar_close(stamps[idx], timeframe)
    except FrozenPoolKernelError:
        pass
    chosen: datetime | None = None
    for open_ts in stamps:
        close = bar_close(open_ts, timeframe)
        if close <= instant:
            chosen = close
    if chosen is None:
        raise FrozenPoolKernelError(
            f"no {timeframe} bar close at or before {instant.isoformat()} on pane"
        )
    return chosen


def _serialize_pool_row(
    row: dict,
    *,
    timeframe: str,
    selected_ids: set[str],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "pool_id": row["pool_id"],
        "side": row["side"],
        "lower": float(row["bottom"]),
        "upper": float(row["top"]),
        "source_timeframe": timeframe,
        "available": True,
        "selected": row["pool_id"] in selected_ids,
    }
    known = row.get("known")
    if isinstance(known, datetime):
        out["known_at"] = known.isoformat()
    source = row.get("source")
    if isinstance(source, datetime):
        out["created_at"] = source.isoformat()
    break_at = row.get("break_at")
    if isinstance(break_at, datetime):
        out["break_at"] = break_at.isoformat()
    return out


def _dt_to_unix(ts: datetime | str | None) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, str):
        ts = parse_decision_iso(ts)
    return int(_ensure_utc(ts).timestamp())


def scanner_common_pool_overlays(
    pools: list[dict[str, Any]],
    *,
    decision_close_unix: int,
    render_end_unix: int | None = None,
) -> list[dict[str, Any]]:
    """Chart rectangles for COMMON scanner pools (separate namespace from workspace LLD)."""
    end_t = int(render_end_unix if render_end_unix is not None else decision_close_unix)
    if end_t > decision_close_unix:
        end_t = decision_close_unix
    overlays: list[dict[str, Any]] = []
    for row in pools:
        pool_id = str(row.get("pool_id") or "")
        if not pool_id:
            continue
        lower = float(row["lower"])
        upper = float(row["upper"])
        side = str(row.get("side") or "")
        known_unix = _dt_to_unix(row.get("known_at"))
        start_t = known_unix if known_unix is not None else decision_close_unix
        if start_t > end_t:
            start_t = end_t
        is_lower = side == "lower"
        color = "#1e88e5" if is_lower else "#fb8c00"
        fill = "#1e88e533" if is_lower else "#fb8c0033"
        direction = "support" if is_lower else "resistance"
        overlays.append(
            {
                "id": f"scanner-common:{pool_id}",
                "namespace": SCANNER_COMMON_NAMESPACE,
                "type": "rectangle",
                "points": [
                    {"time": start_t, "price": lower},
                    {"time": end_t, "price": upper},
                ],
                "style": {
                    "color": color,
                    "fillColor": fill,
                    "lineWidth": 2,
                },
                "metadata": {
                    "source": "scanner_common",
                    "direction": direction,
                    "status": "active",
                    "pool_id": pool_id,
                    "decision_close_unix": decision_close_unix,
                },
            }
        )
    return overlays


def chart_scanner_pool_snapshot(
    symbol: str,
    decision_close: datetime | str,
    *,
    timeframe: str = SCANNER_ENTRY_TIMEFRAME,
    render_end_unix: int | None = None,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Thin read-only wrapper over ``bot.common_causal_pools_v1`` (no local pool logic)."""
    from bot.common_causal_pools_v1 import common_causal_pools_from_loaded_pane
    from bot.e1r_live_scanner.config import ensure_runtime_paths

    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import HISTORY_WEEKS, PANE_FROM_UTC
    from dashboard.research_charts.lld_research_kernel import load_pane_candles
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or SCANNER_ENTRY_TIMEFRAME).strip()
    instant = _parse_decision_close(decision_close)

    probe_unix = int(instant.timestamp())
    _packed_probe, probe_candles = load_pane_candles(
        sym,
        tf,
        from_unix=int(PANE_FROM_UTC.timestamp()),
        to_unix=probe_unix,
        history_weeks=HISTORY_WEEKS,
    )
    probe_stamps = candle_stamps(probe_candles)
    decision_dt = decision_close_on_pane(probe_stamps, instant, tf)
    decision_unix = int(decision_dt.timestamp())

    if decision_unix != probe_unix:
        packed, candles = load_pane_candles(
            sym,
            tf,
            from_unix=int(PANE_FROM_UTC.timestamp()),
            to_unix=decision_unix,
            history_weeks=HISTORY_WEEKS,
        )
    else:
        packed, candles = _packed_probe, probe_candles

    strict = bool(packed.get("strict_complete_buckets"))
    if not strict:
        from bot.common_causal_pools_v1 import CommonCausalPoolsError

        raise CommonCausalPoolsError(
            f"strict_complete_buckets=False for {sym} {tf} decision={decision_dt.isoformat()}"
        )

    lld_cache = cache if cache is not None else {}
    result = common_causal_pools_from_loaded_pane(
        sym,
        candles,
        decision_dt,
        timeframe=tf,
        cache=lld_cache,
        strict_complete_buckets=strict,
    )

    selected_ids = {str(r.get("pool_id") or "") for r in result.ui_selected}
    pools = [
        _serialize_pool_row(row, timeframe=tf, selected_ids=selected_ids)
        for row in result.available
    ]
    decision_close_unix = int(_ensure_utc(result.decision_close).timestamp())
    overlays = scanner_common_pool_overlays(
        pools,
        decision_close_unix=decision_close_unix,
        render_end_unix=render_end_unix,
    )

    return {
        "layer": SCANNER_POOL_LAYER_NAME,
        "namespace": SCANNER_COMMON_NAMESPACE,
        "symbol": result.symbol,
        "timeframe": result.timeframe,
        "decision_close": result.decision_close.isoformat(),
        "decision_index": result.decision_index,
        "pool_count": len(pools),
        "rows_hash": result.rows_hash,
        "config_version": result.config_version,
        "pools": pools,
        "overlays": overlays,
        "proof": dict(result.proof),
    }


# Phase 2 public name (alias; same thin COMMON wrapper).
chart_common_pool_snapshot = chart_scanner_pool_snapshot
