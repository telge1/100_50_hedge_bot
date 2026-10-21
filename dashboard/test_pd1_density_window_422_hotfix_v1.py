"""Phase 6XT — window API 422 hotfix (visible span vs server warmup)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

HARNESS = Path(
    "/home/telgenbuescher/projects/pools+ob+delta_bot/logs/historical_import/"
    "overnight_2026/phase6xt_density_window_422_hotfix_v1/window_422_harness.js"
)
DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
ART = HARNESS.parent


def test_window_harness_replay_goto_within_72h():
    proc = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    assert body["version"] == "phase6xt-exact-origin-canvas-planned-hotfix-v1"
    assert body["replay_goto_sep"]["ok"] is True
    assert body["within_72h_export_limit"] is True
    assert body["post_body"]["from_utc"] == "2026-09-14T21:45:00Z"
    assert body["post_body"]["to_utc"] == "2026-09-15T11:45:00Z"


def test_build_landscape_window_regression_from_artifacts():
    from pd1_landscape_viewer_v1.historical_window_v1 import LandscapeWindowError, build_landscape_window

    reg = json.loads((ART / "regression_results.json").read_text(encoding="utf-8"))
    w = reg["working_8h"]
    r = build_landscape_window("2026-09-14T21:45:00Z", "2026-09-15T05:45:00Z")
    assert len(r["pools"]) == w["pools"]
    assert r["provenance"]["phase5s_batch_reader_active"] is True
    assert r["provenance"]["remaining"] is False
    assert r["window"]["context_from_utc"] == w["context_from_utc"]

    val = reg["failing_frontend_span"]["validation"]
    try:
        build_landscape_window("2026-09-08T21:45:00Z", "2026-09-15T11:45:00Z")
        raise AssertionError("expected LandscapeWindowError")
    except LandscapeWindowError as exc:
        assert exc.code == val["code"]
        assert exc.details["requested_hours"] == val["details"]["requested_hours"]

    c = reg["corrected_visible_window"]
    r2 = build_landscape_window("2026-09-14T21:45:00Z", "2026-09-15T11:45:00Z")
    assert len(r2["pools"]) == c["pools"]
    assert r2["window"]["context_from_utc"] == c["context_from_utc"]


def test_density_js_does_not_subtract_client_context():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8")
    assert "hit.from * 1000 - CONTEXT_MS" not in js
    assert "MAX_API_VISIBLE_MS" in js
    assert "phase6xt-exact-origin-canvas-planned-hotfix-v1" in js
