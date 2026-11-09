"""EMA59 E1C 6M verified band + July cross markers (BT-P7A artifacts, read-only)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .bt_d12_latest_backtester import resolve_pools_ob_repo

SOURCE = "ema59_6m_band_overlay_v1"
STRATEGY_ID = "ema59_e1c_6m_band_research_v1"
VARIANT = "E1C_V_MONTHLY_FIXED"
ANALYSIS_MONTH = "2026-07"
SYMBOL = "DOGEUSDT"
TIMEFRAME = "5m"
EXPECTED_CROSS_COUNT = 20
MARKER_PREFIX = "ema59-6m-x-"
EMA59_SERIES_ID = "ema59_e1c_6m_verified"
LOWER_BAND_SERIES_ID = "ema59_e1c_6m_lower_2sigma"

DIAG_REL = Path("results/phase_ema59_bt_p7a_audit_v1/july_2026_6m_bar_diagnostics.json")
CROSSES_REL = Path("results/phase_e1c_ema59_6month_stddev_v1/long_crosses_E1C_V_MONTHLY_FIXED.json")
DATA_GAP_NOTE = "2026-07-31T23:55:00Z missing in ClickHouse (8927/8928 bars)"


def diagnostics_path() -> Path:
    return resolve_pools_ob_repo() / DIAG_REL


def crosses_path() -> Path:
    return resolve_pools_ob_repo() / CROSSES_REL


def load_diagnostics() -> list[dict[str, Any]] | None:
    path = diagnostics_path()
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return None
    return raw


def load_july_cross_rows() -> tuple[list[dict[str, Any]], list[str]]:
    path = crosses_path()
    errs: list[str] = []
    if not path.is_file():
        return [], errs + ["crosses_file_missing"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return [], errs + ["crosses_not_array"]
    rows = [
        r
        for r in raw
        if isinstance(r, dict)
        and str(r.get("variant") or "") == VARIANT
        and (
            str(r.get("analysis_month") or "") == ANALYSIS_MONTH
            or str(r.get("decision_close_utc") or "").startswith("2026-07")
        )
    ]
    rows.sort(key=lambda r: str(r.get("decision_close_utc") or ""))
    if len(rows) != EXPECTED_CROSS_COUNT:
        errs.append(f"cross_count:{len(rows)}")
    return rows, errs


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


def decision_close_to_chart_open_unix(decision_close_utc: str) -> int:
    """Research chart candle `time` = bar open (5m)."""
    dc = _parse_utc(decision_close_utc)
    op = dc - timedelta(minutes=5)
    return int(op.timestamp())


def build_summary() -> dict[str, Any]:
    diag = load_diagnostics()
    crosses, errs = load_july_cross_rows()
    thr = None
    if crosses:
        thr = crosses[0].get("thr_long")
    return {
        "variant": VARIANT,
        "analysis_month": ANALYSIS_MONTH,
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "expected_crosses": EXPECTED_CROSS_COUNT,
        "cross_count": len(crosses),
        "diagnostic_bars": len(diag) if diag else 0,
        "thr_long_pct": thr,
        "diagnostics_path": str(diagnostics_path()),
        "crosses_path": str(crosses_path()),
        "data_gap_note": DATA_GAP_NOTE,
        "errors": errs,
        "crosses": [
            {
                "cross_id": f"E1C-6M-{i+1:02d}",
                "watch_episode_id": f"6M-X-{i+1:02d}",
                "cross_index": i,
                "decision_close_utc": r.get("decision_close_utc"),
                "next_cross_decision_utc": (
                    crosses[i + 1].get("decision_close_utc") if i + 1 < len(crosses) else None
                ),
                "chart_open_utc": (
                    _parse_utc(str(r["decision_close_utc"])) - timedelta(minutes=5)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "close": r.get("close"),
                "ema59": r.get("ema59"),
                "ema_dist_pct": r.get("ema_dist_pct"),
                "thr_long": r.get("thr_long"),
            }
            for i, r in enumerate(crosses)
        ],
    }


_INJECTED_IDS = {EMA59_SERIES_ID, LOWER_BAND_SERIES_ID}
_THR_LONG_BY_MONTH: dict[str, float] | None = None


def ema59_ta_style(closes: list[float], period: int = 59) -> list[float | None]:
    """pandas ewm(span=period, adjust=False), same seed as E1C."""
    if not closes:
        return []
    out: list[float | None] = [None] * len(closes)
    alpha = 2.0 / (period + 1.0)
    prev: float | None = None
    for i, close in enumerate(closes):
        x = float(close)
        prev = x if prev is None else alpha * x + (1.0 - alpha) * prev
        out[i] = prev
    return out


def _thr_long_by_month() -> dict[str, float]:
    global _THR_LONG_BY_MONTH
    if _THR_LONG_BY_MONTH is not None:
        return _THR_LONG_BY_MONTH
    path = resolve_pools_ob_repo() / "results/phase_e1c_ema59_6month_stddev_v1/monthly_fixed_thresholds.json"
    out: dict[str, float] = {}
    if path.is_file():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for row in raw:
                if not isinstance(row, dict):
                    continue
                if str(row.get("variant") or VARIANT) != VARIANT:
                    continue
                month = str(row.get("analysis_month") or "")
                if month and row.get("thr_long") is not None:
                    out[month] = float(row["thr_long"])
    _THR_LONG_BY_MONTH = out
    return out


def _candle_unix_close(candle: Any) -> tuple[int, float] | None:
    ts = getattr(candle, "timestamp", None)
    close = getattr(candle, "close", None)
    if ts is None and isinstance(candle, dict):
        ts = candle.get("timestamp") or candle.get("time")
        close = candle.get("close")
    if ts is None or close is None:
        return None
    if isinstance(ts, (int, float)):
        unix = int(ts)
    else:
        if getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=timezone.utc)
        unix = int(ts.timestamp())
    return unix, float(close)


def _month_key(unix: int) -> str:
    return datetime.fromtimestamp(unix, tz=timezone.utc).strftime("%Y-%m")


def series_from_candles(candles: list[Any] | None) -> tuple[list[dict[str, float | int]], list[dict[str, float | int]]]:
    """E1C EMA59 and monthly-fixed −2σ for every loaded bar, not only July."""
    rows: list[tuple[int, float]] = []
    for candle in candles or []:
        parsed = _candle_unix_close(candle)
        if parsed is not None:
            rows.append(parsed)
    if not rows:
        return [], []
    rows.sort(key=lambda item: item[0])
    emas = ema59_ta_style([close for _unix, close in rows], 59)
    thresholds = _thr_long_by_month()
    ema_pts: list[dict[str, float | int]] = []
    band_pts: list[dict[str, float | int]] = []
    for (unix, _close), ema in zip(rows, emas):
        if ema is None:
            continue
        ema_pts.append({"time": unix, "value": float(ema)})
        thr = thresholds.get(_month_key(unix))
        if thr is None:
            continue
        band_pts.append({"time": unix, "value": float(ema) * (1.0 + thr / 100.0)})
    return ema_pts, band_pts


def _series_from_july_diagnostics() -> tuple[list[dict[str, float | int]], list[dict[str, float | int]]]:
    diag = load_diagnostics()
    if not diag:
        return [], []
    by_open_unix: dict[int, dict[str, Any]] = {}
    for row in diag:
        ts = str(row.get("timestamp_utc") or "")
        if not ts:
            continue
        by_open_unix[decision_close_to_chart_open_unix(ts)] = row
    ema_pts: list[dict[str, float | int]] = []
    band_pts: list[dict[str, float | int]] = []
    for open_unix in sorted(by_open_unix.keys()):
        row = by_open_unix[open_unix]
        ema_pts.append({"time": open_unix, "value": float(row["ema59"])})
        band_pts.append({"time": open_unix, "value": float(row["lower_2sigma_price"])})
    return ema_pts, band_pts


def build_verified_ema_payload(
    existing_ema: dict[str, Any],
    candles: list[Any] | None = None,
) -> dict[str, Any]:
    """Keep the user EMA59 (settings color) and add only the −2σ band line.

    Do not inject a second EMA59 series: it duplicated period 59 and drew amber
    (#f59e0b) on top of the configured blue line from indicator settings.
    """
    kept = [
        s
        for s in (existing_ema.get("series") or [])
        if s.get("id") not in _INJECTED_IDS
    ]
    ema_pts, band_pts = series_from_candles(candles)
    source = "E1C monthly-fixed on loaded candles"
    if not ema_pts:
        ema_pts, band_pts = _series_from_july_diagnostics()
        source = "BT-P7A diagnostics"
    if band_pts:
        kept.append(
            {
                "id": LOWER_BAND_SERIES_ID,
                "title": "EMA59 −2σ",
                "period": 0,
                "color": "#a855f7",
                "line_width": 2,
                "line_style": "dashed",
                "visible": True,
                "verified_source": source,
                "data": band_pts,
            }
        )
    return {
        "series": kept,
        "ema59_6m_band_active": bool(band_pts),
        "data_gap_note": DATA_GAP_NOTE,
    }


def _cross_id(index: int, row: dict[str, Any]) -> str:
    slug = re.sub(r"[^0-9TZtz]", "", str(row.get("decision_close_utc") or "")) or str(index)
    return f"{MARKER_PREFIX}{slug}"


def build_cross_marker_specs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for i, row in enumerate(rows):
        dc = str(row.get("decision_close_utc") or "")
        if not dc:
            continue
        open_unix = decision_close_to_chart_open_unix(dc)
        specs.append(
            {
                "overlay_id": _cross_id(i, row),
                "chart_open_unix": open_unix,
                "chart_open_utc": (
                    _parse_utc(dc) - timedelta(minutes=5)
                ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "decision_close_utc": dc,
                "price": float(row.get("close") or 0),
                "metadata": {
                    "cross_id": f"E1C-6M-{i+1:02d}",
                    "utc_decision_close": dc,
                    "close": row.get("close"),
                    "ema59": row.get("ema59"),
                    "ema_dist_pct": row.get("ema_dist_pct"),
                    "thr_long_pct": row.get("thr_long"),
                    "mean_distance_pct": row.get("mean_distance_pct"),
                    "std_distance_pct": row.get("std_distance_pct"),
                    "variant": VARIANT,
                    "signal_on": "decision_close",
                    "chart_anchor": "bar_open",
                },
            }
        )
    return specs


def build_overlay_markers(rows: list[dict[str, Any]], *, symbol: str) -> list[Any]:
    from .trp_import import load_trp

    trp = load_trp()
    OverlayMarker = trp["OverlayMarker"]
    OverlayStyle = trp["OverlayStyle"]
    sym = str(symbol or "").strip().upper()
    out = []
    for spec in build_cross_marker_specs(rows):
        meta = {
            "origin": SOURCE,
            "strategy_id": STRATEGY_ID,
            "kind": "E1C_6M_LONG_CROSS",
            **(spec.get("metadata") or {}),
        }
        ts = datetime.fromtimestamp(int(spec["chart_open_unix"]), tz=timezone.utc)
        out.append(
            OverlayMarker(
                overlay_id=str(spec["overlay_id"]),
                symbol=sym,
                timestamp=trp["ensure_utc"](ts),
                price=spec.get("price"),
                position="below",
                shape="arrow_up",
                text="6M ↓",
                size=10.0,
                style=OverlayStyle(color="#22c55e", width=1.0),
                timeframe_scope=TIMEFRAME,
                visible=True,
                z_order=44,
                metadata=meta,
            )
        )
    return out
