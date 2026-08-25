"""Import orderbook_analyse cluster_sweep_research without copying engines."""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

OA_ROOT = Path("/home/telgenbuescher/projects/orderbook_analyse")
OA_SRC = OA_ROOT / "src"


def ensure_oa_on_path() -> str:
    root = str(OA_SRC)
    if not OA_SRC.is_dir():
        raise RuntimeError(f"orderbook_analyse src not found: {root}")
    if root not in sys.path:
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
