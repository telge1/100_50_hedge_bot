"""Read-only coverage preflight for continuous discovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.ob200_v3_raw_discovery.files import list_closed_segments
from orderbook_analyse.ema_zone_microstructure_confirmation.research_layers import (
    COMPUTATION_MODE_EMA_ONLY,
    normalize_computation_mode,
)
from orderbook_analyse.orderbook_v2.ch_client import get_clickhouse_client, load_clickhouse_settings

_SETTINGS = {"max_execution_time": 180, "receive_timeout": 200}


@dataclass
class SourceSpan:
    source: str
    min_ts: datetime | None
    max_ts: datetime | None
    n_rows: int
    status: str
    notes: str = ""


def _iso_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _source_span_dict(span: SourceSpan) -> dict[str, Any]:
    return {
        "source": span.source,
        "min_ts": _iso_z(span.min_ts),
        "max_ts": _iso_z(span.max_ts),
        "n_rows": span.n_rows,
        "status": span.status,
        "notes": span.notes,
    }


def _q(client, sql: str, params: dict) -> tuple:
    rows = client.query(sql, parameters=params, settings=_SETTINGS).result_rows
    return rows[0] if rows else (None, None, 0)


def _as_utc(dt: Any) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return pd_to_utc(dt)


def pd_to_utc(dt: Any) -> datetime:
    import pandas as pd

    ts = pd.Timestamp(dt)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def probe_symbol_coverage(
    *,
    symbol: str,
    raw_root: Path,
    outcome_horizon_s: int = 14_400,
    computation_mode: str | None = None,
) -> dict[str, Any]:
    resolved_mode = normalize_computation_mode(computation_mode)
    ema_only = resolved_mode == COMPUTATION_MODE_EMA_ONLY
    load_clickhouse_settings()
    client = get_clickhouse_client()
    sources: list[SourceSpan] = []

    if ema_only:
        cmin, cmax, cn = _q(
            client,
            """
            SELECT min(open_time), max(open_time), count()
            FROM signal_generator.candles_1m FINAL
            WHERE symbol={s:String} AND interval='1m'
            """,
            {"s": symbol},
        )
        sources.append(
            SourceSpan(
                "candles_1m",
                _as_utc(cmin),
                _as_utc(cmax),
                int(cn or 0),
                "OK" if cn else "DATA_INCOMPLETE",
            )
        )
        if not cn or not cmin or not cmax:
            return {
                "symbol": symbol,
                "status": "DATA_INCOMPLETE",
                "incomplete_reason": "missing:candles_1m",
                "computation_mode": resolved_mode,
                "data_basis": "candles_1m",
                "orderbook_required": False,
                "sources": [_source_span_dict(s) for s in sources],
                "intersection_start": None,
                "intersection_end": None,
                "discovery_start": None,
                "discovery_end": None,
                "outcome_path_end": None,
                "closed_segments": 0,
                "open_tmp_ignored": 0,
                "segment_files": [],
            }
        inter_start = _as_utc(cmin)
        inter_end = _as_utc(cmax)
        warmup = timedelta(minutes=200 * 5)
        discovery_start = inter_start
        discovery_end = inter_end
        return {
            "symbol": symbol,
            "status": "OK" if inter_start < inter_end else "DATA_INCOMPLETE",
            "incomplete_reason": "" if inter_start < inter_end else "empty_intersection",
            "computation_mode": resolved_mode,
            "data_basis": "candles_1m",
            "orderbook_required": False,
            "sources": [_source_span_dict(s) for s in sources],
            "intersection_start": inter_start.isoformat().replace("+00:00", "Z"),
            "intersection_end": inter_end.isoformat().replace("+00:00", "Z"),
            "discovery_start": discovery_start.isoformat().replace("+00:00", "Z"),
            "discovery_end": discovery_end.isoformat().replace("+00:00", "Z"),
            "ema200_warmup_needed_before": (discovery_start - warmup).isoformat().replace("+00:00", "Z"),
            "outcome_horizon_s": outcome_horizon_s,
            "outcome_path_end": inter_end.isoformat().replace("+00:00", "Z") if inter_end else None,
            "closed_segments": 0,
            "open_tmp_ignored": 0,
            "segment_files": [],
        }

    segs = list_closed_segments(
        raw_root,
        symbols=(symbol,),
        start=datetime(2020, 1, 1, tzinfo=timezone.utc),
        end=datetime(2030, 1, 1, tzinfo=timezone.utc),
        include_boundary_stubs=False,
    )
    open_tmps = list(raw_root.rglob(f"{symbol}_*.tmp")) + list(raw_root.rglob(f"{symbol}_*.TMP"))
    if segs:
        l2_min = min(s.start_utc for s in segs)
        l2_max = max(s.end_utc for s in segs)
        sources.append(
            SourceSpan(
                "orderbook_ob200_v3_raw",
                l2_min,
                l2_max,
                len(segs),
                "OK",
                f"closed_segments={len(segs)}; open_tmp_ignored={len(open_tmps)}",
            )
        )
    else:
        sources.append(
            SourceSpan("orderbook_ob200_v3_raw", None, None, 0, "DATA_INCOMPLETE", "no_closed_segments")
        )

    cmin, cmax, cn = _q(
        client,
        """
        SELECT min(open_time), max(open_time), count()
        FROM signal_generator.candles_1m FINAL
        WHERE symbol={s:String} AND interval='1m'
        """,
        {"s": symbol},
    )
    sources.append(
        SourceSpan(
            "candles_1m",
            _as_utc(cmin),
            _as_utc(cmax),
            int(cn or 0),
            "OK" if cn else "DATA_INCOMPLETE",
        )
    )

    tmin, tmax, tn = _q(
        client,
        """
        SELECT min(trade_ts), max(trade_ts), count()
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol={s:String}
        """,
        {"s": symbol},
    )
    sources.append(
        SourceSpan(
            "public_trades_native",
            _as_utc(tmin),
            _as_utc(tmax),
            int(tn or 0),
            "OK" if tn else "DATA_INCOMPLETE",
        )
    )

    omin, omax, on = _q(
        client,
        """
        SELECT min(bucket_time), max(bucket_time), count()
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol={s:String}
        """,
        {"s": symbol},
    )
    sources.append(
        SourceSpan(
            "open_interest_1m",
            _as_utc(omin),
            _as_utc(omax),
            int(on or 0),
            "OK" if on else "DATA_INCOMPLETE",
            "derived_from_open_interest_5s",
        )
    )

    lmin, lmax, ln = _q(
        client,
        """
        SELECT min(event_time), max(event_time), count()
        FROM orderbook_analysis.all_liquidations
        WHERE symbol={s:String}
        """,
        {"s": symbol},
    )
    sources.append(
        SourceSpan(
            "liquidations",
            _as_utc(lmin),
            _as_utc(lmax),
            int(ln or 0),
            "OK" if ln else "DATA_INCOMPLETE",
        )
    )

    # Optional LLD — best-effort probe
    lld_status = "OPTIONAL_UNAVAILABLE"
    try:
        n_lld = client.query(
            "SELECT count() FROM system.tables WHERE database='orderbook_analysis' AND name LIKE '%liquidity%'",
            settings=_SETTINGS,
        ).result_rows[0][0]
        lld_status = "OPTIONAL_PRESENT" if n_lld else "OPTIONAL_UNAVAILABLE"
    except Exception:
        lld_status = "OPTIONAL_UNAVAILABLE"
    sources.append(SourceSpan("liquidity_locations", None, None, 0, lld_status, "optional"))

    required = [s for s in sources if s.source != "liquidity_locations"]
    ok = [s for s in required if s.status == "OK" and s.min_ts and s.max_ts]
    if len(ok) < len(required):
        missing = [s.source for s in required if s.status != "OK"]
        return {
            "symbol": symbol,
            "status": "DATA_INCOMPLETE",
            "incomplete_reason": "missing:" + ",".join(missing),
            "computation_mode": resolved_mode,
            "data_basis": "orderbook_ob200_v3_raw",
            "orderbook_required": True,
            "sources": [_source_span_dict(s) for s in sources],
            "intersection_start": None,
            "intersection_end": None,
            "discovery_start": None,
            "discovery_end": None,
            "outcome_path_end": None,
            "closed_segments": len(segs),
            "open_tmp_ignored": len(open_tmps),
            "segment_files": [s.path.name for s in segs],
        }

    inter_start = max(s.min_ts for s in ok)  # type: ignore[arg-type]
    inter_end = min(s.max_ts for s in ok)  # type: ignore[arg-type]
    # L2 closed end is binding for microstructure; candles may extend slightly
    l2 = next(s for s in sources if s.source == "orderbook_ob200_v3_raw")
    inter_start = max(inter_start, l2.min_ts)  # type: ignore[arg-type]
    inter_end = min(inter_end, l2.max_ts)  # type: ignore[arg-type]

    # EMA200 warmup: need 200 closed 5m bars ≈ 1000m before discovery
    warmup = timedelta(minutes=200 * 5)
    discovery_start = inter_start
    # align to next 5m boundary after start for cleaner regime clocks
    discovery_end = inter_end
    candle_max = next(s for s in sources if s.source == "candles_1m").max_ts
    outcome_path_end = candle_max

    return {
        "symbol": symbol,
        "status": "OK" if inter_start < inter_end else "DATA_INCOMPLETE",
        "incomplete_reason": "" if inter_start < inter_end else "empty_intersection",
        "computation_mode": resolved_mode,
        "data_basis": "orderbook_ob200_v3_raw",
        "orderbook_required": True,
        "sources": [_source_span_dict(s) for s in sources],
        "intersection_start": inter_start.isoformat().replace("+00:00", "Z"),
        "intersection_end": inter_end.isoformat().replace("+00:00", "Z"),
        "discovery_start": discovery_start.isoformat().replace("+00:00", "Z"),
        "discovery_end": discovery_end.isoformat().replace("+00:00", "Z"),
        "ema200_warmup_needed_before": (discovery_start - warmup).isoformat().replace("+00:00", "Z"),
        "outcome_horizon_s": outcome_horizon_s,
        "outcome_path_end": outcome_path_end.isoformat().replace("+00:00", "Z") if outcome_path_end else None,
        "closed_segments": len(segs),
        "open_tmp_ignored": len(open_tmps),
        "segment_files": [s.path.name for s in segs],
    }
