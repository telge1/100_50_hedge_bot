"""Open-tile lifetime resume matches a full rebuild and skips the warmup scan."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from pd1_landscape_viewer_v1 import historical_window_v1 as hw
from pd1_landscape_viewer_v1.window_build_process_v1 import ping_child


def _members(dc: datetime) -> list[dict]:
    step = int(dc.timestamp()) // 300
    rows = [
        {
            "pool_id": "A",
            "side": "lower",
            "lower_bound": 100.0 + (step % 3),
            "upper_bound": 110.0 + (step % 3),
            "strength": 1.0 + (step % 3),
            "strength_class": "Weak",
        }
    ]
    if step % 7 != 0:
        rows.append(
            {
                "pool_id": "B",
                "side": "upper",
                "lower_bound": 200.0,
                "upper_bound": 210.0 - (0.5 if step > 12 else 0.0),
                "strength": 2.0,
                "strength_class": "Medium",
            }
        )
    if step >= 8:
        rows.append(
            {
                "pool_id": "C",
                "side": "lower",
                "lower_bound": 50.0,
                "upper_bound": 60.0,
                "strength": 3.0,
                "strength_class": "Strong",
            }
        )
    return rows


def _grid(n: int, start: datetime | None = None) -> list[datetime]:
    t = start or datetime(2026, 11, 27, 0, 0, tzinfo=timezone.utc)
    return [t + timedelta(minutes=5 * i) for i in range(n)]


def _norm(pools: list[dict]) -> list[dict]:
    out = []
    for pl in pools:
        out.append(
            {
                "pool_id": pl["pool_id"],
                "bottom": pl["bottom"],
                "top": pl["top"],
                "strength": pl["strength"],
                "segments": [list(seg) for seg in pl["segments"]],
            }
        )
    return out


@pytest.fixture
def patched_members(monkeypatch):
    calls = {"n": 0}

    def fake_load(cache, dc, client, db, hcfg):
        calls["n"] += 1
        return _members(dc)

    monkeypatch.setattr(hw, "_load_members", fake_load)
    hw.clear_lifetime_resume_for_tests()
    return calls


def _hcfg():
    return SimpleNamespace(
        symbol="TESTUSDT",
        pipeline_line_id="line",
        config_hash="hash",
        timeframe="5m",
        pool_detail=1,
    )


@pytest.mark.parametrize("slot", [5, 15])
@pytest.mark.parametrize("split_at", [1, 7, 11, 20])
def test_resume_matches_full_scan(patched_members, slot, split_at):
    grid = _grid(24)
    hcfg = _hcfg()
    full, *_rest = hw._build_lifetimes_state(grid, {}, None, "db", hcfg, slot)
    head_pool, open_start, open_dc, open_end = hw._build_lifetimes_state(
        grid[:split_at], {}, None, "db", hcfg, slot
    )
    before = patched_members["n"]
    resumed, *_cursor = hw.resume_open_lifetimes(
        head_pool,
        open_start,
        open_dc,
        open_end,
        grid[split_at:],
        slot,
        lambda dc: hw._load_members({}, dc, None, "db", hcfg),
    )
    assert _norm(hw._sorted_pools(resumed)) == _norm(hw._sorted_pools(full))
    assert patched_members["n"] - before < 24


def test_resume_one_step_is_cheaper_than_rebuilding_warmup(patched_members):
    grid = _grid(400)
    hcfg = _hcfg()
    t0 = time.perf_counter()
    by_pool, open_start, open_dc, open_end = hw._build_lifetimes_state(
        grid[:-1], {}, None, "db", hcfg, 5
    )
    full_ms = (time.perf_counter() - t0) * 1000
    patched_members["n"] = 0
    t1 = time.perf_counter()
    hw.resume_open_lifetimes(
        by_pool,
        open_start,
        open_dc,
        open_end,
        grid[-1:],
        5,
        lambda dc: hw._load_members({}, dc, None, "db", hcfg),
    )
    delta_ms = (time.perf_counter() - t1) * 1000
    assert patched_members["n"] == 1
    assert delta_ms < full_ms


def test_cold_window_child_is_a_different_process():
    out = ping_child()
    assert out["ok"] is True
    assert out["pid"] != os.getpid()
