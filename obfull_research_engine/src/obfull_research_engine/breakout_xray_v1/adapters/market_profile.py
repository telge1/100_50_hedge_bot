"""Market-profile adapter (V1 Option B: dashboard dual import)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .. import FORMULA_ID, IMPLEMENTATION_SOURCE
from ..time_windows import as_utc


def previous_closed_30m_bounds(decision_time: datetime) -> tuple[datetime, datetime]:
    from obfull_research_engine.market_profile_context.windows import (
        previous_closed_bounds,
    )

    return previous_closed_bounds(as_utc(decision_time), "30m")


def load_strategy_tpo_edge_profile(
    *,
    symbol: str,
    decision_time: datetime,
    client: Any | None = None,
) -> dict[str, Any]:
    """Previous closed 30m TPO VAH/VAL via dashboard-identical adapter.

    Live CH must be blocked by execution hold before calling this.
    """
    from obfull_research_engine.market_profile_context.adapter import build_one_profile

    start, end = previous_closed_30m_bounds(decision_time)
    raw = build_one_profile(
        symbol=symbol,
        tf="30m",
        start=start,
        end=end,
        kind="previous_closed",
        client=client,
        include_bins=False,
    )
    if raw is None:
        raise RuntimeError("STOP_XRAY_MP_PROFILE_EMPTY")
    tpo = raw.get("tpo") or {}
    va = tpo.get("value_area") or {}
    if "vah" not in va and isinstance(raw.get("value_area"), dict):
        va = raw["value_area"]
    return {
        "window_start": start,
        "window_end": end,
        "tpo": {
            "value_area": {
                "vah": va.get("vah"),
                "val": va.get("val"),
                "poc": va.get("poc"),
            }
        },
        "raw": raw,
        "formula_id": FORMULA_ID,
        "implementation_source": IMPLEMENTATION_SOURCE,
        "temporary_cross_repo_dependency": True,
    }


def mp_manifest_fields() -> dict[str, Any]:
    return {
        "formula_id": FORMULA_ID,
        "implementation_source": IMPLEMENTATION_SOURCE,
        "temporary_cross_repo_dependency": True,
        "edge_definition": "TPO_VAH_upper_TPO_VAL_lower",
        "followup": "extract_tpo_dual_to_oa_core_not_in_v1",
    }


def profile_from_fixture(
    *,
    window_start: datetime,
    window_end: datetime,
    vah: float,
    val: float,
    poc: float | None = None,
) -> dict[str, Any]:
    """Offline fixture profile matching resolve_strategy_reference shape."""
    return {
        "window_start": as_utc(window_start),
        "window_end": as_utc(window_end),
        "tpo": {
            "value_area": {
                "vah": float(vah),
                "val": float(val),
                "poc": poc if poc is not None else (float(vah) + float(val)) / 2.0,
            }
        },
        "formula_id": FORMULA_ID,
        "implementation_source": IMPLEMENTATION_SOURCE,
        "temporary_cross_repo_dependency": True,
    }


def offline_value_area_from_bins(
    bins: list[tuple[float, float, float, float, float]],
    *,
    value_area_pct: float = 0.70,
) -> dict[str, Any]:
    """``bins`` = (bin_index, price_low, price_high, price_mid, volume)."""
    from orderbook_analyse.market_profile.contracts import ProfileBin
    from orderbook_analyse.market_profile.profile import compute_value_area

    profile_bins = [
        ProfileBin(
            bin_index=int(i),
            price_low=float(lo),
            price_high=float(hi),
            price_mid=float(mid),
            volume=float(vol),
            buy_volume=float(vol) * 0.5,
            sell_volume=float(vol) * 0.5,
            trades=1,
            notional=float(mid) * float(vol),
        )
        for i, lo, hi, mid, vol in bins
    ]
    va = compute_value_area(profile_bins, value_area_pct)
    return {
        "poc": va.poc,
        "vah": va.vah,
        "val": va.val,
        "value_area_pct": value_area_pct,
        "bin_count": va.bin_count,
        "volume_share": va.volume_share,
    }


def offline_dual_profile_parity(
    *,
    symbol: str,
    window_start: datetime,
    window_end: datetime,
    trades: list[dict[str, Any]],
    candles_1m: Any = None,
) -> dict[str, Any]:
    """Run dashboard dual_profile with injected trades (no ClickHouse).

    Trade dicts must include ``ts`` (datetime), ``price``, ``size``, ``trade_id``,
    and preferably ``side`` — matching research volume_profile fixtures.
    """
    from obfull_research_engine.market_profile_context.provenance import (
        ensure_mp_import_path,
    )
    from obfull_research_engine.market_profile_context.windows import make_window

    ensure_mp_import_path()
    from market_profile_v1.dual_profile import build_dual_window_profile
    from market_profile_v1.service import DEFAULT_TARGET_BINS, DEFAULT_VALUE_AREA_PCT
    from orderbook_analyse.market_profile.contracts import ShapeThresholds

    window = make_window(
        "30m", as_utc(window_start), as_utc(window_end), kind="previous_closed"
    )

    class _NoCH:
        def query(self, *a, **k):  # pragma: no cover
            raise RuntimeError("STOP_XRAY_MP_NO_CH_IN_OFFLINE_PARITY")

    normalized = []
    for t in trades:
        row = dict(t)
        if "ts" not in row and "trade_ts" in row:
            row["ts"] = row["trade_ts"]
        normalized.append(row)

    raw = build_dual_window_profile(
        _NoCH(),
        symbol.upper(),
        window,
        value_area_pct=float(DEFAULT_VALUE_AREA_PCT),
        target_bins=int(DEFAULT_TARGET_BINS),
        use_final=True,
        thresholds=ShapeThresholds(),
        candles_1m=candles_1m,
        trades=normalized,
        include_bins=True,
    )
    tpo = (raw or {}).get("tpo") or {}
    va = tpo.get("value_area") or {}
    # upper/lower reference = VAH/VAL for strategy edges
    return {
        "poc": va.get("poc"),
        "vah": va.get("vah"),
        "val": va.get("val"),
        "upper_reference": va.get("vah"),
        "lower_reference": va.get("val"),
        "profile_window_start": as_utc(window_start),
        "profile_window_end": as_utc(window_end),
        "value_area_pct": float(DEFAULT_VALUE_AREA_PCT),
        "raw": raw,
        "formula_id": FORMULA_ID,
    }
