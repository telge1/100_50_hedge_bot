"""Optional integration: historical first-touch universe from external run dir."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ENGINE_ROOT.parent
_shadow = str(REPO_ROOT / "src")
while _shadow in sys.path:
    sys.path.remove(_shadow)
sys.path.insert(0, str(ENGINE_ROOT / "src"))
_ORDERBOOK_SRC = Path("/home/telgenbuescher/projects/orderbook_analyse/src")
if _ORDERBOOK_SRC.is_dir():
    sys.path.insert(0, str(_ORDERBOOK_SRC))

import pytest  # noqa: E402

from obfull_research_engine.mp_qdh_first_touch_study_v1.source_run import SOURCE_RUN_ENV  # noqa: E402
from obfull_research_engine.mp_qdh_first_touch_study_v1.universe import (  # noqa: E402
    build_first_touch_universe,
)

EXPECTED_UNIVERSE_HASH = "c76eac60161f6ba1596527a7844a933525949d78598f8eb4bb27efc438ea272d"


@pytest.mark.integration
def test_historical_first_touch_universe_external_source():
    src = (os.environ.get(SOURCE_RUN_ENV) or "").strip()
    if not src:
        pytest.skip(f"{SOURCE_RUN_ENV} not set; historical integration skipped")
    path = Path(src)
    assert path.is_dir(), f"source run dir missing: {path}"
    # Capture mtimes for read-only assertion
    watched = [path / "events_all.csv", path / "episodes.csv", path / "batch_windows.csv"]
    before = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in watched}

    uni = build_first_touch_universe(REPO_ROOT, source_run_dir=path)
    man = uni["manifest"]
    assert man["n_batch_events"] == 282
    assert man["n_first_touch_raw"] == 120
    assert man["n_included"] == 115
    assert man["exclusion_counts"].get("UNRESOLVED") == 5
    assert man["exclusion_counts"].get("NOT_FIRST_TOUCH") == 162
    assert uni["universe_hash"] == EXPECTED_UNIVERSE_HASH
    assert all(r["is_first_touch"] for r in uni["included"])
    for r in uni["included"]:
        if r["label_price_only"] == "TRUE_BREAK":
            assert r["trade_side"] == r["break_side"]
    assert uni["source_run"]["source_read_only_expected"] is True
    assert uni["source_run"]["resolution_source"] == "cli"

    after = {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in watched}
    assert after == before, "historical source files were modified"
