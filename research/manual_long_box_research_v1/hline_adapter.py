"""Map manual long box to horizontal LineDef for V3AN touch geometry."""

from __future__ import annotations

from .l1_imports import bootstrap_l1_watcher_paths
from .models import LongBoxSetup


def long_box_to_hline(setup: LongBoxSetup):
    """Constant entry level as hline — same interface as manual support hline drawings."""
    bootstrap_l1_watcher_paths()
    from line_geometry import LineDef

    t0 = float(setup.start_ts)
    t1 = float(max(setup.end_ts, setup.start_ts))
    return LineDef(
        drawing_id=f"mlb2-hline-{setup.drawing_id}",
        drawing_type="hline",
        symbol=setup.symbol,
        extend_right=True,
        price=float(setup.entry_price),
        t1=t0,
        p1=float(setup.entry_price),
        t2=t1,
        p2=float(setup.entry_price),
    )
