"""Causal visible window bounds for build_landscape_window (not chart viewport)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONTRACT_PATH = Path(__file__).resolve().parent / "density_contract_v1.json"
_CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

WARMUP_HOURS = int(_CONTRACT["warmup_hours"])
MAX_EXPORT_HOURS = float(_CONTRACT["max_export_hours"])
VISIBLE_CONTEXT_HOURS = float(_CONTRACT["visible_context_hours"])
SLOT_MINUTES = 5


def _parse_utc(s: str) -> datetime:
    s = str(s).strip().replace(" ", "T")
    if not s.endswith("Z"):
        s += "Z"
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def assert_on_5m_decision_close(decision_close_utc: str) -> datetime:
    dt = _parse_utc(decision_close_utc)
    if dt.second or dt.microsecond or (dt.minute % SLOT_MINUTES) != 0:
        raise ValueError(f"decision_close must align to 5m UTC grid: {decision_close_utc}")
    return dt


def causal_window_bounds(
    decision_close_utc: str,
    *,
    visible_context_hours: float | None = None,
) -> tuple[str, str]:
    """
    Visible SSOT window for one decision close.

    - ``visible_to`` = decision close + one 5m step (matches chart parity export).
    - ``visible_from`` = visible_to − context hours (default 48h, ≤ 72h export cap).
    - 168h warmup is applied inside ``build_landscape_window`` from ``visible_from``.
    """
    t_close = assert_on_5m_decision_close(decision_close_utc)
    ctx_h = visible_context_hours if visible_context_hours is not None else VISIBLE_CONTEXT_HOURS
    if ctx_h > MAX_EXPORT_HOURS:
        raise ValueError(f"visible_context_hours {ctx_h} exceeds MAX_EXPORT_HOURS {MAX_EXPORT_HOURS}")
    vis_to = t_close + timedelta(minutes=SLOT_MINUTES)
    vis_from = vis_to - timedelta(hours=ctx_h)
    span_h = (vis_to - vis_from).total_seconds() / 3600.0
    if span_h > MAX_EXPORT_HOURS + 1e-6:
        raise ValueError("computed window exceeds export limit")
    return _iso(vis_from), _iso(vis_to)


def parity_fixture_bounds(decision_close_utc: str) -> tuple[str, str] | None:
    """Fixed bounds from phase_density_chart_parity export when available."""
    if decision_close_utc == "2026-09-25T11:00:00Z":
        return "2026-09-23T11:00:00Z", "2026-09-25T11:05:00Z"
    return None
