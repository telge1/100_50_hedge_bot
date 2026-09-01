"""Import orderbook_analyse packages without copying engines.

Bootstrap rule: call ensure_oa_on_path() before any ``import orderbook_analyse...``.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
OA_SRC = OA_ROOT / "src"
OA_INIT = OA_SRC / "orderbook_analyse" / "__init__.py"


def ensure_oa_on_path() -> str:
    """Insert ``<oa_root>/src`` at sys.path[0] before importing orderbook_analyse."""
    root = str(OA_SRC.resolve())
    if not OA_INIT.is_file():
        raise RuntimeError(
            "orderbook_analyse_src_missing: "
            f"expected package init at {OA_INIT}"
        )
    # Keep a single front entry even if a later duplicate exists.
    while root in sys.path:
        sys.path.remove(root)
    sys.path.insert(0, root)
    return root


@lru_cache(maxsize=1)
def load_cluster_sweep():
    ensure_oa_on_path()
    from orderbook_analyse.cluster_sweep_research.audit_export import (  # noqa: WPS433
        MANUAL_VERDICTS,
        final_status,
    )
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import (  # noqa: WPS433
        coverage_report,
        default_client,
        fetch_liquidations,
        fetch_ob_1m,
        fetch_oi_1m,
        fetch_trades_1m,
    )
    from orderbook_analyse.cluster_sweep_research.ema_features import (  # noqa: WPS433
        required_warmup_bars,
    )
    from orderbook_analyse.cluster_sweep_research.pipeline import (  # noqa: WPS433
        STRATEGY_ID,
        STRATEGY_VERSION,
        run_cluster_sweep_on_candles,
    )

    return {
        "STRATEGY_ID": STRATEGY_ID,
        "STRATEGY_VERSION": STRATEGY_VERSION,
        "MANUAL_VERDICTS": MANUAL_VERDICTS,
        "final_status": final_status,
        "coverage_report": coverage_report,
        "default_client": default_client,
        "fetch_liquidations": fetch_liquidations,
        "fetch_ob_1m": fetch_ob_1m,
        "fetch_oi_1m": fetch_oi_1m,
        "fetch_trades_1m": fetch_trades_1m,
        "required_warmup_bars": required_warmup_bars,
        "run_cluster_sweep_on_candles": run_cluster_sweep_on_candles,
    }


@lru_cache(maxsize=1)
def load_market_profile():
    """Anchored market profile compute surface.

    Deliberately excludes ``market_profile.render``: that module is the
    matplotlib PNG writer for the offline script and would pull a GUI-less
    plotting stack into the web process for no reason. Everything here is
    read-only compute over an injected ClickHouse client.
    """
    ensure_oa_on_path()
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import (  # noqa: WPS433
        aggregate_timeframe,
        fetch_candles_1m,
    )
    from orderbook_analyse.market_profile import SESSIONS  # noqa: WPS433
    from orderbook_analyse.market_profile.anchor import build_windows  # noqa: WPS433
    from orderbook_analyse.market_profile.build import (  # noqa: WPS433
        build_profile,
        mark_naked_pocs,
    )
    from orderbook_analyse.market_profile.contracts import ShapeThresholds  # noqa: WPS433

    return {
        "SESSIONS": SESSIONS,
        "ShapeThresholds": ShapeThresholds,
        "aggregate_timeframe": aggregate_timeframe,
        "build_profile": build_profile,
        "build_windows": build_windows,
        "fetch_candles_1m": fetch_candles_1m,
        "mark_naked_pocs": mark_naked_pocs,
    }


@lru_cache(maxsize=1)
def load_ema_dual_cross():
    ensure_oa_on_path()
    from orderbook_analyse.cluster_sweep_research.clickhouse_source import (  # noqa: WPS433
        coverage_report,
        default_client,
        fetch_liquidations,
        fetch_ob_1m,
        fetch_oi_1m,
        fetch_trades_1m,
    )
    from orderbook_analyse.cluster_sweep_research.ema_features import required_warmup_bars  # noqa: WPS433
    from orderbook_analyse.ema_dual_cross_multisource.config import (  # noqa: WPS433
        EMA_DUAL_CROSS_DEFAULTS,
        STRATEGY_ID,
        STRATEGY_VERSION,
    )
    from orderbook_analyse.ema_dual_cross_multisource.pipeline import run_ema_dual_cross_on_candles  # noqa: WPS433

    return {
        "STRATEGY_ID": STRATEGY_ID,
        "STRATEGY_VERSION": STRATEGY_VERSION,
        "EMA_DUAL_CROSS_DEFAULTS": EMA_DUAL_CROSS_DEFAULTS,
        "coverage_report": coverage_report,
        "default_client": default_client,
        "fetch_liquidations": fetch_liquidations,
        "fetch_ob_1m": fetch_ob_1m,
        "fetch_oi_1m": fetch_oi_1m,
        "fetch_trades_1m": fetch_trades_1m,
        "required_warmup_bars": required_warmup_bars,
        "run_ema_dual_cross_on_candles": run_ema_dual_cross_on_candles,
    }
