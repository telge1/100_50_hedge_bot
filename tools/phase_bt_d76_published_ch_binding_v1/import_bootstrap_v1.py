"""5m PD1 published CH import path + pools-dashboard landscape module (BT-D76)."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
POOLS_DASH = ROOT / "backtester/short/dashboard"
D76 = Path(__file__).resolve().parent
IMPORT_5M = D76.parent / "phase4xao_jun_sep_nohup_rollout_v1" / "import_path_5m_pd1.py"


def ensure_published_ch_density_import_path() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location("import_path_5m_pd1", IMPORT_5M)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    info = mod.ensure_5m_pd1_readback_import_path()
    os.environ.setdefault("HISTORICAL_LMV2_BATCH_READER", "1")
    return dict(info)


def load_pools_landscape_window_module() -> Any:
    """Pools-repo landscape builder (density_only + member_cache); not spread shadow."""
    ensure_published_ch_density_import_path()
    for mod in list(sys.modules):
        if mod == "pd1_landscape_viewer_v1" or mod.startswith("pd1_landscape_viewer_v1."):
            del sys.modules[mod]
    if str(POOLS_DASH) not in sys.path:
        sys.path.insert(0, str(POOLS_DASH))
    import pd1_landscape_viewer_v1.historical_window_v1 as landscape_mod  # noqa: WPS433

    return landscape_mod
