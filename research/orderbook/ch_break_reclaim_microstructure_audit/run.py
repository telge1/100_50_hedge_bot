"""Orchestrate CH break/reclaim microstructure audit."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from research.orderbook.ch_break_reclaim_microstructure_audit.events import (
    load_ch_covered_events,
    outcomes_table,
)
from research.orderbook.ch_break_reclaim_microstructure_audit.extract import extract_all
from research.orderbook.ch_break_reclaim_microstructure_audit.report import (
    select_deep_dives,
    write_artifacts,
)
from research.orderbook.ch_break_reclaim_microstructure_audit.stats import (
    compute_group_statistics,
    compute_timepoint_statistics,
)

logger = logging.getLogger(__name__)

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/ch_break_reclaim_microstructure_audit_20260808"
)


def run_audit(
    *,
    out_dir: Path = DEFAULT_OUT,
    limit: int | None = None,
) -> dict[str, Any]:
    events = load_ch_covered_events()
    if limit is not None:
        events = events[:limit]
    logger.info("loaded %s CH-covered events", len(events))
    if len(events) != 54 and limit is None:
        logger.warning("expected 54 events, got %s", len(events))

    extracted = extract_all(events)
    outcomes = outcomes_table(events)
    # merge resolved touch/break into outcomes
    res_map = {r["event_id"]: r for r in extracted["resolutions"]}
    for o in outcomes:
        r = res_map.get(o["event_id"], {})
        o["resolved_first_touch"] = r.get("resolved_first_touch")
        o["resolved_first_break"] = r.get("resolved_first_break")
        o["resolved_first_touch_source"] = r.get("first_touch_source")
        o["resolved_first_break_source"] = r.get("first_break_source")
        o["extract_error"] = r.get("error")

    features = extracted["features"]
    # attach data_quality from quality map if missing
    qmap = {r["event_id"]: r.get("data_quality") for r in extracted["quality"]}
    for f in features:
        f.setdefault("data_quality", qmap.get(f["event_id"]))

    tp_stats = compute_timepoint_statistics(features)
    # also BREAK vs all reclaim/hold as secondary file content inside group stats
    group_stats = compute_group_statistics(features)
    deep_ids = select_deep_dives(outcomes, extracted["quality"])

    summary = write_artifacts(
        out_dir,
        outcomes=outcomes,
        features=features,
        timelines=extracted["timelines"],
        quality=extracted["quality"],
        timepoint_stats=tp_stats,
        group_stats=group_stats,
        resolutions=extracted["resolutions"],
        deep_dive_ids=deep_ids,
    )
    return summary
