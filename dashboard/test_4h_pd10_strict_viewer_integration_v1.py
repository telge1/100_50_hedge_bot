"""Phase 6XT: 4h PD10 strict exact-origin window integration (read-only CH)."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
VIS_FROM = "2026-09-16T00:00:00Z"
VIS_TO = "2026-09-17T20:00:00Z"
CTX_FROM = "2026-09-09T00:00:00Z"
LINE_4H_PD10 = "historical_mixed_frozen_doge_apr_sep_2026_v1_4hpd10_v1"
HASH_PD10 = "bd9ac4f30e177321b688f5adb0fe2681ebb4966d51701e3b323498bfcd0d06c2"
HASH_15M_PD1 = "4aeb36fe22ef3ab24fc65c8e7bc33b6ca499dd51759d07936c96fee781c641c9"


def _py_env():
    import os

    env = {**os.environ}
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


def _run_snippet(code: str, timeout: int = 240) -> dict:
    proc = subprocess.run(
        [str(DASH.parent / ".venv/bin/python"), "-c", code],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_py_env(),
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.timeout(240)
def test_4h_pd10_pilot_window_membership_and_line():
    body = _run_snippet(
        f"""
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
from pd1_landscape_viewer_v1.historical_window_batch_v1 import unique_decision_closes_between
from datetime import datetime, timezone
from research_charts.historical_lmv2_pd_contract_v1 import membership_slot_minutes

d=build_landscape_window("{VIS_FROM}", "{VIS_TO}", timeframe="4h", pool_detail=10)
prov=d["provenance"]
win=d.get("window") or {{}}
ctx=win.get("context_from_utc") or prov.get("context_from")
slot=membership_slot_minutes("4h")
ctx_from=datetime.fromisoformat(ctx.replace("Z","+00:00"))
vis_to=datetime.fromisoformat("{VIS_TO}".replace("Z","+00:00"))
closes=unique_decision_closes_between(ctx_from, vis_to, slot_minutes=slot)
vis_closes=[c for c in closes if datetime.fromisoformat("{VIS_FROM}".replace("Z","+00:00")) <= c <= vis_to]
warmup=[c for c in closes if c < datetime.fromisoformat("{VIS_FROM}".replace("Z","+00:00"))]
fp=prov.get("config_hash")
print(json.dumps({{
  "n_prices": len(d["prices"]),
  "line": prov.get("pipeline_line_id"),
  "tf": prov.get("timeframe"),
  "slot": prov.get("membership_slot_minutes"),
  "context_from": ctx,
  "pools": len(d.get("pools") or []),
  "visible_closes": len(vis_closes),
  "warmup_closes": len(warmup),
  "total_closes": len(closes),
  "hash": fp,
  "phase5s": prov.get("phase5s_batch_reader_active"),
}}))
"""
    )
    assert body["line"] == LINE_4H_PD10
    assert body["tf"] == "4h"
    assert body["slot"] == 240
    assert body["context_from"] == CTX_FROM
    assert body["hash"] == HASH_PD10
    assert body["visible_closes"] == 12
    assert body["warmup_closes"] == 42
    assert body["total_closes"] == 54
    assert body["n_prices"] > 0
    assert body["pools"] > 0
    assert body["phase5s"] is True


@pytest.mark.timeout(120)
def test_15m_pd1_regression_fingerprint():
    body = _run_snippet(
        """
import json, os
os.environ["HISTORICAL_LMV2_BATCH_READER"]="1"
from pd1_landscape_viewer_v1.historical_window_v1 import build_landscape_window
d=build_landscape_window("2026-09-14T21:45:00Z","2026-09-15T05:45:00Z")
prov=d["provenance"]
print(json.dumps({"n": len(d["prices"]), "line": prov.get("pipeline_line_id"), "hash": prov.get("config_hash")}))
"""
    )
    assert body["n"] == 97
    assert body["line"].endswith("15mpd1_v1")
    assert body["hash"] == HASH_15M_PD1


@pytest.mark.timeout(120)
def test_invalid_scopes_still_422():
    from pd1_landscape_viewer_v1.historical_window_v1 import LandscapeWindowError, build_landscape_window

    for tf, pd in (("4h", 1), ("4h", 5)):
        with pytest.raises(LandscapeWindowError) as exc:
            build_landscape_window(VIS_FROM, VIS_TO, timeframe=tf, pool_detail=pd)
        assert exc.value.code == "INVALID_SCOPE"


def test_js_4h_pd10_scope_matrix():
    js = (DASH / "static/js/research/pd1_exact_origin_density_research_v1.js").read_text(encoding="utf-8")
    adapter = (DASH / "static/js/research/pd1_exact_density_trp_adapter.js").read_text(encoding="utf-8")
    assert "phase6xt-exact-origin-canvas-planned-hotfix-v1" in js
    assert '{ timeframe: "4h", pool_detail: 10 }' in js
    assert 'if (s === "4h") return 240' in adapter
    assert "14400" in js
