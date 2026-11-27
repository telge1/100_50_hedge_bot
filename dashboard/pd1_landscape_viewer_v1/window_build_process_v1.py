"""Cold PD1 window builds run in a short-lived interpreter.

Waiting on that process releases the GIL, so forming-bar and orderbook
handlers keep running. The child does not import the dashboard main module.
A following 5m step uses the in-process lifetime cursor and does not spawn.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

_DASHBOARD_ROOT = str(Path(__file__).resolve().parents[1])


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    parts = [p for p in sys.path if p]
    prev = env.get("PYTHONPATH", "")
    if prev:
        parts.append(prev)
    # Keep the dashboard root first so the child imports this tree.
    env["PYTHONPATH"] = os.pathsep.join([_DASHBOARD_ROOT, *parts])
    return env


def ping_child() -> dict[str, Any]:
    proc = subprocess.run(
        [sys.executable, "-m", "pd1_landscape_viewer_v1.window_build_subprocess_v1", "--ping"],
        cwd=_DASHBOARD_ROOT,
        env=_child_env(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])
    return json.loads(proc.stdout)


def run_cold_window(kwargs: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in kwargs.items() if k != "cache_store"}
    body["cache_store"] = False
    proc = subprocess.run(
        [sys.executable, "-m", "pd1_landscape_viewer_v1.window_build_subprocess_v1"],
        cwd=_DASHBOARD_ROOT,
        env=_child_env(),
        input=json.dumps(body),
        text=True,
        capture_output=True,
        timeout=240,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-4000:] or "cold window process failed")
    payload = json.loads(proc.stdout)
    if not isinstance(payload, dict):
        raise RuntimeError("cold window process returned a non-object")
    return payload
