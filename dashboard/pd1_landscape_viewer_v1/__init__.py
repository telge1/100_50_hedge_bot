"""PD1 Landscape research viewer (dashboard route, read-only bundle)."""

import os
from pathlib import Path

NAV_ACTIVE = "live-charts-viewer"
PAGE_PATH = "/live-charts/viewer"

_DEFAULT_VIEWER_ROOT = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_pd1_landscape_replay_v1/viewer"
)

VIEWER_ROOT = Path(os.environ.get("PD1_VIEWER_ROOT", str(_DEFAULT_VIEWER_ROOT))).resolve()

ALLOWED_DATA_FILES = frozenset(
    {"meta.json", "pools.json", "prices.json", "events.json", "clusters_by_dc.json"}
)
