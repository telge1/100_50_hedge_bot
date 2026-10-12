"""Read-only Research adapter: Live Micro LLD V2 pools from isolated CH test tables."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .chart_live_micro_lld_v2 import _parse_close
from .live_micro_lld_v2_ch_test.errors import (
    AmbiguousRunError,
    ChTestError,
    ConfigNotAvailableError,
    OutOfCoverageError,
)
from .live_micro_lld_v2_ch_test.identity import (
    LEGACY_RUN_OVERRIDES,
    build_live_micro_lld_v2_config,
    config_hash,
    preset_matrix_entry,
)
from .live_micro_lld_v2_ch_test.reader import LiveMicroLldV2ChTestReader
from .live_micro_lld_v2_ch_test.run_resolve import RunSelector

LIVE_MICRO_LLD_V2_CH_TEST_LAYER = "live_micro_lld_v2_ch_test"
LIVE_MICRO_LLD_V2_CH_TEST_NAMESPACE = "LIVE_MICRO_LLD_V2_CH_TEST"

DEFAULT_RUN_ID = "de5f74f6-236f-4dc1-81df-94e15dec1b83"

SUPPORTED_SYMBOL = "TUTUSDT"
SUPPORTED_TIMEFRAME = "5m"
SUPPORTED_POOL_DETAILS = frozenset(range(1, 11))

# Research prototype scope (multi-PD storage; symbol/tf gate for dashboard).
CH_TEST_SYMBOLS = frozenset({SUPPORTED_SYMBOL})
CH_TEST_TIMEFRAMES = frozenset({SUPPORTED_TIMEFRAME})


def _coverage_doc(run_meta: dict[str, Any]) -> dict[str, Any]:
    rid = str(run_meta.get("run_id"))
    legacy = LEGACY_RUN_OVERRIDES.get(rid, {})
    from_close = run_meta.get("from_close") or legacy.get("from_close")
    to_close = (
        run_meta.get("to_close")
        or run_meta.get("decision_close")
        or legacy.get("to_close")
    )
    load_start = run_meta.get("candle_load_start") or legacy.get("candle_load_start")
    pd = int(run_meta.get("pool_detail") or legacy.get("pool_detail") or 0)
    warmup = preset_matrix_entry(pd)["warmup_bars"] if pd else None
    if hasattr(from_close, "isoformat"):
        from_close = from_close.isoformat()
    if hasattr(to_close, "isoformat"):
        to_close = to_close.isoformat()
    if hasattr(load_start, "isoformat"):
        load_start = load_start.isoformat()
    return {
        "run_id": rid,
        "symbol": str(run_meta.get("symbol") or SUPPORTED_SYMBOL).upper(),
        "timeframe": str(run_meta.get("timeframe") or SUPPORTED_TIMEFRAME),
        "pool_detail": pd,
        "config_hash": str(run_meta.get("config_hash") or ""),
        "engine_version": str(run_meta.get("engine_version") or ""),
        "engine_input_hash": str(run_meta.get("engine_input_hash") or ""),
        "coverage_from_close": str(from_close),
        "coverage_to_close": str(to_close),
        "candle_load_start": str(load_start) if load_start else "",
        "warmup_bars": warmup,
        "engine_label": "run_live_micro_lld_v2",
    }


def _assert_supported(symbol: str, timeframe: str, pool_detail: int) -> None:
    sym = str(symbol or "").strip().upper()
    tf = str(timeframe or "").strip()
    pd = int(pool_detail)
    if sym not in CH_TEST_SYMBOLS or tf not in CH_TEST_TIMEFRAMES:
        raise ValueError(
            f"CH test prototype supports symbols {sorted(CH_TEST_SYMBOLS)} "
            f"and timeframes {sorted(CH_TEST_TIMEFRAMES)}"
        )
    if pd not in SUPPORTED_POOL_DETAILS:
        raise ValueError("pool_detail must be 1..10")


def _assert_in_coverage(from_dt: datetime, to_dt: datetime, coverage: dict[str, Any]) -> None:
    cov_from = _parse_close(coverage["coverage_from_close"])
    cov_to = _parse_close(coverage["coverage_to_close"])
    if to_dt < cov_from or from_dt > cov_to:
        raise OutOfCoverageError(
            "requested window outside CH test run coverage",
            coverage,
        )
    if to_dt > cov_to or from_dt < cov_from:
        raise OutOfCoverageError(
            "requested window extends outside CH test run coverage",
            coverage,
        )


def _tag_ch_test_zones(zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for z in zones:
        z2 = dict(z)
        z2["namespace"] = LIVE_MICRO_LLD_V2_CH_TEST_NAMESPACE
        pid = str(z2.get("pool_id") or "")
        z2["id"] = "live-micro-v2-ch-test:" + pid
        style = dict(z2.get("style") or {})
        z2["style"] = style
        z2["border_style"] = "dashed"
        meta = dict(z2.get("metadata") or {})
        meta["source"] = LIVE_MICRO_LLD_V2_CH_TEST_LAYER
        z2["metadata"] = meta
        out.append(z2)
    return out


def chart_live_micro_lld_v2_ch_test(
    symbol: str,
    from_close: datetime | str,
    to_close: datetime | str,
    *,
    timeframe: str = "5m",
    run_id: str | None = None,
    pool_detail: int = 10,
    pool_types: list[str] | None = None,
    strength_classes: list[str] | None = None,
) -> dict[str, Any]:
    pd = int(pool_detail)
    _assert_supported(symbol, timeframe, pd)
    from_dt = _parse_close(from_close)
    to_dt = _parse_close(to_close)
    if from_dt > to_dt:
        raise ValueError("from_close must be <= to_close")

    expected_hash = config_hash(build_live_micro_lld_v2_config(pd))
    selector = RunSelector()
    try:
        run_meta = selector.resolve_run(
            symbol,
            timeframe,
            pd,
            run_id=run_id,
            require_config_hash=expected_hash,
        )
    except (ConfigNotAvailableError, AmbiguousRunError):
        raise

    coverage = _coverage_doc(run_meta)
    _assert_in_coverage(from_dt, to_dt, coverage)

    rid = str(run_meta["run_id"])
    reader = LiveMicroLldV2ChTestReader()
    pt_set = set(pool_types) if pool_types else None
    sc_set = set(strength_classes) if strength_classes else None
    zones = reader.display_segments(
        rid,
        from_dt,
        to_dt,
        pool_types=pt_set,
        strength_classes=sc_set,
    )
    zones = _tag_ch_test_zones(zones)

    return {
        "layer": LIVE_MICRO_LLD_V2_CH_TEST_LAYER,
        "namespace": LIVE_MICRO_LLD_V2_CH_TEST_NAMESPACE,
        "symbol": str(symbol).upper(),
        "timeframe": str(timeframe),
        "pool_detail": pd,
        "run_id": rid,
        "config_hash": expected_hash,
        "from_close": from_dt.isoformat(),
        "to_close": to_dt.isoformat(),
        "as_of_close": to_dt.isoformat(),
        "pool_count": len({z["pool_id"] for z in zones}),
        "segment_count": len(zones),
        "zones": zones,
        "coverage": coverage,
        "engine_input": {
            "candle_load_start": coverage.get("candle_load_start"),
            "warmup_bars": coverage.get("warmup_bars"),
            "coverage_from_close": coverage.get("coverage_from_close"),
            "coverage_to_close": coverage.get("coverage_to_close"),
            "engine_input_hash": coverage.get("engine_input_hash"),
            "note": "Pools read from CH snapshot; no engine run in this endpoint.",
        },
    }
