"""BT-D32 — PD1 window API must not block the asyncio event loop."""

from __future__ import annotations

import inspect
from pathlib import Path


def test_pd1_viewer_window_handler_uses_to_thread():
    api_path = Path(__file__).resolve().parent / "pd1_landscape_viewer_v1" / "api.py"
    src = api_path.read_text(encoding="utf-8")
    assert "await asyncio.to_thread(" in src
    assert "get_landscape_window_response" in src
    # Ensure handler remains async (offload pattern).
    from pd1_landscape_viewer_v1.api import build_router

    router = build_router(require_auth=lambda: {}, render_template=lambda n, c: "")
    routes = {r.path: r.endpoint for r in router.routes}
    assert inspect.iscoroutinefunction(routes["/live-charts/viewer/api/window"])
