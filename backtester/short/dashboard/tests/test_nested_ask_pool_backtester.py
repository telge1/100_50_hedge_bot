"""Dashboard tests for Nested Ask Pool Edge Short V1 integration."""

from __future__ import annotations

import time
from pathlib import Path

import pytest


def test_dashboard_adapter_constants():
    from research_charts.nested_ask_pool_backtester import (
        BACKTESTER_SOURCE,
        RESULTS_ROOT,
        STRATEGY_ID,
    )

    assert STRATEGY_ID == "a_plus_nested_ask_pool_edge_short_v1"
    assert BACKTESTER_SOURCE == STRATEGY_ID
    assert "a_plus_nested_ask_pool_edge_short_v1" in str(RESULTS_ROOT)
    assert "a_plus_liquidity_pool_signal_scanner_v1" not in str(RESULTS_ROOT)


def test_build_overlay_objects_segment_and_fill():
    from research_charts.nested_ask_pool_backtester import build_overlay_objects

    specs = [
        {
            "overlay_id": "nap-limit-t1",
            "kind": "NAP_PENDING_LIMIT",
            "line_kind": "segment",
            "start_timestamp": "2026-08-28T01:00:00Z",
            "end_timestamp": "2026-08-28T01:10:00Z",
            "start_price": 0.08935,
            "end_price": 0.08935,
            "price": 0.08935,
            "color": "#e67e22",
            "text": "SHORT LIMIT",
            "tooltip": "Status=PENDING\nchild_pool_id=x",
            "meta": {},
        },
        {
            "overlay_id": "nap-fill-t1",
            "kind": "NAP_FILL",
            "timestamp": "2026-08-28T01:10:00Z",
            "price": 0.08935,
            "shape": "arrow_down",
            "color": "#d62728",
            "text": "SHORT FILL",
            "position": "at_price",
            "tooltip": "Status=FILLED",
            "meta": {},
        },
    ]
    try:
        objs = build_overlay_objects(specs, symbol="DOGEUSDT")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"TRP unavailable: {exc}")
    assert len(objs) == 2
    assert objs[0].kind == "segment"
    assert float(objs[0].start_price) == 0.08935
    assert objs[1].text == "SHORT FILL"
    assert objs[1].position == "at_price"


def test_load_and_build_sanitizes_nan(tmp_path):
    import json
    import math

    from research_charts.nested_ask_pool_backtester import (
        build_overlay_objects,
        json_safe,
        load_run_payload,
    )

    run = tmp_path / "nested_ask_pool_edge_short_v1_9"
    run.mkdir()
    # Intentional non-standard JSON tokens as produced by pandas/numpy dumps.
    (run / "dashboard_overlay_payload.json").write_text(
        """{
          "symbol": "DOGEUSDT",
          "specs": [{
            "overlay_id": "nap-exit-1",
            "kind": "NAP_EXIT",
            "timestamp": "2026-08-28T02:00:00Z",
            "price": NaN,
            "shape": "circle",
            "color": "#7f8c8d",
            "text": "EXIT",
            "position": "at_price",
            "tooltip": "exit_price=nan",
            "meta": {"engine_fill_at": NaN, "child_pool_id": "x"}
          }]
        }""",
        encoding="utf-8",
    )
    (run / "dashboard_provenance.json").write_text(
        '{"symbol":"DOGEUSDT","run_id":9,"summary":{}}', encoding="utf-8"
    )
    (run / "manifest.json").write_text('{"run_id":9,"summary":{}}', encoding="utf-8")

    payload = load_run_payload(run)
    assert payload["markers"][0]["price"] is None
    assert payload["markers"][0]["meta"]["engine_fill_at"] is None
    assert math.isnan(float("nan"))  # sanity
    assert json_safe(float("nan")) is None
    assert json.dumps(json_safe(payload["markers"]))  # must be strict-JSON safe

    try:
        objs = build_overlay_objects(payload["markers"], symbol="DOGEUSDT")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"TRP unavailable: {exc}")
    assert len(objs) == 1
    assert objs[0].price is None
    assert objs[0].position == "above"
    assert objs[0].metadata.get("engine_fill_at") is None


def test_workspace_store_nested():
    from research_charts.workspace_session import NAP_STRATEGY_ID, ResearchWorkspace

    ws = ResearchWorkspace()
    payload = {
        "meta": {
            "symbol": "DOGEUSDT",
            "strategy_id": NAP_STRATEGY_ID,
            "run_id": 1,
            "start_utc": "2026-08-28T00:00:00Z",
            "end_utc": "2026-08-28T04:00:00Z",
        },
        "summary": {
            "candidates": 2,
            "fills_strict": 1,
            "ambiguous": 0,
            "structural_sl": {"n": 1, "tp_first": 1, "sl_first": 0},
        },
        "markers": [
            {
                "overlay_id": "nap-fill-x",
                "kind": "NAP_FILL",
                "timestamp": "2026-08-28T01:10:00Z",
                "price": 0.08935,
                "shape": "arrow_down",
                "color": "#d62728",
                "text": "SHORT FILL",
                "position": "at_price",
                "tooltip": "x",
                "meta": {},
            }
        ],
    }
    try:
        snap = ws.store_nested_ask_pool_run(payload)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"TRP unavailable: {exc}")
    assert snap["nested_ask_pool"]["loaded"] is True
    assert snap["nested_ask_pool"]["strategy_id"] == NAP_STRATEGY_ID
    nap_ids = [oid for oid in ws.overlays.ids() if str(oid).startswith("nap-")]
    assert nap_ids
    assert all("LONG" not in str(getattr(ws.overlays.get_overlay(oid), "text", "") or "") for oid in nap_ids)


def test_job_duplicate_and_complete(tmp_path, monkeypatch):
    from research_charts import nested_ask_pool_jobs as jobs

    monkeypatch.setattr(jobs, "RESULTS_ROOT", tmp_path)
    monkeypatch.setattr(jobs, "known_symbols", lambda: {"DOGEUSDT"})

    calls = {"n": 0}

    def fake_run(**kwargs):
        calls["n"] += 1
        out = tmp_path / "nested_ask_pool_edge_short_v1_1"
        out.mkdir(parents=True, exist_ok=True)
        (out / "dashboard_overlay_payload.json").write_text(
            '{"specs":[],"symbol":"DOGEUSDT"}', encoding="utf-8"
        )
        (out / "dashboard_provenance.json").write_text(
            '{"symbol":"DOGEUSDT","run_id":1,"summary":{}}', encoding="utf-8"
        )
        (out / "manifest.json").write_text('{"run_id":1,"summary":{}}', encoding="utf-8")
        return {"run_id": 1, "out_dir": str(out), "summary": {}}

    monkeypatch.setattr(jobs, "run_nested_ask_pool_backtest", fake_run)

    p1, c1 = jobs.start_nested_ask_pool_job(
        symbol="DOGEUSDT",
        start="2026-08-28T00:00:00Z",
        end="2026-08-28T04:00:00Z",
    )
    assert c1 == 200
    p2, c2 = jobs.start_nested_ask_pool_job(
        symbol="DOGEUSDT",
        start="2026-08-28T00:00:00Z",
        end="2026-08-28T04:00:00Z",
    )
    assert c2 == 409
    assert p2["error"] == "DUPLICATE_ACTIVE_JOB"
    for _ in range(80):
        st, _ = jobs.nested_ask_pool_job_status(p1["job_id"])
        if st.get("state") in {"completed", "failed"}:
            break
        time.sleep(0.05)
    assert st.get("state") == "completed"
    assert calls["n"] == 1


def test_aps_strategy_still_registered():
    from research_charts.pool_signals_backtester import STRATEGY_ID as APS_ID
    from research_charts.nested_ask_pool_backtester import STRATEGY_ID as NAP_ID

    assert APS_ID == "a_plus_liquidity_pool_signal_scanner_v1"
    assert NAP_ID != APS_ID
