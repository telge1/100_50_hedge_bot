"""1h on-demand lifetimes: segment start must not precede causal DC (as-of)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from pd1_landscape_viewer_v1.on_demand_1hpd1_v1 import build_landscape_window_on_demand_1hpd1_v1
from pd1_landscape_viewer_v1.historical_window_v1 import _causal_decision_close, _parse_utc
from research_charts.short_pool_oracle_v1 import parse_decision_iso


@pytest.mark.timeout(120)
def test_1h_segment_starts_on_hourly_dc_not_grid_win_start():
    d = build_landscape_window_on_demand_1hpd1_v1(
        "2026-08-26T12:00:00Z",
        "2026-08-28T12:00:00Z",
        compute_mode="exact_origin_v1",
    )
    violations = []
    for pl in d.get("pools") or []:
        ka = pl.get("known_at")
        birth = parse_decision_iso(str(ka)) if ka else None
        if birth is None:
            m = re.search(r":(\d{9,11})$", pl["pool_id"])
            if m:
                birth = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        for seg in pl.get("segments") or []:
            s0 = parse_decision_iso(seg[0])
            if birth and s0 < birth:
                violations.append((pl["pool_id"], birth.isoformat(), seg[0]))
            # 1h slot: ACTIVE segment starts must be on DC boundary
            dc = _causal_decision_close(s0, 60)
            if dc != s0:
                violations.append((pl["pool_id"], "dc_misalign", seg[0]))
    assert not violations, violations[:5]


@pytest.mark.timeout(120)
def test_first_segment_not_before_known_at_for_lowers():
    d = build_landscape_window_on_demand_1hpd1_v1(
        "2026-08-26T00:00:00Z",
        "2026-08-29T00:00:00Z",
        compute_mode="exact_origin_v1",
    )
    for pl in d.get("pools") or []:
        if pl.get("side") != "lower" or not pl.get("known_at"):
            continue
        birth = parse_decision_iso(str(pl["known_at"]))
        for seg in pl.get("segments") or []:
            s0 = parse_decision_iso(seg[0])
            assert s0 >= birth, (pl["pool_id"], seg, pl["known_at"])
