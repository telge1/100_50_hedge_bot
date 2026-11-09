"""L1 V3T — drawing health, persist errors, delete verification."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from drawings.manager import DrawingManager
from drawings.persistence import save_drawings
from drawings.tools import make_trend

from research_charts.drawing_health import compute_drawing_health
from research_charts.workspace_session import (
    DRAWINGS_PATH,
    DrawingPersistError,
    ResearchWorkspace,
    reset_workspace_for_tests,
)


def _trend(did: str, *, price_b: float = 0.09) -> object:
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 10, tzinfo=timezone.utc)
    return make_trend(
        drawing_id=did,
        symbol="DOGEUSDT",
        timestamp_a=t0,
        price_a=0.08,
        timestamp_b=t1,
        price_b=price_b,
        created_on_timeframe="5m",
    )


def test_health_ram_equals_file(tmp_path: Path) -> None:
    mgr = DrawingManager()
    d = _trend("d-test-equal")
    mgr.add_drawing(d)
    path = tmp_path / "drawings.json"
    save_drawings(path, mgr.get_drawings(include_hidden=True))
    h = compute_drawing_health(mgr, path)
    assert h["drift"] is False
    assert h["ram_count"] == 1
    assert h["file_count"] == 1
    assert h["ram_sha256"] == h["file_sha256"]
    assert h["ids_only_in_ram"] == []
    assert h["ids_only_in_file"] == []


def test_health_id_only_in_ram(tmp_path: Path) -> None:
    mgr = DrawingManager()
    mgr.add_drawing(_trend("d-ram-only"))
    path = tmp_path / "drawings.json"
    save_drawings(path, [])
    h = compute_drawing_health(mgr, path)
    assert h["drift"] is True
    assert h["ids_only_in_ram"] == ["d-ram-only"]
    assert h["ids_only_in_file"] == []


def test_health_id_only_in_file(tmp_path: Path) -> None:
    mgr = DrawingManager()
    d = _trend("d-file-only")
    path = tmp_path / "drawings.json"
    save_drawings(path, [d])
    h = compute_drawing_health(mgr, path)
    assert h["drift"] is True
    assert h["ids_only_in_file"] == ["d-file-only"]
    assert h["ids_only_in_ram"] == []


def test_health_anchor_diff(tmp_path: Path) -> None:
    mgr = DrawingManager()
    mgr.add_drawing(_trend("d-diff", price_b=0.091))
    path = tmp_path / "drawings.json"
    save_drawings(path, [_trend("d-diff", price_b=0.092)])
    h = compute_drawing_health(mgr, path)
    assert h["drift"] is True
    assert len(h["ids_with_content_diff"]) == 1
    assert h["ids_with_content_diff"][0]["drawing_id"] == "d-diff"


def test_persist_oserror_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ws = reset_workspace_for_tests()
    ws._selected_drawing_id = None

    def _boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(ws, "_trp", {**ws._trp, "save_drawings": _boom})
    with pytest.raises(DrawingPersistError):
        ws.persist_drawings()


def test_delete_rollback_on_persist_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    ws = reset_workspace_for_tests()
    d = _trend("d-delete-me")
    ws.drawings.add_drawing(d)
    ws._selected_drawing_id = "d-delete-me"

    def _boom(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(ws, "_trp", {**ws._trp, "save_drawings": _boom})
    snap = ws.delete_selected()
    assert snap["drawing_delete"]["success"] is False
    assert snap["drawing_delete"]["deleted_id"] == "d-delete-me"
    assert "d-delete-me" in ws.drawings.ids()


def test_delete_success_reports_id(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ws = reset_workspace_for_tests()
    path = tmp_path / "drawings.json"

    def _save(p, drawings, default_style=None):
        save_drawings(path, drawings, default_style=default_style)

    monkeypatch.setattr(ws, "_trp", {**ws._trp, "save_drawings": _save})
    d = _trend("d-gone")
    ws.drawings.add_drawing(d)
    ws._selected_drawing_id = "d-gone"
    snap = ws.delete_selected()
    assert snap["drawing_delete"]["success"] is True
    assert snap["drawing_delete"]["deleted_id"] == "d-gone"
    assert "d-gone" not in ws.drawings.ids()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    ids = [x["drawing_id"] for x in on_disk["drawings"]]
    assert "d-gone" not in ids


def test_health_endpoint_registered_with_auth() -> None:
    from research_charts.api import build_router

    def _auth():
        return {"id": "t"}

    router = build_router(require_auth=_auth, render_template=lambda _n, _c: "")
    paths = {getattr(route, "path", None) for route in router.routes}
    assert "/api/research/drawings/health" in paths
    health_route = next(r for r in router.routes if getattr(r, "path", None) == "/api/research/drawings/health")
    assert "GET" in set(getattr(health_route, "methods", []) or [])
    deps = [d.call for d in (getattr(health_route, "dependant", None).dependencies or [])]
    assert _auth in deps

    src = Path(__file__).resolve().parents[1] / "api.py"
    assert "api_research_drawings_health" in src.read_text(encoding="utf-8")


def test_health_live_workspace_vs_file() -> None:
    reset_workspace_for_tests()
    from research_charts.workspace_session import get_workspace

    h = compute_drawing_health(get_workspace().drawings, DRAWINGS_PATH)
    assert "drift" in h
    assert h["drawings_path"] == str(DRAWINGS_PATH)
    assert isinstance(h["ram_drawing_ids"], list)
    assert "d-ac60572bfc69" in h["file_drawing_ids"]
