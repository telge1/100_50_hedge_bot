"""Phase 6XT — Exact Origin merge joins only touching or overlapping columns."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

DASH = Path(__file__).resolve().parent
HARNESS = DASH / "test_exact_origin_density_merge_gap_fix_v1.js"
PILOT = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_5m_multi_pd_render_completeness_audit_v1/window_pools_pilot.json"
)
ART = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_exact_origin_density_merge_gap_fix_v1"
)
LO_15M = "2026-04-01T00:00:00Z"
HI_15M = "2026-09-30T23:45:00Z"
WINDOWS_15M = (
    ("validated_sep", "2026-09-16T00:00:00Z", "2026-09-17T23:55:00Z"),
    ("corpus_start", "2026-04-01T00:00:00Z", "2026-04-02T23:45:00Z"),
    ("month_boundary", "2026-06-30T12:00:00Z", "2026-07-01T12:00:00Z"),
    ("corpus_end", "2026-09-29T00:00:00Z", "2026-09-30T23:45:00Z"),
    ("regression_97", "2026-09-14T21:45:00Z", "2026-09-15T05:45:00Z"),
)


def _node(*args: str) -> dict:
    proc = subprocess.run(
        ["node", str(HARNESS), *args],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-4000:] + proc.stdout[-2000:]
    return json.loads(proc.stdout)


def test_synthetic_gap_touch_and_overlap():
    body = _node("synthetic")
    assert body["five_m_gap_production_min3"] == [
        {"depth": 5, "start": 0, "end": 300},
        {"depth": 5, "start": 600, "end": 900},
    ]
    assert body["five_m_gap_legacy_min3"] == [{"depth": 5, "start": 0, "end": 900}]
    assert body["touching"] == [{"depth": 5, "start": 0, "end": 600}]
    assert body["overlap"] == [{"depth": 5, "start": 0, "end": 600}]
    assert body["fifteen_m_gap_production_min3"] == body["fifteen_m_gap_legacy_min3"]
    assert body["fifteen_m_gap_production_min3"] == [
        {"depth": 5, "start": 0, "end": 900},
        {"depth": 5, "start": 1800, "end": 2700},
    ]


def test_5m_pilot_counts_and_multi_pd_inclusion():
    body = _node("pilot", str(PILOT))
    expect_legacy = {
        "1": {"raw": 150685, "merged": 2080, "filters": {"1": 2080, "2": 1473, "3": 916, "5": 287, "10": 2}},
        "5": {"raw": 100390, "merged": 1201, "filters": {"1": 1201, "2": 556, "3": 228, "5": 40, "10": 0}},
        "10": {"raw": 85981, "merged": 876, "filters": {"1": 876, "2": 205, "3": 39, "5": 2, "10": 0}},
    }
    expect_prod = {
        "1": {"merged": 2091, "filters": {"1": 2091, "2": 1480, "3": 920, "5": 287, "10": 2}},
        "5": {"merged": 1206, "filters": {"1": 1206, "2": 556, "3": 228, "5": 40, "10": 0}},
        "10": {"merged": 876, "filters": {"1": 876, "2": 205, "3": 39, "5": 2, "10": 0}},
    }
    for pd, exp in expect_legacy.items():
        row = body["singles"][pd]
        assert row["raw"] == exp["raw"]
        assert row["culled"] == row["raw"]
        assert row["pools_missing_origin"] == 0
        assert 0 < row["origins"] <= row["pools"]
        assert row["legacy"]["merged"] == exp["merged"]
        assert row["production"]["holes"] == 0
        for n, count in exp["filters"].items():
            assert row["legacy"]["filters"][n]["n"] == count
            got = row["production"]["filters"][n]
            assert got["n"] == expect_prod[pd]["filters"][n]
            assert got["upper"] + got["lower"] == got["n"]
        assert row["production"]["merged"] == expect_prod[pd]["merged"]
        if row["bridges"] == 0:
            assert row["production"]["sha256"] == row["legacy"]["sha256"]
            assert row["legacy"]["holes"] == 0
        else:
            assert row["production"]["sha256"] != row["legacy"]["sha256"]
            assert row["legacy"]["holes"] > 0
            assert row["production"]["merged"] == row["legacy"]["merged"] + row["bridges"]
    assert body["singles"]["1"]["bridges"] == 11
    assert body["singles"]["5"]["bridges"] == 5
    assert body["singles"]["10"]["bridges"] == 0
    multi = body["multi"]
    assert multi["concat"] == multi["sum_singles"] == 4173
    assert multi["cross_merge_smaller"] is True
    assert multi["cross_merge_count"] < multi["concat"]
    for n, row in multi["inclusion"].items():
        assert row["pd1"]["missing"] == 0
        assert row["pd5"]["missing"] == 0
        assert row["pd10"]["missing"] == 0
        assert row["pd1"]["n"] + row["pd5"]["n"] + row["pd10"]["n"] == multi["filters"][n]["n"]
    ART.mkdir(parents=True, exist_ok=True)
    (ART / "pilot_5m_before_after.json").write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def _py_env():
    import os

    env = dict(os.environ)
    env["PYTHONPATH"] = ":".join(
        [
            str(DASH.parent),
            str(DASH),
            "/home/telgenbuescher/projects/pools+ob+delta_bot",
            "/home/telgenbuescher/projects/trading_research_platform",
        ]
    )
    env["HISTORICAL_LMV2_BATCH_READER"] = "1"
    return env


@pytest.mark.timeout(420)
def test_15m_pd1_geometry_unchanged_on_several_windows():
    ART.mkdir(parents=True, exist_ok=True)
    rows = []
    for label, start, end in WINDOWS_15M:
        proc = subprocess.run(
            [
                str(DASH.parent / ".venv/bin/python"),
                "-c",
                f"""
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
d = build_landscape_window("{start}", "{end}", timeframe="15m", pool_detail=1)
pools = [{{
    "pool_id": p.get("pool_id"),
    "origin": p.get("origin"),
    "side": p.get("side"),
    "bottom": p.get("bottom"),
    "top": p.get("top"),
    "segments": p.get("segments") or [],
}} for p in d["pools"]]
print(json.dumps({{
    "n_prices": len(d["prices"]),
    "n_pools": len(pools),
    "line": d["provenance"].get("pipeline_line_id"),
    "slot": d["provenance"].get("membership_slot_minutes"),
    "pools": pools,
}}))
""",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            env=_py_env(),
            cwd=str(DASH),
        )
        assert proc.returncode == 0, proc.stderr[-4000:]
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["line"] == "historical_mixed_frozen_doge_apr_sep_2026_v1_15mpd1_v1"
        assert payload["slot"] == 15
        job = {
            "label": label,
            "from_utc": start,
            "to_utc": end,
            "lo_utc": LO_15M,
            "hi_utc": HI_15M,
            "slot": 15,
            "pools": payload["pools"],
        }
        job_path = ART / f"window_{label}.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        stats = _node("window", str(job_path))
        assert stats["bridges"] == 0, label
        assert stats["production"]["sha256"] == stats["legacy"]["sha256"], label
        assert stats["production"]["holes"] == 0
        assert stats["legacy"]["holes"] == 0
        for n in ("1", "2", "3", "5", "10"):
            assert stats["production"]["filters"][n] == stats["legacy"]["filters"][n], (label, n)
        rows.append(
            {
                "label": label,
                "from_utc": start,
                "to_utc": end,
                "n_prices": payload["n_prices"],
                "n_pools": payload["n_pools"],
                "raw": stats["raw"],
                "merged": stats["production"]["merged"],
                "sha256": stats["production"]["sha256"],
                "filters": {n: stats["production"]["filters"][n]["n"] for n in ("1", "2", "3", "5", "10")},
            }
        )
        job_path.unlink()
    (ART / "15m_pd1_windows.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
