"""Exact-origin density render parity (adapter JS vs 5m-reference sweep grid)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent
ADAPTER = DASH / "static/js/research/pd1_exact_density_trp_adapter.js"
NODE_TEST = DASH / "test_pd1_eod_density_render_parity_v1.js"


@pytest.mark.parametrize("timeframe", ["1h", "5m"])
def test_density_sweep_grid_matches_5m_reference(timeframe: str):
    from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response

    win = get_landscape_window_response(
        from_utc="2026-08-14T18:00:00Z",
        to_utc="2026-08-15T18:00:00Z",
        timeframe=timeframe,
        pool_detail=1,
        response_mode="exact_origin_v1",
    )
    pools_path = DASH / "logs" / f"_parity_pools_{timeframe}.json"
    pools_path.write_text(json.dumps(win["pools"]), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(NODE_TEST), str(pools_path), str(timeframe)],
        cwd=str(DASH),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    out = json.loads(proc.stdout.strip().splitlines()[-1])
    assert out["parity"] == "PASS", out


def test_active_lookup_parity_1h():
    from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response

    win = get_landscape_window_response(
        from_utc="2026-08-14T12:00:00Z",
        to_utc="2026-08-15T12:00:00Z",
        timeframe="1h",
        pool_detail=1,
        response_mode="exact_origin_v1",
    )
    pools_path = DASH / "logs" / "_parity_pools_active_lookup.json"
    pools_path.write_text(json.dumps(win["pools"]), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DASH / "test_pd1_eod_active_lookup_parity_v1.js"), str(pools_path)],
        cwd=str(DASH),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_sweep_cache_cull_parity_1h():
    from pd1_landscape_viewer_v1.window_service_v1 import get_landscape_window_response

    win = get_landscape_window_response(
        from_utc="2026-08-14T12:00:00Z",
        to_utc="2026-08-15T12:00:00Z",
        timeframe="1h",
        pool_detail=1,
        response_mode="exact_origin_v1",
    )
    pools_path = DASH / "logs" / "_parity_pools_sweep_cache.json"
    pools_path.write_text(json.dumps(win["pools"]), encoding="utf-8")
    proc = subprocess.run(
        ["node", str(DASH / "test_pd1_eod_sweep_cache_parity_v1.js"), str(pools_path)],
        cwd=str(DASH),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
