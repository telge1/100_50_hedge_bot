"""Validate chunk spans against MAX_EXPORT_HOURS."""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS_D3 = Path(__file__).resolve().parents[1] / "phase_bt_d3_canonical_depth2_provider_v1"
if str(TOOLS_D3) not in sys.path:
    sys.path.insert(0, str(TOOLS_D3))

from window_policy_v1 import MAX_EXPORT_HOURS, causal_window_bounds, _parse_utc  # noqa: E402


def chunk_visible_span_hours(closes: list[str]) -> float:
    if not closes:
        return 0.0
    vis_lo, _ = causal_window_bounds(closes[0])
    _, vis_hi = causal_window_bounds(closes[-1])
    return (_parse_utc(vis_hi) - _parse_utc(vis_lo)).total_seconds() / 3600.0


def assert_chunks_export_safe(chunks: list[list[str]]) -> list[dict]:
    rows: list[dict] = []
    for i, ch in enumerate(chunks):
        span = chunk_visible_span_hours(ch)
        ok = span <= MAX_EXPORT_HOURS + 1e-6
        rows.append(
            {
                "chunk_index": i,
                "closes": len(ch),
                "first": ch[0],
                "last": ch[-1],
                "visible_span_hours": round(span, 4),
                "export_safe": ok,
            }
        )
        if not ok:
            raise ValueError(f"chunk {i} span {span}h exceeds MAX_EXPORT_HOURS {MAX_EXPORT_HOURS}")
    return rows
