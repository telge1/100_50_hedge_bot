"""Import trading_research_platform packages without pulling PySide / TRP app."""

from __future__ import annotations

import sys
from functools import lru_cache

from .boundary import TRP_ROOT


def ensure_trp_on_path() -> str:
    root = str(TRP_ROOT)
    if not TRP_ROOT.is_dir():
        raise RuntimeError(f"trading_research_platform not found: {root}")
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


@lru_cache(maxsize=1)
def load_trp():
    """Lazy import of GUI-free TRP modules."""
    ensure_trp_on_path()
    try:
        from dataclasses import replace as dc_replace

        from data.models import Candle, DataSource, ensure_utc
        from data.timeframes import (
            SUPPORTED_TIMEFRAMES,
            aggregate,
            expected_source_bars,
            floor_utc,
            timeframe_seconds,
        )
        from drawings.compose import compose_drawings
        from drawings.manager import DrawingManager
        from drawings.models import POSITION_TYPES, Drawing, DrawingStyle
        from drawings.persistence import (
            deserialize_drawing,
            load_default_style,
            load_drawings,
            save_drawings,
            serialize_drawing,
            serialize_style,
        )
        from drawings.position import (
            DEFAULT_NOTIONAL,
            DEFAULT_RISK_REWARD,
            compute_position,
            side_from_type,
        )
        from drawings.tools import (
            is_two_point,
            make_arrow,
            make_circle,
            make_hline,
            make_long_position,
            make_measure,
            make_rectangle,
            make_short_position,
            make_trend,
            make_vline,
        )
        from indicators.ema_overlays import EmaOverlayEngine, EmaOverlaysConfig, ema_overlays_payload
        from indicators.ema_overlays.config import EmaLineConfig
        from indicators.liquidity_location import LICENSE_NOTICE, LiquidityLocationConfig
        from indicators.liquidity_location.clusters import (
            cluster_bucket_counts,
            cluster_pools,
            filter_clusters,
        )
        from indicators.liquidity_location.compose import compose_lld_overlays, lld_ema_payload
        from indicators.liquidity_location.engine import run_liquidity_location
        from indicators.registry import EMA_OVERLAYS, LIQUIDITY_LOCATION, STOCHASTIC
        from indicators.settings_store import IndicatorSettingsStore
        from indicators.stochastic import StochasticConfig, compute_stochastic, stochastic_payload
        from overlays.manager import OverlayManager
        from overlays.models import OverlayLine, OverlayMarker, OverlayStyle
        from overlays.samples import build_test_overlays, is_test_overlay
        from overlays.serialization import resolve_color, serialize_overlays, to_unix_seconds
    finally:
        # Avoid shadowing dashboard/app.py with TRP's app package.
        root = str(TRP_ROOT)
        while root in sys.path:
            sys.path.remove(root)

    return {
        "Candle": Candle,
        "DataSource": DataSource,
        "ensure_utc": ensure_utc,
        "SUPPORTED_TIMEFRAMES": SUPPORTED_TIMEFRAMES,
        "aggregate": aggregate,
        "expected_source_bars": expected_source_bars,
        "floor_utc": floor_utc,
        "timeframe_seconds": timeframe_seconds,
        "EmaOverlayEngine": EmaOverlayEngine,
        "EmaOverlaysConfig": EmaOverlaysConfig,
        "EmaLineConfig": EmaLineConfig,
        "ema_overlays_payload": ema_overlays_payload,
        "StochasticConfig": StochasticConfig,
        "compute_stochastic": compute_stochastic,
        "stochastic_payload": stochastic_payload,
        "LiquidityLocationConfig": LiquidityLocationConfig,
        "LICENSE_NOTICE": LICENSE_NOTICE,
        "compose_lld_overlays": compose_lld_overlays,
        "lld_ema_payload": lld_ema_payload,
        "run_liquidity_location": run_liquidity_location,
        "cluster_pools": cluster_pools,
        "filter_clusters": filter_clusters,
        "cluster_bucket_counts": cluster_bucket_counts,
        "serialize_overlays": serialize_overlays,
        "resolve_color": resolve_color,
        "to_unix_seconds": to_unix_seconds,
        "DrawingManager": DrawingManager,
        "Drawing": Drawing,
        "DrawingStyle": DrawingStyle,
        "POSITION_TYPES": POSITION_TYPES,
        "compose_drawings": compose_drawings,
        "serialize_drawing": serialize_drawing,
        "deserialize_drawing": deserialize_drawing,
        "serialize_style": serialize_style,
        "save_drawings": save_drawings,
        "load_drawings": load_drawings,
        "load_default_style": load_default_style,
        "compute_position": compute_position,
        "side_from_type": side_from_type,
        "DEFAULT_NOTIONAL": DEFAULT_NOTIONAL,
        "DEFAULT_RISK_REWARD": DEFAULT_RISK_REWARD,
        "is_two_point": is_two_point,
        "make_hline": make_hline,
        "make_vline": make_vline,
        "make_trend": make_trend,
        "make_rectangle": make_rectangle,
        "make_measure": make_measure,
        "make_circle": make_circle,
        "make_arrow": make_arrow,
        "make_long_position": make_long_position,
        "make_short_position": make_short_position,
        "OverlayManager": OverlayManager,
        "OverlayLine": OverlayLine,
        "OverlayMarker": OverlayMarker,
        "OverlayStyle": OverlayStyle,
        "build_test_overlays": build_test_overlays,
        "is_test_overlay": is_test_overlay,
        "IndicatorSettingsStore": IndicatorSettingsStore,
        "EMA_OVERLAYS": EMA_OVERLAYS,
        "STOCHASTIC": STOCHASTIC,
        "LIQUIDITY_LOCATION": LIQUIDITY_LOCATION,
        "dc_replace": dc_replace,
    }
