"""Research Charts async EZM wiring (start/poll/import + markers)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ui_has_ezm_option_and_run_route():
    html = (ROOT / "templates" / "research_charts.html").read_text(encoding="utf-8")
    js = (ROOT / "static" / "js" / "research" / "research_charts.js").read_text(encoding="utf-8")
    api = (ROOT / "research_charts" / "api.py").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "research.css").read_text(encoding="utf-8")
    assert 'value="ema_zone_microstructure_confirmation_v1"' in html
    assert "EMA Zone Microstructure Confirmation V1" in html
    assert 'id="researchEzmLayerMode"' in html
    assert 'id="researchEzmComputationMode"' in html
    assert "Prüfung" in html
    assert "EMA plus Orderbuch" in html
    assert "Nur EMA" in html
    assert "Nur Mikrostruktur" in html
    assert "EMA und Mikrostruktur" in html
    assert 'id="researchEzmLegend"' in html
    assert "research-ezm-legend" in css
    assert "runEzmCandidateDiscovery" in js
    assert "ezmComputationMode" in js
    assert "computation_mode" in js
    assert "applyEzmLayerMode" in js
    assert "syncEzmLayerUi" in js
    assert js.index("function syncEzmLayerUi") < js.index("function applyWorkspace(snap)")
    assert "layer_only" in js
    assert "/api/research/ezm/run" in js
    assert "/api/research/ezm/status" in js
    assert "/api/research/ezm/import" in js
    assert "ema_zone_microstructure_confirmation_v1" in api
    assert "start_ezm_research_job" in api
    assert "import_ezm_job_to_workspace" in api
    assert "layer_only" in api


def test_edc_csw_run_paths_unchanged():
    api = (ROOT / "research_charts" / "api.py").read_text(encoding="utf-8")
    assert "run_ema_dual_cross_backtest" in api
    assert "run_cluster_sweep_backtest" in api
    assert 'strategy_id in ("ema_dual_cross_multisource_v1", "ema_dual_cross")' in api
    assert 'strategy_id not in ("cluster_sweep_ema_9_20_59", "cluster_sweep")' in api


def test_marker_specs_long_short():
    from research_charts.ezm_backtester import micro_rows_to_marker_specs, setup_rows_to_marker_specs

    setup_specs = setup_rows_to_marker_specs(
        [
            {
                "confirmation_mode": "ema_only",
                "output_layer": "ema_setup",
                "setup_id": "s1",
                "zone_name": "EMA20",
                "zone_role": "resistance",
                "zone_event": "exact_touch",
                "marker_at": "2026-01-02T11:00:00Z",
                "marker_price": 0.12,
                "emit_setup_marker": True,
            }
        ]
    )
    assert len(setup_specs) == 1
    assert setup_specs[0]["kind"] == "EZM_SETUP"
    assert setup_specs[0]["direction"] == "NONE"
    assert setup_specs[0]["shape"] == "diamond"

    rows = [
        {
            "confirmation_mode": "ema_plus_microstructure",
            "output_layer": "microstructure_confirmation",
            "setup_id": "s1",
            "direction": "LONG",
            "candidate_direction": "LONG",
            "reaction_state": "defense_rejection_confirmed",
            "candidate_state": "defense_rejection_confirmed",
            "emit_directional_marker": True,
            "decision_at": "2026-01-02T12:00:00Z",
            "decision_price": 0.12,
            "signal_id": "a",
            "symbol": "DOGEUSDT",
        },
        {
            "confirmation_mode": "ema_plus_microstructure",
            "setup_id": "s2",
            "direction": "SHORT",
            "candidate_direction": "SHORT",
            "reaction_state": "breakout_confirmed",
            "emit_directional_marker": True,
            "decision_at": "2026-01-02T13:00:00Z",
            "decision_price": 0.11,
            "signal_id": "b",
            "symbol": "DOGEUSDT",
        },
        {
            "direction": "LONG",
            "candidate_state": "wait_microstructure_confirmation",
            "decision_at": "2026-01-02T14:00:00Z",
            "decision_price": 0.1,
            "signal_id": "c",
        },
    ]
    specs = micro_rows_to_marker_specs(rows)
    assert len(specs) == 3
    confirmed = [s for s in specs if s["kind"] == "EZM_CONFIRMED"]
    assert len(confirmed) == 2
    assert confirmed[0]["shape"] == "arrow_up" and confirmed[0]["position"] == "below"
    assert confirmed[0]["color"] == "#2ca02c"
    assert confirmed[1]["shape"] == "arrow_down" and confirmed[1]["position"] == "above"
    assert confirmed[1]["color"] == "#d62728"
    wait_specs = [s for s in specs if s["kind"] == "EZM_MICRO_WAIT"]
    assert len(wait_specs) == 1


def test_micro_blocked_keeps_direction_without_marker():
    from research_charts.ezm_backtester import micro_rows_to_marker_specs

    specs = micro_rows_to_marker_specs(
        [
            {
                "confirmation_mode": "ema_plus_microstructure",
                "setup_id": "s3",
                "candidate_direction": "LONG",
                "reaction_state": "breakout_confirmed",
                "emit_directional_marker": False,
                "clearance_status": "next_zone_near",
                "decision_at": "2026-01-02T15:00:00Z",
                "decision_price": 0.13,
            }
        ]
    )
    assert len(specs) == 1
    assert specs[0]["kind"] == "EZM_MICRO_BLOCKED"
    assert specs[0]["direction"] == "LONG"
    assert specs[0]["color"] == "#bcbd22"


def test_parse_window_and_status_not_ezm(tmp_path, monkeypatch):
    from research_charts.ezm_jobs import _parse_pair, ezm_job_status

    start, end = _parse_pair("2026-01-01T00:00", "2026-01-02T00:00")
    assert start.endswith("Z")
    assert end.endswith("Z")

    jobs = tmp_path / "jobs"
    job_id = "a" * 32
    d = jobs / job_id
    d.mkdir(parents=True)
    (d / "request.json").write_text(
        json.dumps(
            {
                "strategy_id": "wave_fade_frozen_f16ae32_causal_entry_v1",
                "signal_start": "2026-01-01T00:00:00Z",
                "signal_end_exclusive": "2026-01-02T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    (d / "status.json").write_text(
        json.dumps({"job_id": job_id, "state": "COMPLETED", "coins": []}),
        encoding="utf-8",
    )
    monkeypatch.setenv("STOCH_FADE_RESEARCH_JOBS_ROOT", str(jobs))
    payload, code = ezm_job_status(job_id, environ={"STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs)})
    assert code == 409
    assert payload["error"] == "NOT_EZM_JOB"


def test_workspace_store_and_toggle(monkeypatch):
    from research_charts.ezm_backtester import STRATEGY_ID, build_overlay_markers
    from research_charts import workspace_session as ws_mod

    class FakeOverlay:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class FakeOverlays:
        def __init__(self):
            self._items = {}

        def ids(self):
            return list(self._items.keys())

        def get_overlay(self, oid):
            return self._items[oid]

        def remove_overlay(self, oid):
            self._items.pop(oid, None)

        def add_overlay(self, ov):
            self._items[ov.overlay_id] = ov

        def __contains__(self, oid):
            return oid in self._items

    ws = object.__new__(ws_mod.ResearchWorkspace)
    ws._trp = {
        "OverlayMarker": FakeOverlay,
        "OverlayStyle": lambda **k: type("S", (), k)(),
        "ensure_utc": lambda dt: dt if getattr(dt, "tzinfo", None) else dt.replace(tzinfo=timezone.utc),
    }
    ws.overlays = FakeOverlays()
    ws._ezm_run = None
    ws._ezm_visible = False
    ws._ezm_layer_mode = "both"
    ws._cluster_sweep_run = None
    ws._cluster_sweep_visible = False
    ws._cluster_sweep_event_index = 0
    ws._ema_dual_cross_run = None
    ws._ema_dual_cross_visible = False
    ws._ema_dual_cross_event_index = 0
    ws.persist_drawings = lambda: None
    ws.snapshot = lambda: {
        "success": True,
        "ezm": ws._ezm_snapshot(),
        "cluster_sweep": {"loaded": False},
        "ema_dual_cross": {"loaded": False},
    }

    payload = {
        "meta": {"symbol": "ETHUSDT", "strategy_id": STRATEGY_ID, "job_id": "j1"},
        "candidates": [
            {
                "confirmation_mode": "ema_plus_microstructure",
                "output_layer": "microstructure_confirmation",
                "setup_id": "s1",
                "candidate_direction": "LONG",
                "reaction_state": "defense_rejection_confirmed",
                "emit_directional_marker": True,
                "decision_at": "2026-01-02T12:00:00Z",
                "decision_price": 100.0,
            }
        ],
        "ema_setup_events": [
            {
                "confirmation_mode": "ema_only",
                "output_layer": "ema_setup",
                "setup_id": "s1",
                "zone_name": "EMA20",
                "zone_role": "resistance",
                "zone_event": "exact_touch",
                "marker_at": "2026-01-02T11:00:00Z",
                "marker_price": 99.5,
                "emit_setup_marker": True,
            }
        ],
        "microstructure_confirmation_events": [
            {
                "confirmation_mode": "ema_plus_microstructure",
                "output_layer": "microstructure_confirmation",
                "setup_id": "s1",
                "candidate_direction": "LONG",
                "reaction_state": "defense_rejection_confirmed",
                "emit_directional_marker": True,
                "decision_at": "2026-01-02T12:00:00Z",
                "decision_price": 100.0,
            }
        ],
        "markers": [],
        "coverage": {},
        "summary": {},
    }
    snap = ws.store_ezm_run(payload)
    assert snap["ezm"]["loaded"] is True
    assert snap["ezm"]["visible"] is False
    assert snap["ezm"]["layer_mode"] == "both"
    assert snap["backtester"]["strategy_id"] == STRATEGY_ID

    # Patch build path used by set_ezm_visible
    monkeypatch.setattr(
        "research_charts.ezm_backtester.build_overlay_markers",
        lambda specs, *, symbol: [
            FakeOverlay(
                overlay_id=s["overlay_id"],
                symbol=symbol,
                metadata={
                    "origin": "ezm_candidate_discovery",
                    "strategy_id": STRATEGY_ID,
                    "layer": s.get("layer"),
                },
            )
            for s in specs
        ],
    )
    snap2 = ws.set_ezm_visible(True, "ETHUSDT")
    assert snap2["ezm"]["visible"] is True
    assert snap2["backtester"]["visible"] is True
    assert len(ws.overlays.ids()) == 2

    snap4 = ws.set_ezm_layer_mode("ema_only", "ETHUSDT")
    assert snap4["ezm"]["layer_mode"] == "ema_only"
    assert len(ws.overlays.ids()) == 1

    snap5 = ws.set_ezm_layer_mode("micro_only", "ETHUSDT")
    assert snap5["ezm"]["layer_mode"] == "micro_only"
    assert len(ws.overlays.ids()) == 1

    snap3 = ws.set_ezm_visible(False, "ETHUSDT")
    assert snap3["ezm"]["visible"] is False
    assert len(ws.overlays.ids()) == 0
    assert callable(build_overlay_markers)


def test_start_ezm_single_symbol_validation(monkeypatch):
    from research_charts import ezm_jobs

    monkeypatch.setattr(ezm_jobs, "known_symbols", lambda: {"ETHUSDT", "SOLUSDT"})

    payload, code = ezm_jobs.start_ezm_research_job(
        symbol="ETHUSDT,SOLUSDT",
        start="2026-01-01T00:00:00Z",
        end="2026-01-02T00:00:00Z",
    )
    assert code == 400
    assert payload["error"] == "single_symbol_required"

    payload, code = ezm_jobs.start_ezm_research_job(
        symbol="ZZZUSDT",
        start="2026-01-01T00:00:00Z",
        end="2026-01-02T00:00:00Z",
    )
    assert code == 404
    assert payload["error"] == "unknown_symbol"

    payload, code = ezm_jobs.start_ezm_research_job(
        symbol="ETHUSDT",
        start="2026-01-01T00:00:00Z",
        end="2026-01-02T00:00:00Z",
        computation_mode="invalid_mode",
    )
    assert code == 400
    assert payload["error"] == "INVALID_COMPUTATION_MODE"


def test_ezm_computation_mode_persisted_in_job_request(tmp_path, monkeypatch):
    from research_charts import ezm_jobs
    from stoch_fade_research_jobs.jobs import job_dir_for

    jobs = tmp_path / "jobs"
    monkeypatch.setenv("STOCH_FADE_RESEARCH_JOBS_ROOT", str(jobs))
    monkeypatch.setenv("STOCH_EZM_RUNNER_STUB", "success")
    monkeypatch.setattr(ezm_jobs, "known_symbols", lambda: {"ETHUSDT"})
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs.coverage_report",
        lambda **kwargs: {"coins": [{"symbol": "ETHUSDT", "testable": True}]},
    )
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs.filter_testable",
        lambda symbols, coins: (symbols, None),
    )
    monkeypatch.setattr("stoch_fade_research_jobs.jobs.active_job_id", lambda environ=None: None)
    monkeypatch.setattr(
        "stoch_fade_research_jobs.jobs._spawn_and_lock",
        lambda job_id, directory, environ=None, spawn=None: ({"success": True, "job_id": job_id}, 200),
    )
    monkeypatch.setattr(
        "stoch_heavy_job_gate.try_acquire",
        lambda owner, job_id, environ=None: (True, None, None),
    )

    payload, code = ezm_jobs.start_ezm_research_job(
        symbol="ETHUSDT",
        start="2026-01-01T00:00:00Z",
        end="2026-01-02T00:00:00Z",
        computation_mode="ema_only",
        environ={"STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs)},
    )
    assert code == 200
    assert payload["computation_mode"] == "ema_only"
    req = json.loads((job_dir_for(payload["job_id"], {"STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs)}) / "request.json").read_text())
    manifest = json.loads((job_dir_for(payload["job_id"], {"STOCH_FADE_RESEARCH_JOBS_ROOT": str(jobs)}) / "job_manifest.json").read_text())
    assert req["computation_mode"] == "ema_only"
    assert manifest["computation_mode"] == "ema_only"
