"""Published timeline vs causal density window (BT-D84 gate)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (
    ROOT / "tools/phase_bt_d3_canonical_depth2_provider_v1",
    ROOT / "tools/phase_bt_d76_published_ch_binding_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from import_bootstrap_v1 import load_pools_landscape_window_module  # noqa: E402
from window_policy_v1 import causal_window_bounds  # noqa: E402


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def published_import_bounds() -> tuple[datetime, datetime]:
    lw = load_pools_landscape_window_module()
    hcfg = lw.resolve_viewer_historical_scope(timeframe="5m", pool_detail=1)
    published = lw._published_for(hcfg)
    from research_charts.historical_pool_state_v1 import static_timeline_import_bounds

    return static_timeline_import_bounds(published)


def closes_with_density_scope_violation(closes: list[str]) -> list[str]:
    """Decision closes whose causal visible_to extends past published import hi."""
    _, data_hi = published_import_bounds()
    bad: list[str] = []
    for c in closes:
        _vis_from, vis_to = causal_window_bounds(c)
        if _parse_utc(vis_to) > data_hi:
            bad.append(c)
    return bad


def validate_full_window_density(closes: list[str], *, expected_count: int) -> dict:
    bad = closes_with_density_scope_violation(closes)
    lo, hi = published_import_bounds()
    return {
        "ok": len(closes) == expected_count and not bad,
        "expected_closes": expected_count,
        "actual_closes": len(closes),
        "published_data_lo_utc": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "published_data_hi_utc": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "density_scope_violations": bad,
        "density_scope_violation_count": len(bad),
        "last_supported_close_rule": (
            "causal visible_to must be <= published_data_hi; "
            "typically limits last September close to 2026-09-30T23:40:00Z (8637 closes)"
        ),
    }
