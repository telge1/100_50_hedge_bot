"""Dashboard source provenance + config hashes (read-only)."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

DASHBOARD_ROOT = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
OA_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")

SOURCE_FILES = (
    DASHBOARD_ROOT / "market_profile_v1" / "dual_profile.py",
    DASHBOARD_ROOT / "market_profile_v1" / "service.py",
    DASHBOARD_ROOT / "market_profile_v1" / "api.py",
    OA_SRC / "orderbook_analyse" / "market_profile" / "profile.py",
    OA_SRC / "orderbook_analyse" / "market_profile" / "loader.py",
    OA_SRC / "orderbook_analyse" / "market_profile" / "anchor.py",
    OA_SRC / "orderbook_analyse" / "market_profile" / "shape.py",
    OA_SRC / "orderbook_analyse" / "market_profile" / "contracts.py",
    OA_SRC / "orderbook_analyse" / "market_profile" / "__init__.py",
)


def ensure_mp_import_path() -> None:
    for p in (str(DASHBOARD_ROOT), str(OA_SRC)):
        if p not in sys.path:
            sys.path.insert(0, p)


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def collect_provenance() -> dict[str, Any]:
    ensure_mp_import_path()
    from market_profile_v1 import FORMAT_VERSION as DASH_FMT
    from market_profile_v1.dual_profile import (
        DEFAULT_BRACKET_MINUTES,
        DUAL_CONTRACT_VERSION,
        TPO_CONTRACT,
        TRADES_FQN,
        VOLUME_CONTRACT,
    )
    from market_profile_v1.service import (
        DEFAULT_TARGET_BINS,
        DEFAULT_VALUE_AREA_PCT,
        PERIOD_MP_TIMEFRAMES,
    )
    from orderbook_analyse.market_profile import (
        CANDLES_FQN,
        DEFAULT_HVN_FACTOR,
        DEFAULT_LVN_FACTOR,
        FORMAT_VERSION as OA_FMT,
    )
    from orderbook_analyse.market_profile.contracts import ShapeThresholds

    hashes = {str(p): file_sha256(p) for p in SOURCE_FILES if p.exists()}
    blob = json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()
    source_code_hash = hashlib.sha256(blob).hexdigest()
    thresholds = ShapeThresholds()
    cfg = {
        "value_area_pct": DEFAULT_VALUE_AREA_PCT,
        "target_bins": DEFAULT_TARGET_BINS,
        "bracket_minutes": DEFAULT_BRACKET_MINUTES,
        "use_final": False,
        "period_mp_timeframes": list(PERIOD_MP_TIMEFRAMES),
        "hvn_factor": DEFAULT_HVN_FACTOR,
        "lvn_factor": DEFAULT_LVN_FACTOR,
        "shape_thresholds": thresholds.to_dict(),
        "trades_fqn": TRADES_FQN,
        "candles_fqn": CANDLES_FQN,
        "dual_contract_version": DUAL_CONTRACT_VERSION,
        "tpo_contract": TPO_CONTRACT,
        "volume_contract": VOLUME_CONTRACT,
    }
    config_hash = hashlib.sha256(
        json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "dashboard_root": str(DASHBOARD_ROOT),
        "dashboard_format_version": DASH_FMT,
        "oa_format_version": OA_FMT,
        "dual_contract_version": DUAL_CONTRACT_VERSION,
        "tpo_contract": TPO_CONTRACT,
        "volume_contract": VOLUME_CONTRACT,
        "compute_path": "DIRECT_IMPORT_DASHBOARD_PURE_FUNCTIONS",
        "entry_function": "market_profile_v1.dual_profile.build_dual_window_profile",
        "window_function": "orderbook_analyse.market_profile.anchor.build_windows",
        "value_area_function": "orderbook_analyse.market_profile.profile.compute_value_area",
        "shape_function": "orderbook_analyse.market_profile.shape.classify_shape",
        "price_step_function": "orderbook_analyse.market_profile.loader.resolve_price_step",
        "volume_loader": "orderbook_analyse.market_profile.loader.fetch_volume_at_price",
        "tpo_engine": "clickhouse_bracket_agg",
        "volume_engine": "clickhouse_volume_agg",
        "no_formula_fork": True,
        "source_files": {k: v for k, v in hashes.items()},
        "source_code_hash": source_code_hash,
        "config": cfg,
        "config_hash": config_hash,
    }
